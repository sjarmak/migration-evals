"""Tier 4 - Daikon invariants (stub; PRD M1).

Daikon integration is out of scope for the initial funnel landing.
The module is intentionally importable so the funnel can feature-flag
the tier in/out without ``ImportError``; calling :func:`run` raises
:class:`NotImplementedError` which the funnel treats as "skip tier".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from migration_evals.harness.recipe import Recipe
from migration_evals.oracles.verdict import OracleVerdict

TIER_NAME = "daikon"
DEFAULT_COST_USD = 0.10


def run(
    repo_path: Path,
    harness_recipe: Recipe,
    sandbox_adapter: Any,
    *,
    cassette: Optional[Any] = None,
    cost_usd: float = DEFAULT_COST_USD,
) -> OracleVerdict:
    """Raise :class:`NotImplementedError` - the funnel skips this tier.

    When the Daikon integration lands, this function will produce a real
    :class:`OracleVerdict`. Until then, it signals its unavailability by
    raising so the cascade can skip it cleanly.
    """
    raise NotImplementedError(
        "tier4_daikon is a stub; enable via adapters['enable_daikon'] when "
        "the real implementation lands."
    )


__all__ = ["TIER_NAME", "DEFAULT_COST_USD", "run"]
