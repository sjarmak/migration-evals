"""Tier 1 — compile / typecheck only (PRD M1).

The cheapest oracle in the funnel: run the recipe's ``build_cmd`` in a
sandbox and emit a pass verdict iff the command exits zero. Tests and
semantic checks are deferred to later tiers.

The tier is deliberately agnostic to the sandbox provider — it consumes a
:class:`~migration_evals.adapters.SandboxAdapter`-shaped object,
which can be a real sandbox wrapper, a Docker-backed substitute, or a
replay cassette. The only contract is ``exec(...)`` returning a dict with
``exit_code``/``stdout``/``stderr`` keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from migration_evals.harness.recipe import Recipe
from migration_evals.oracles.verdict import OracleVerdict

TIER_NAME = "compile_only"
DEFAULT_COST_USD = 0.01
DEFAULT_IMAGE = "build-sandbox:latest"
DEFAULT_TIMEOUT_S = 300


def _coerce_exit_code(envelope: Mapping[str, Any]) -> int:
    """Extract an exit code from a sandbox-shaped exec envelope."""
    value = envelope.get("exit_code")
    if value is None:
        value = envelope.get("exitCode")
    if value is None:
        # Missing exit codes are treated as failure — we will not silently
        # pass a tier that cannot prove success.
        return -1
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


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
    """Run the recipe's build command and return a compile-only verdict."""
    repo_path = Path(repo_path)
    sandbox_id = sandbox_adapter.create_sandbox(
        image=image,
        env=env,
        cassette=cassette,
    )
    try:
        envelope = sandbox_adapter.exec(
            sandbox_id,
            command=harness_recipe.build_cmd,
            timeout_s=timeout_s,
            cassette=cassette,
        )
    finally:
        # Best-effort teardown; we do not let a destroy failure mask the
        # real outcome, but we also do not hide it entirely.
        try:
            sandbox_adapter.destroy_sandbox(sandbox_id)
        except Exception:  # pragma: no cover - defensive
            pass

    exit_code = _coerce_exit_code(envelope)
    passed = exit_code == 0

    details = {
        "command": harness_recipe.build_cmd,
        "exit_code": exit_code,
        "repo_path": str(repo_path),
        "stdout_tail": str(envelope.get("stdout", ""))[-1024:],
        "stderr_tail": str(envelope.get("stderr", ""))[-1024:],
    }
    return OracleVerdict(tier=TIER_NAME, passed=passed, cost_usd=cost_usd, details=details)


__all__ = ["TIER_NAME", "DEFAULT_COST_USD", "run"]
