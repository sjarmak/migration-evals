"""Dual-family judge adapter (bead migration_evals-cns).

When the diff under review was produced by a Claude-family agent, a
single Claude judge has same-family bias. This module wires two
:class:`~migration_evals.adapters.AnthropicAdapter`-shaped judges (one
Claude family, one non-Claude — typically OpenAI) so Tier 3 can score
every trial twice and require pairwise agreement before passing.

Wire-up
-------
:func:`build_judge_adapter` is the entry point. When the YAML config
sets ``adapters.judge.dual_family: true``, it returns a
:class:`DualFamilyJudgeAdapter`. Otherwise it falls back to the
existing single-family Anthropic adapter so smoke configs and existing
runs are unaffected.

The dual adapter satisfies :class:`AnthropicAdapter` so the funnel and
:mod:`migration_evals.oracles.tier3_judge` consume it without
additional plumbing. Per-judge verdict extraction lives in
``tier3_judge`` — the adapter's job is mechanical: call both judges,
emit a combined envelope with each side's full response under
``_dual_family``.

Bias mitigation contract
------------------------
Both judges see byte-identical ``messages`` and ``system`` payloads, so
any disagreement reflects model judgment, not prompt drift. The Other
side's ``model`` is configured separately because a Claude model name
would error against an OpenAI client.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "DualFamilyJudgeAdapter",
    "build_judge_adapter",
]


class DualFamilyJudgeAdapter:
    """Cross-family judge wrapper.

    Calls ``anthropic_adapter.messages_create`` and
    ``other_adapter.messages_create`` in sequence with the same
    ``messages`` / ``system`` / ``max_tokens`` payload. The Other side
    receives ``other_model`` (configured at construction) instead of
    the caller's ``model`` so the two clients can be different families.

    Cost (when each side reports it on ``total_cost_usd``) is summed on
    the wrapper's own ``total_cost_usd`` for funnel-level accounting.
    """

    def __init__(
        self,
        *,
        anthropic_adapter: Any,
        other_adapter: Any,
        other_model: str,
    ) -> None:
        self._anthropic = anthropic_adapter
        self._other = other_adapter
        self._other_model = other_model
        self.call_count: int = 0
        self.last_request: dict[str, Any] = {}

    @property
    def total_cost_usd(self) -> float:
        a = float(getattr(self._anthropic, "total_cost_usd", 0.0) or 0.0)
        o = float(getattr(self._other, "total_cost_usd", 0.0) or 0.0)
        return a + o

    def messages_create(
        self,
        *,
        model: str,
        messages: Iterable[Mapping[str, Any]],
        system: Any | None = None,
        max_tokens: int = 1024,
        cassette: Any | None = None,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        # Materialize once so both sides see byte-identical payloads.
        materialised = list(messages)
        self.last_request = {
            "model": model,
            "other_model": self._other_model,
            "messages": materialised,
            "system": system,
            "max_tokens": max_tokens,
        }
        anthropic_envelope = self._anthropic.messages_create(
            model=model,
            messages=materialised,
            system=system,
            max_tokens=max_tokens,
            cassette=cassette,
            **kwargs,
        )
        other_envelope = self._other.messages_create(
            model=self._other_model,
            messages=materialised,
            system=system,
            max_tokens=max_tokens,
            cassette=cassette,
            **kwargs,
        )
        self.call_count += 1
        # Top-level content stays anthropic-shaped so any consumer that
        # ignores _dual_family still sees a coherent verdict text.
        return {
            "content": list(anthropic_envelope.get("content") or []),
            "model": model,
            "_dual_family": {
                "anthropic_envelope": dict(anthropic_envelope),
                "other_envelope": dict(other_envelope),
                "other_model": self._other_model,
            },
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_judge_adapter(
    *,
    repo_path: Path,
    adapters_cfg: Mapping[str, Any],
    anthropic_cassette_dir: Path | None,
    openai_cassette_dir: Path | None = None,
) -> Any:
    """Return the judge adapter configured for this run.

    When ``adapters.judge.dual_family: true``, returns a
    :class:`DualFamilyJudgeAdapter` wrapping:

    * the Anthropic-shaped adapter built by
      :func:`~migration_evals.adapters_anthropic.build_anthropic_adapter`
      (selected via ``anthropic_provider``).
    * a non-Claude adapter selected by ``judge.other_provider``. Today
      only ``"openai"`` is wired (via
      :mod:`migration_evals.adapters_openai`); add families as needed.

    Otherwise returns the single-family Anthropic adapter.
    """
    from migration_evals.adapters_anthropic import build_anthropic_adapter

    judge_cfg = adapters_cfg.get("judge") or {}
    if not judge_cfg.get("dual_family"):
        return build_anthropic_adapter(
            repo_path=repo_path,
            adapters_cfg=adapters_cfg,
            cassette_dir=anthropic_cassette_dir,
        )

    other_provider = str(judge_cfg.get("other_provider") or "openai").lower()
    other_model = judge_cfg.get("other_model")
    if not other_model:
        raise ValueError(
            "adapters.judge.dual_family requires adapters.judge.other_model "
            "(no default — Claude model names are not portable across families)"
        )

    anthropic_adapter = build_anthropic_adapter(
        repo_path=repo_path,
        adapters_cfg=adapters_cfg,
        cassette_dir=anthropic_cassette_dir,
    )

    if other_provider == "openai":
        from migration_evals.adapters_openai import build_openai_judge_adapter

        other_adapter = build_openai_judge_adapter(
            repo_path=repo_path,
            adapters_cfg=adapters_cfg,
            cassette_dir=openai_cassette_dir,
        )
    else:
        raise ValueError(f"unknown judge.other_provider {other_provider!r}; expected 'openai'")

    return DualFamilyJudgeAdapter(
        anthropic_adapter=anthropic_adapter,
        other_adapter=other_adapter,
        other_model=str(other_model),
    )
