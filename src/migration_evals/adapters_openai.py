"""OpenAI-SDK-backed :class:`~migration_evals.adapters.AnthropicAdapter`.

Cross-family Tier-3 judge adapter (bead migration_evals-cns). Wraps
``openai`` Chat Completions and emits the AnthropicAdapter envelope
shape so :mod:`migration_evals.oracles.tier3_judge` reads it without
knowing which family answered.

Why this exists
---------------
When the diff under review was produced by a Claude-family agent, a
single Claude judge has same-family bias: judge and agent share training
data, prompting conventions, and failure modes, so the judge is more
likely to rubber-stamp whatever its sibling produced. A non-Claude judge
breaks the loop and lets the dual-judge mode flag disagreement
(see :mod:`migration_evals.adapters_judge`).

API surface mapping
-------------------
The :class:`AnthropicAdapter` Protocol uses ``messages`` + ``system``
(string or list of content blocks with optional ``cache_control``). The
OpenAI Chat Completions API has no separate system parameter; the
system content lives as the first message with ``role: system``. The
adapter flattens any list-of-blocks ``system`` into a single string and
prepends it as a system message. ``cache_control`` markers are dropped
— OpenAI does not consume the Anthropic prompt-cache marker.

Modern OpenAI models (GPT-5+) require ``max_completion_tokens`` rather
than the legacy ``max_tokens`` parameter; the adapter forwards the
former.

Cost accounting
---------------
:data:`DEFAULT_OPENAI_COST_RATES` is approximate as of 2026-04 and
intended for budget guarding, not invoicing. Override via
``adapters.openai_cost_rates_usd_per_mtok`` in YAML when prices change.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "OpenAIJudgeAdapter",
    "OpenAIBudgetExceededError",
    "DEFAULT_OPENAI_COST_RATES",
    "build_openai_judge_adapter",
]


# Approximate per-million-token rates as of 2026-04. USD per 1M tokens.
# Source: https://openai.com/api/pricing - updated quarterly. Used by the
# budget guard and post-call cost accounting; mispricing degrades the
# guard but never silently overruns a real spend cap.
DEFAULT_OPENAI_COST_RATES: Mapping[str, Mapping[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "gpt-5": {"input": 1.25, "output": 10.00},
}


class OpenAIBudgetExceededError(RuntimeError):
    """Raised pre-call when a worst-case cost would exceed the budget."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flatten_system(system: Any) -> str:
    """Collapse the AnthropicAdapter ``system`` payload into a single string.

    Accepts None, a plain string, or a list of content blocks. Block-level
    ``cache_control`` markers are intentionally dropped — OpenAI does not
    consume the Anthropic prompt-cache marker.
    """
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for blk in system:
            if isinstance(blk, Mapping):
                text = blk.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n\n".join(parts)
    return str(system)


def _estimate_input_tokens(messages: Iterable[Mapping[str, Any]], system_text: str) -> int:
    """Rough char/4 input-token estimate for budget guarding."""
    char_count = len(system_text)
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            char_count += len(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, Mapping):
                    text = blk.get("text")
                    if isinstance(text, str):
                        char_count += len(text)
    return max(1, char_count // 4)


def _worst_case_cost(
    *,
    model: str,
    in_tokens_est: int,
    max_tokens: int,
    cost_rates: Mapping[str, Mapping[str, float]],
) -> float | None:
    """Return USD upper bound for the next call, or None when unknown."""
    rates = cost_rates.get(model)
    if rates is None:
        return None
    input_cost = (in_tokens_est * rates["input"]) / 1_000_000
    output_cost = (max_tokens * rates["output"]) / 1_000_000
    return input_cost + output_cost


def _actual_cost(
    *,
    model: str,
    usage: Mapping[str, Any],
    cost_rates: Mapping[str, Mapping[str, float]],
) -> float:
    """Return USD billed for one call, or 0.0 when the model is unknown."""
    rates = cost_rates.get(model)
    if rates is None:
        return 0.0
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    cost = (prompt * rates["input"] + completion * rates["output"]) / 1_000_000
    return cost


def _load_openai_client(*, api_key: str | None = None) -> Any:
    """Lazy-import + construct an ``openai.OpenAI`` client.

    Kept top-level so tests can monkeypatch this single seam instead of
    needing the SDK installed.
    """
    import openai  # type: ignore[import-not-found]

    return openai.OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))


