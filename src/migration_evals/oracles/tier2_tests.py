"""Tier 2 — run the migrated repo's existing test suite (PRD M1).

Same sandbox contract as :mod:`tier1_compile`, but invokes
``harness_recipe.test_cmd``. A non-zero exit code fails the tier; tests
that pass are treated as evidence that the migration preserved behavior
within the coverage of the existing suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from migration_evals.harness.recipe import Recipe
from migration_evals.oracles.tier1_compile import _coerce_exit_code
from migration_evals.oracles.verdict import OracleVerdict

TIER_NAME = "tests"
DEFAULT_COST_USD = 0.03
DEFAULT_IMAGE = "build-sandbox:latest"
DEFAULT_TIMEOUT_S = 900


def run(
    repo_path: Path,
    harness_recipe: Recipe,
    sandbox_adapter: Any,
    *,
    image: str = DEFAULT_IMAGE,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    cassette: Optional[Any] = None,
    cost_usd: float = DEFAULT_COST_USD,
    env: Optional[Mapping[str, str]] = None,
) -> OracleVerdict:
    """Run the recipe's test command and return a tests verdict."""
    repo_path = Path(repo_path)
    sandbox_id = sandbox_adapter.create_sandbox(
        image=image,
        env=env,
        cassette=cassette,
    )
    try:
        envelope = sandbox_adapter.exec(
            sandbox_id,
            command=harness_recipe.test_cmd,
            timeout_s=timeout_s,
            cassette=cassette,
        )
    finally:
        try:
            sandbox_adapter.destroy_sandbox(sandbox_id)
        except Exception:  # pragma: no cover - defensive
            pass

    exit_code = _coerce_exit_code(envelope)
    passed = exit_code == 0
    details = {
        "command": harness_recipe.test_cmd,
        "exit_code": exit_code,
        "repo_path": str(repo_path),
        "stdout_tail": str(envelope.get("stdout", ""))[-2048:],
        "stderr_tail": str(envelope.get("stderr", ""))[-2048:],
    }
    return OracleVerdict(tier=TIER_NAME, passed=passed, cost_usd=cost_usd, details=details)


__all__ = ["TIER_NAME", "DEFAULT_COST_USD", "run"]
