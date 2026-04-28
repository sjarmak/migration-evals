"""Batch-change quality oracles (dsm + migration-evals-30w).

These oracles measure properties that should distinguish a good agent
batch change from grep-and-sed: did the agent change only what was
asked, is the diff idempotent, does it beat a deterministic baseline
tool, and did it stay inside the recipe's allowlist of paths it was
allowed to touch? They run alongside the existing tier cascade and emit
verdicts that surface in the ``Batch-change quality`` section of the
report.

Each oracle is a function ``run(repo, quality_spec) -> OracleVerdict``.
Oracles whose recipe does not provide the inputs they need return a
``passed=True`` verdict tagged ``skipped=True`` so the report can show
"not configured" rather than a false-positive pass.

The ``touched_paths`` oracle is the only quality oracle that can flip
``passed=False`` based on a recipe-author choice (``mode="enforce"``).
``diff_minimality`` also returns ``passed=False`` when its calibrated
thresholds are breached. The remaining oracles are informational.
"""

from __future__ import annotations

from pathlib import Path

from migration_evals.oracles.quality import (
    baseline_comparison,
    diff_minimality,
    idempotency,
    touched_paths,
)
from migration_evals.oracles.verdict import OracleVerdict
from migration_evals.quality_spec import QualitySpec

QUALITY_ORACLES = (
    ("diff_minimality", diff_minimality.run),
    ("idempotency", idempotency.run),
    ("baseline_comparison", baseline_comparison.run),
    ("touched_paths", touched_paths.run),
)


def run_quality_oracles(
    repo_path: Path, quality_spec: QualitySpec
) -> tuple[tuple[str, OracleVerdict], ...]:
    """Run every quality oracle in a fixed order.

    The order is ``diff_minimality``, ``idempotency``,
    ``baseline_comparison``, ``touched_paths``. None of them
    short-circuit each other - failure in one is informational at the
    funnel level (the cascade has already produced its verdict by the
    time these run).
    """
    repo = Path(repo_path)
    results: list[tuple[str, OracleVerdict]] = []
    for name, fn in QUALITY_ORACLES:
        verdict = fn(repo, quality_spec)
        results.append((name, verdict))
    return tuple(results)


__all__ = [
    "QUALITY_ORACLES",
    "baseline_comparison",
    "diff_minimality",
    "idempotency",
    "run_quality_oracles",
    "touched_paths",
]