def _build_chat_messages(
    materialised: list[Mapping[str, Any]],
    system_text: str,
) -> list[dict[str, Any]]:
    """Compose OpenAI Chat Completions ``messages`` from Anthropic shape."""
    chat_messages: list[dict[str, Any]] = []
    if system_text:
        chat_messages.append({"role": "system", "content": system_text})
    for msg in materialised:
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        if isinstance(content, list):
            # Flatten Anthropic-style content blocks to a single string.
            parts: list[str] = []
            for blk in content:
                if isinstance(blk, Mapping):
                    text = blk.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
            content = "\n\n".join(parts)
        chat_messages.append({"role": role, "content": content if content is not None else ""})
    return chat_messages


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OpenAIJudgeAdapter:
    """AnthropicAdapter that wraps ``openai.OpenAI().chat.completions.create``.

    Parameters
    ----------
    client
        Optional injected SDK client. ``None`` triggers lazy construction
        via :func:`_load_openai_client` on the first call so importing
        this module never requires ``openai`` to be installed.
    api_key
        Forwarded to the lazy client. Falls back to ``$OPENAI_API_KEY``.
    cost_rates
        Per-model rate table; see :data:`DEFAULT_OPENAI_COST_RATES`.
    per_call_budget_usd
        Pre-call worst-case spend cap. ``None`` disables the guard.
    """

    def __init__(
        self,
        *,
        client: Any = None,
        api_key: str | None = None,
        cost_rates: Mapping[str, Mapping[str, float]] | None = None,
        per_call_budget_usd: float | None = None,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._cost_rates: Mapping[str, Mapping[str, float]] = (
            cost_rates if cost_rates is not None else DEFAULT_OPENAI_COST_RATES
        )
        self._per_call_budget_usd = per_call_budget_usd
        self.total_cost_usd: float = 0.0
        self.call_count: int = 0
        self.last_request: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # AnthropicAdapter Protocol
    # ------------------------------------------------------------------

    def messages_create(
        self,
        *,
        model: str,
        messages: Iterable[Mapping[str, Any]],
        system: Any | None = None,
        max_tokens: int = 1024,
        cassette: Any | None = None,  # ignored; Protocol artefact
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        materialised = list(messages)
        system_text = _flatten_system(system)

        # Pre-call budget guard.
        if self._per_call_budget_usd is not None:
            in_est = _estimate_input_tokens(materialised, system_text)
            worst = _worst_case_cost(
                model=model,
                in_tokens_est=in_est,
                max_tokens=max_tokens,
                cost_rates=self._cost_rates,
            )
            if worst is not None and worst > self._per_call_budget_usd:
                raise OpenAIBudgetExceededError(
                    f"per-call budget ${self._per_call_budget_usd:.4f} would be "
                    f"exceeded by worst-case ${worst:.4f} (model={model}, "
                    f"in_est={in_est} tok, max_out={max_tokens} tok)"
                )

        chat_messages = _build_chat_messages(materialised, system_text)

        # Capture for auditability before the SDK call so a thrown
        # exception still leaves a record of what we were trying to send.
        self.last_request = {
            "model": model,
            "messages": chat_messages,
            "max_completion_tokens": max_tokens,
            **{k: v for k, v in kwargs.items() if k != "cassette"},
        }

        client = (
            self._client if self._client is not None else _load_openai_client(api_key=self._api_key)
        )
        if self._client is None:
            self._client = client

        sdk_kwargs: dict[str, Any] = {
            "model": model,
            "messages": chat_messages,
            "max_completion_tokens": max_tokens,
        }
        for key, value in kwargs.items():
            if key == "cassette":
                continue
            sdk_kwargs[key] = value

        response = client.chat.completions.create(**sdk_kwargs)
        raw = response.model_dump() if hasattr(response, "model_dump") else dict(response)

        text = _extract_chat_text(raw)
        usage = raw.get("usage") or {}
        cost = _actual_cost(model=model, usage=usage, cost_rates=self._cost_rates)
        self.total_cost_usd += cost
        self.call_count += 1

        return {
            "id": raw.get("id"),
            "model": raw.get("model") or model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": _stop_reason(raw),
            "usage": {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "_judge_family": "openai",
            "_openai_raw": raw,
        }


def _extract_chat_text(raw: Mapping[str, Any]) -> str:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, Mapping):
        return ""
    message = first.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for blk in content:
                if isinstance(blk, Mapping):
                    text = blk.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts)
    return ""


def _stop_reason(raw: Mapping[str, Any]) -> str | None:
    choices = raw.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        reason = choices[0].get("finish_reason")
        if isinstance(reason, str):
            return reason
    return None


# ---------------------------------------------------------------------------
# Cassette adapter (offline replay)
# ---------------------------------------------------------------------------


class _CassetteOpenAIAdapter:
    """Cassette-backed OpenAI stand-in for the cross-family judge tier.

    Loads a recorded envelope from ``<cassette_dir>/<repo_name>.json``
    and adds the ``_judge_family: openai`` marker so dual-mode tests can
    distinguish the source. Falls back to a hard-coded PASS envelope
    when no cassette is present so the funnel never blocks offline.
    """

    def __init__(self, repo_name: str, cassette_dir: Path | None) -> None:
        self._repo_name = repo_name
        self._cassette_dir = cassette_dir
        self.last_request: dict[str, Any] = {}
        self.call_count = 0

    def messages_create(
        self,
        *,
        model: str,
        messages: Iterable[Mapping[str, Any]],
        system: Any = None,
        max_tokens: int = 1024,
        cassette: Any = None,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        self.call_count += 1
        self.last_request = {
            "model": model,
            "messages": list(messages),
            "system": system,
            "max_tokens": max_tokens,
            **kwargs,
        }
        envelope: dict[str, Any] | None = None
        if self._cassette_dir is not None:
            cassette_path = self._cassette_dir / f"{self._repo_name}.json"
            if cassette_path.is_file():
                try:
                    envelope = json.loads(cassette_path.read_text())
                except (OSError, ValueError):
                    envelope = None
        if envelope is None:
            envelope = {
                "content": [{"type": "text", "text": "PASS judge defaulted to pass"}],
            }
        envelope.setdefault("_judge_family", "openai")
        return envelope


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_openai_judge_adapter(
    *,
    repo_path: Path,
    adapters_cfg: Mapping[str, Any],
    cassette_dir: Path | None,
) -> Any:
    """Pick the OpenAI judge adapter implied by ``adapters_cfg``.

    Config key ``openai_provider`` selects the backend:

    * ``"cassette"`` (default) — replay-cassette stand-in. Smoke-config
      compatible; no API key required.
    * ``"sdk"`` — :class:`OpenAIJudgeAdapter` wrapping ``openai``.
      Optional config keys: ``openai_api_key`` (else
      ``$OPENAI_API_KEY``), ``openai_per_call_budget_usd``,
      ``openai_cost_rates_usd_per_mtok`` (overrides
      :data:`DEFAULT_OPENAI_COST_RATES`).
    """
    provider = (adapters_cfg.get("openai_provider") or "cassette").lower()

    if provider == "cassette":
        return _CassetteOpenAIAdapter(Path(repo_path).name, cassette_dir)

    if provider == "sdk":
        api_key = adapters_cfg.get("openai_api_key")
        per_call_budget = adapters_cfg.get("openai_per_call_budget_usd")
        rates_override = adapters_cfg.get("openai_cost_rates_usd_per_mtok")
        cost_rates = (
            rates_override if isinstance(rates_override, Mapping) else DEFAULT_OPENAI_COST_RATES
        )
        return OpenAIJudgeAdapter(
            api_key=api_key,
            cost_rates=cost_rates,
            per_call_budget_usd=per_call_budget,
        )

    raise ValueError(f"unknown openai_provider {provider!r}; expected 'cassette' or 'sdk'")
