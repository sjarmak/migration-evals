"""Shared result.json composition core (bead migration_evals-afi).

Both result writers - the legacy fixture path in ``cli.py`` and the
config-driven path in ``runner.py`` - derive the same four fields from a
:class:`~migration_evals.oracles.verdict.FunnelResult`. Keeping that
mapping in one place means a change to how a funnel outcome lands in
``result.json`` (the contract ``schemas/mig_result.schema.json`` and the
publication gate depend on) cannot drift between the two execution
modes.
"""

from __future__ import annotations

from typing import Any

__all__ = ["funnel_core_fields", "trial_score"]


def funnel_core_fields(funnel_result: Any) -> dict[str, Any]:
    """Map a FunnelResult onto the result.json fields it owns.

    Returns ``success`` / ``failure_class`` / ``oracle_tier`` / ``funnel``
    - the subset both execution modes must serialize identically.
    """
    return {
        "success": bool(funnel_result.final_verdict.passed),
        "failure_class": funnel_result.failure_class,
        "oracle_tier": funnel_result.final_verdict.tier,
        "funnel": funnel_result.to_dict(),
    }


def trial_score(funnel_result: Any) -> float:
    """Binary trial score: 1.0 on a passing final verdict, else 0.0."""
    return 1.0 if bool(funnel_result.final_verdict.passed) else 0.0
