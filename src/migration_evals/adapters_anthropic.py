"""Anthropic-SDK-backed :class:`~migration_evals.adapters.AnthropicAdapter`.

POC implementation for vj9.6. Wraps the official ``anthropic`` Python
SDK so the Tier-3 judge can issue real Claude calls when the funnel is
not running in cassette-replay mode. Stays a lazy / optional dep:
``anthropic`` is only imported at the moment the adapter actually needs
to instantiate a client - tests inject a fake client via the constructor
and never touch the SDK.

Key concerns
------------
* **Prompt caching passthrough.** The Tier-3 judge sets
  ``cache_control: {type: ephemeral}`` on the rubric block of the
  ``system`` payload. The SDK accepts that shape natively; this adapter
  forwards ``system`` byte-identically so cache hits register and the
  per-call cost stays in the ~$0.02-0.08 band the bead targets.
* **Cost guard (pre-call).** When ``per_call_budget_usd`` is set and the
  worst-case cost (estimated input + ``max_tokens`` of output) exceeds
  it, the adapter raises :class:`BudgetExceededError` *before* issuing
  the call. The estimate uses a 4-chars-per-token rule of thumb; it is
  intentionally conservative.
* **Cost accounting (post-call).** Actual cost is computed from
  ``response.usage`` and accumulated on ``self.total_cost_usd``. Cache
  reads / writes are billed at their distinct rates per the SDK usage
  fields.
* **Return shape.** :class:`migration_evals.oracles.tier3_judge` reads
  ``envelope["content"][0]["text"]``. The SDK returns a Pydantic model;
  we always emit ``response.model_dump()`` so callers see a dict.

Rate table caveat
-----------------
:data:`DEFAULT_COST_RATES` is approximate as of 2026-04 and intended for
budget-guard use, not invoicing. Override via
``adapters.cost_rates_usd_per_mtok`` in the YAML config when prices
change.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "AnthropicSDKAdapter",
    "BudgetExceededError",
    "DEFAULT_COST_RATES",
    "build_anthropic_adapter",
]


# Approximate per-million-token rates as of 2026-04. USD per 1M tokens.
# Source: https://www.anthropic.com/api/pricing - rate updates land roughly
# quarterly; refresh this table when they do. The funnel uses these only
# for the budget guard and post-call cost accounting; mispricing degrades
# the guard but never silently overruns a real spend cap.
DEFAULT_COST_RATES: Mapping[str, Mapping[str, float]] = {
    "claude-haiku-4-5": {
        "input": 1.0,
        "output": 5.0,
        "cache_read": 0.10,
        "cache_write": 1.25,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "claude-opus-4-7": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.50,
        "cache_write": 18.75,
    },
}


class BudgetExceededError(RuntimeError):
    """Raised pre-call when a worst-case cost would exceed the budget."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _estimate_input_tokens(messages: Iterable[Mapping[str, Any]], system: Any) -> int:
    """Rough char/4 input-token estimate, sufficient for budget guarding.

    The Anthropic tokenizer is BPE; 4 chars per token is the documented
    rule of thumb. We deliberately do not import a real tokenizer here -
    the budget guard is a coarse safety net, not a billing predictor.
    """
    char_count = 0
    if isinstance(system, str):
        char_count += len(system)
    elif isinstance(system, list):
        for blk in system:
            if isinstance(blk, Mapping):
                text = blk.get("text")
                if isinstance(text, str):
                    char_count += len(text)
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
    fresh_in = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    cost = (
        fresh_in * rates["input"]
        + out * rates["output"]
        + cache_read * rates["cache_read"]
        + cache_write * rates["cache_write"]
    ) / 1_000_000
    return cost


