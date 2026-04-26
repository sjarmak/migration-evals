"""Oracle verdict + funnel result dataclasses (PRD M1).

Every tier in the oracle funnel returns an :class:`OracleVerdict`. The
funnel orchestrator aggregates them into a :class:`FunnelResult`.

Both are ``frozen=True`` so that the cascade cannot accidentally mutate a
verdict after the fact - verdicts cross module boundaries (tier module →
funnel → CLI → result.json writer) and must be safe to share.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple


def _freeze_mapping(m: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return a read-only view of the incoming mapping (or an empty one)."""
    return MappingProxyType(dict(m or {}))


@dataclass(frozen=True)
class OracleVerdict:
    """Verdict emitted by a single oracle tier.

    :param tier: One of the ``OracleTier`` string values (``"compile_only"``,
        ``"tests"``, ``"ast_conformance"``, ``"judge"``, ``"daikon"``).
    :param passed: ``True`` iff the tier considered the trial successful.
    :param cost_usd: Estimated per-invocation USD cost for the tier; used by
        the funnel to compute total cost per trial.
    :param details: Arbitrary per-tier diagnostic payload. Wrapped in a
        read-only ``MappingProxyType`` on construction so callers cannot
        mutate a verdict after emission.
    """

    tier: str
    passed: bool
    cost_usd: float
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:  # pragma: no cover - trivial
        # Enforce immutability of the details mapping without breaking the
        # frozen dataclass contract.
        object.__setattr__(self, "details", _freeze_mapping(self.details))


@dataclass(frozen=True)
class FunnelResult:
    """Aggregate of every tier that ran for a single trial.

    ``per_tier_verdict`` is a tuple of ``(tier_name, OracleVerdict)`` pairs
    in the order the tiers executed. ``final_verdict`` is the verdict that
    terminated the cascade - either the first ``passed=False`` verdict
    (short-circuit) or the last verdict on the path if everything passed.
    """

    per_tier_verdict: Tuple[Tuple[str, OracleVerdict], ...]
    final_verdict: OracleVerdict
    total_cost_usd: float
    failure_class: Optional[str]
    # Side-channel verdicts emitted by the batch-change quality oracles
    # (dsm). They run alongside the cascade and do NOT short-circuit it -
    # ``passed=False`` here is informational, not a tier failure.
    quality_verdicts: Tuple[Tuple[str, OracleVerdict], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict for result.json inclusion."""
        return {
            "per_tier_verdict": [
                {
                    "tier": name,
                    "passed": verdict.passed,
                    "cost_usd": verdict.cost_usd,
                    "details": dict(verdict.details),
                }
                for name, verdict in self.per_tier_verdict
            ],
            "final_verdict": {
                "tier": self.final_verdict.tier,
                "passed": self.final_verdict.passed,
                "cost_usd": self.final_verdict.cost_usd,
                "details": dict(self.final_verdict.details),
            },
            "total_cost_usd": self.total_cost_usd,
            "failure_class": self.failure_class,
            "quality_verdicts": [
                {
                    "tier": name,
                    "passed": verdict.passed,
                    "cost_usd": verdict.cost_usd,
                    "details": dict(verdict.details),
                }
                for name, verdict in self.quality_verdicts
            ],
        }


__all__ = ["OracleVerdict", "FunnelResult"]
