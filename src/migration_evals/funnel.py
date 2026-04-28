"""Tiered-oracle funnel orchestrator (PRD M1).

:func:`run_funnel` cascades a single trial through the five-tier funnel:

    T1 compile_only  -> T2 tests  -> T2b ast_conformance (synthetic only)
      -> T3 judge    -> T4 daikon (only if ``enable_daikon``)

The cascade short-circuits on the first ``passed=False`` verdict.
A tier that raises :class:`NotImplementedError` is treated as *skipped*
(no verdict recorded) so the Daikon stub cannot break the funnel.

The ``adapters`` argument is a plain mapping so callers can add new
entries (e.g. ``openrewrite`` for future tiers) without changing the
signature. The known keys are ``"sandbox"``, ``"anthropic"``, and
``"enable_daikon"``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from migration_evals.harness.recipe import Recipe
from migration_evals.oracles import (
    tier0_diff,
    tier1_compile,
    tier2_tests,
    tier3_judge,
    tier4_daikon,
)
from migration_evals.oracles.verdict import FunnelResult, OracleVerdict
from migration_evals.synthetic import ast_oracle
from migration_evals.types import FailureClass

AST_TIER_NAME = "ast_conformance"
AST_DEFAULT_COST_USD = 0.0

# Stage alias table - maps CLI --stage values to the set of tiers to run.
STAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "diff": (tier0_diff.TIER_NAME,),
    "compile": (tier1_compile.TIER_NAME,),
    "tests": (tier2_tests.TIER_NAME,),
    "judge": (tier3_judge.TIER_NAME,),
    "daikon": (tier4_daikon.TIER_NAME,),
    "all": (
        tier0_diff.TIER_NAME,
        tier1_compile.TIER_NAME,
        tier2_tests.TIER_NAME,
        AST_TIER_NAME,
        tier3_judge.TIER_NAME,
        tier4_daikon.TIER_NAME,
    ),
}


def _ast_verdict(repo_path: Path, recipe: Recipe) -> OracleVerdict:
    """Wrap :func:`ast_oracle.check` into an :class:`OracleVerdict`.

    The synthetic generator produces both ``orig/`` and ``migrated/``
    sub-trees under the repo. If either is missing we treat the tier as a
    skip-pass (details carry ``skipped=True``).
    """
    orig = repo_path / "orig"
    migrated = repo_path / "migrated"
    if not orig.is_dir() or not migrated.is_dir():
        return OracleVerdict(
            tier=AST_TIER_NAME,
            passed=True,
            cost_usd=AST_DEFAULT_COST_USD,
            details={
                "skipped": True,
                "reason": "missing orig/ or migrated/ subtree",
            },
        )
    report = ast_oracle.check(orig, migrated)
    passed = report.get("overall") != "fail"
    return OracleVerdict(
        tier=AST_TIER_NAME,
        passed=passed,
        cost_usd=AST_DEFAULT_COST_USD,
        details={"ast_report": report},
    )


def _failure_class_for(tier_name: str) -> str:
    """Map the tier that short-circuited to a :class:`FailureClass` value."""
    if tier_name == tier0_diff.TIER_NAME:
        # A malformed patch / unparseable file is the agent's fault, not the
        # harness or the oracle.
        return FailureClass.AGENT_ERROR.value
    if tier_name == tier1_compile.TIER_NAME:
        return FailureClass.HARNESS_ERROR.value
    return FailureClass.AGENT_ERROR.value


def _should_run(
    tier_name: str,
    *,
    stages: tuple[str, ...] | None,
    is_synthetic: bool,
    enable_daikon: bool,
) -> bool:
    if tier_name == AST_TIER_NAME and not is_synthetic:
        return False
    if tier_name == tier4_daikon.TIER_NAME and not enable_daikon:
        return False
    if stages is None:
        return True
    return tier_name in stages


def run_funnel(
    repo: Path,
    recipe: Recipe,
    adapters: Mapping[str, Any],
    *,
    is_synthetic: bool = False,
    stages: tuple[str, ...] | None = None,
) -> FunnelResult:
    """Cascade ``repo`` through the tiered funnel and return a :class:`FunnelResult`.

    Short-circuits on the first tier whose verdict is ``passed=False``;
    otherwise the verdict from the last executed tier becomes the
    ``final_verdict``. Tiers that raise :class:`NotImplementedError` are
    skipped (no verdict recorded, no cost accumulated).
    """
    repo_path = Path(repo)
    enable_daikon = bool(adapters.get("enable_daikon"))
    sandbox = adapters.get("sandbox")
    anthropic = adapters.get("anthropic")

    pipeline: list[tuple[str, Callable[[], OracleVerdict]]] = [
        (
            tier0_diff.TIER_NAME,
            lambda: tier0_diff.run(repo_path, recipe, sandbox),
        ),
        (
            tier1_compile.TIER_NAME,
            lambda: tier1_compile.run(repo_path, recipe, sandbox),
        ),
        (
            tier2_tests.TIER_NAME,
            lambda: tier2_tests.run(repo_path, recipe, sandbox),
        ),
        (AST_TIER_NAME, lambda: _ast_verdict(repo_path, recipe)),
        (
            tier3_judge.TIER_NAME,
            lambda: tier3_judge.run(repo_path, recipe, anthropic),
        ),
        (
            tier4_daikon.TIER_NAME,
            lambda: tier4_daikon.run(repo_path, recipe, sandbox),
        ),
    ]

    verdicts: list[tuple[str, OracleVerdict]] = []
    total_cost = 0.0

    for tier_name, invoker in pipeline:
        if not _should_run(
            tier_name,
            stages=stages,
            is_synthetic=is_synthetic,
            enable_daikon=enable_daikon,
        ):
            continue
        try:
            verdict = invoker()
        except NotImplementedError:
            # Skip stub tiers without breaking the cascade.
            continue
        verdicts.append((tier_name, verdict))
        total_cost += float(verdict.cost_usd)
        if not verdict.passed:
            quality_verdicts = _run_quality_oracles_safe(repo_path, adapters)
            return FunnelResult(
                per_tier_verdict=tuple(verdicts),
                final_verdict=verdict,
                total_cost_usd=round(total_cost, 6),
                failure_class=_failure_class_for(tier_name),
                quality_verdicts=quality_verdicts,
            )

    if not verdicts:
        # No tier ran - treat as harness error so the trial is visibly broken
        # rather than silently marked successful.
        empty = OracleVerdict(
            tier="none",
            passed=False,
            cost_usd=0.0,
            details={"reason": "no tiers executed"},
        )
        return FunnelResult(
            per_tier_verdict=(),
            final_verdict=empty,
            total_cost_usd=0.0,
            failure_class=FailureClass.HARNESS_ERROR.value,
        )

    last_name, last_verdict = verdicts[-1]
    quality_verdicts = _run_quality_oracles_safe(repo_path, adapters)
    return FunnelResult(
        per_tier_verdict=tuple(verdicts),
        final_verdict=last_verdict,
        total_cost_usd=round(total_cost, 6),
        failure_class=None,
        quality_verdicts=quality_verdicts,
    )


def _run_quality_oracles_safe(
    repo_path: Path, adapters: Mapping[str, Any]
) -> tuple[tuple[str, OracleVerdict], ...]:
    """Run the batch-change quality oracles when the recipe configures one.

    A trial without ``adapters['quality_spec']`` skips the quality phase
    entirely (empty tuple, backward-compatible). Quality oracles are
    informational - we deliberately swallow any unexpected exception from
    them rather than letting a bug in a quality oracle break a trial that
    the cascade already produced a verdict for.
    """
    quality_spec = adapters.get("quality_spec")
    if quality_spec is None:
        return ()
    # Local import to avoid a circular module load (oracles.quality
    # transitively imports verdict.py which imports nothing from funnel,
    # but keeping it deferred makes the lazy boundary explicit).
    from migration_evals.oracles.quality import run_quality_oracles

    try:
        return run_quality_oracles(repo_path, quality_spec)
    except Exception as exc:  # pragma: no cover - defensive
        skip = OracleVerdict(
            tier="quality_phase",
            passed=True,
            cost_usd=0.0,
            details={"skipped": True, "reason": f"quality_phase_error: {exc}"},
        )
        return (("quality_phase", skip),)


__all__ = [
    "AST_TIER_NAME",
    "AST_DEFAULT_COST_USD",
    "STAGE_ALIASES",
    "run_funnel",
]