def _load_anthropic_client(*, api_key: str | None = None) -> Any:
    """Lazy-import + construct an ``anthropic.Anthropic`` client.

    Kept top-level so tests can monkeypatch this single seam instead of
    needing the SDK installed.
    """
    import anthropic  # type: ignore[import-not-found]

    return anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AnthropicSDKAdapter:
    """Wrap ``anthropic.Anthropic().messages.create`` for the Tier-3 judge.

    Parameters
    ----------
    client
        Optional injected SDK client. When ``None``, the adapter lazily
        constructs one via :func:`_load_anthropic_client` on the first
        call - so importing this module never requires ``anthropic`` to
        be installed.
    api_key
        Forwarded to the lazy client when it is created. Falls back to
        ``$ANTHROPIC_API_KEY``.
    cost_rates
        Per-model rate table; see :data:`DEFAULT_COST_RATES`.
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
            cost_rates if cost_rates is not None else DEFAULT_COST_RATES
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
        materialised_messages = list(messages)
        # Pre-call budget guard.
        if self._per_call_budget_usd is not None:
            in_est = _estimate_input_tokens(materialised_messages, system)
            worst = _worst_case_cost(
                model=model,
                in_tokens_est=in_est,
                max_tokens=max_tokens,
                cost_rates=self._cost_rates,
            )
            if worst is not None and worst > self._per_call_budget_usd:
                raise BudgetExceededError(
                    f"per-call budget ${self._per_call_budget_usd:.4f} would be "
                    f"exceeded by worst-case ${worst:.4f} (model={model}, "
                    f"in_est={in_est} tok, max_out={max_tokens} tok)"
                )

        # Capture for auditability before the SDK call so a thrown
        # exception still leaves a record of what we were trying to send.
        self.last_request = {
            "model": model,
            "messages": materialised_messages,
            "system": system,
            "max_tokens": max_tokens,
            **{k: v for k, v in kwargs.items() if k != "cassette"},
        }

        client = (
            self._client
            if self._client is not None
            else _load_anthropic_client(api_key=self._api_key)
        )
        if self._client is None:
            self._client = client

        sdk_kwargs: dict[str, Any] = {
            "model": model,
            "messages": materialised_messages,
            "max_tokens": max_tokens,
        }
        if system is not None:
            sdk_kwargs["system"] = system
        for key, value in kwargs.items():
            if key == "cassette":
                continue
            sdk_kwargs[key] = value

        response = client.messages.create(**sdk_kwargs)
        envelope = response.model_dump() if hasattr(response, "model_dump") else dict(response)

        # Post-call cost accounting.
        usage = envelope.get("usage") or {}
        cost = _actual_cost(model=model, usage=usage, cost_rates=self._cost_rates)
        self.total_cost_usd += cost
        self.call_count += 1
        return envelope


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_anthropic_adapter(
    *,
    repo_path: Path,
    adapters_cfg: Mapping[str, Any],
    cassette_dir: Path | None,
) -> Any:
    """Pick the Anthropic adapter implied by ``adapters_cfg``.

    Config key ``anthropic_provider`` selects the backend:

    * ``"cassette"`` (default) - the replay-cassette stand-in from
      :mod:`migration_evals.cli`. Smoke-config compatible.
    * ``"claude_code"`` - dispatch via the ``claude -p`` CLI using the
      user's Claude Code OAuth credentials. No API key required.
      Optional keys: ``claude_bin`` (default ``claude``),
      ``claude_timeout_s`` (default 120).
    * ``"sdk"`` - :class:`AnthropicSDKAdapter` wrapping ``anthropic``.
      Optional config keys: ``anthropic_api_key`` (else
      ``$ANTHROPIC_API_KEY``), ``anthropic_per_call_budget_usd``,
      ``cost_rates_usd_per_mtok`` (overrides
      :data:`DEFAULT_COST_RATES`).
    """
    provider = (adapters_cfg.get("anthropic_provider") or "cassette").lower()

    if provider == "cassette":
        from migration_evals.cli import _CassetteAnthropicAdapter

        return _CassetteAnthropicAdapter(Path(repo_path).name, cassette_dir)

    if provider == "claude_code":
        from migration_evals.adapters_claude_code import (
            DEFAULT_CLAUDE_BIN,
            DEFAULT_TIMEOUT_S,
            ClaudeCodeAdapter,
        )

        return ClaudeCodeAdapter(
            claude_bin=adapters_cfg.get("claude_bin", DEFAULT_CLAUDE_BIN),
            timeout_s=int(adapters_cfg.get("claude_timeout_s", DEFAULT_TIMEOUT_S)),
        )

    if provider == "sdk":
        api_key = adapters_cfg.get("anthropic_api_key")
        per_call_budget = adapters_cfg.get("anthropic_per_call_budget_usd")
        rates_override = adapters_cfg.get("cost_rates_usd_per_mtok")
        cost_rates = rates_override if isinstance(rates_override, Mapping) else DEFAULT_COST_RATES
        # Adapter lazy-loads the SDK on first call, so importing anthropic
        # only happens at the point a real network call is about to be
        # issued. That keeps cassette-mode users dependency-free.
        return AnthropicSDKAdapter(
            api_key=api_key,
            cost_rates=cost_rates,
            per_call_budget_usd=per_call_budget,
        )

    raise ValueError(
        f"unknown anthropic_provider {provider!r}; expected 'cassette', " f"'claude_code', or 'sdk'"
    )
