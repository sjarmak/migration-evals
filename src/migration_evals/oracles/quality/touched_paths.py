"""Touched-paths allowlist oracle (dsm follow-up, migration-evals-30w).

A recipe declares ``QualitySpec.touched_paths_allowlist`` as a tuple of
fnmatch globs (``**/*.go``, ``docs/**``, ...). This oracle parses the
agent's ``patch.diff`` and reports any path the agent touched that does
not match at least one glob in the union.

Two modes, controlled by ``QualitySpec.touched_paths_allowlist_mode``:

- ``warn`` (default): violations are listed in details, but the verdict
  remains ``passed=True`` so existing recipes can opt into the oracle
  without flipping their trial outcomes.
- ``enforce``: a non-empty violation set sets ``passed=False`` so the
  trial registers a quality failure.

Touched-path extraction handles unified-diff conventions: the literal
``/dev/null`` token (used as a sentinel for "this side has no file" on
adds and deletes) is never recorded, but the real source/target path on
the *other* side of a delete is recorded so the allowlist can gate
deletions as well as edits and creates.

Glob semantics use :func:`fnmatch.fnmatch` (consistent with the existing
``baseline_comparison`` oracle). On Python 3.12 ``fnmatch.translate``
renders ``**/`` as a path-separator-requiring prefix, so ``**/*.go``
matches ``internal/pkg/foo.go`` but NOT root-level ``main.go``. Recipe
authors who want both should union ``**/*.go`` with ``*.go``.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from migration_evals.oracles.tier0_diff import PATCH_ARTIFACT_NAMES
from migration_evals.oracles.verdict import OracleVerdict
from migration_evals.quality_spec import QualitySpec

TIER_NAME = "touched_paths"
DEFAULT_COST_USD = 0.0

# Cap parsed-diff size to bound memory if an agent writes a hostile
# patch.diff. The oracle runs on the host (not in the sandbox), so a
# 500 MB diff would OOM the eval runner. 50 MB is well above any
# legitimate batch-change diff.
MAX_DIFF_BYTES = 50 * 1024 * 1024

# Captures the path after `--- a/` or `+++ b/`, stopping at a tab
# (git's metadata separator) or end-of-line. Wider than the sibling
# oracles' `\S+` form because this oracle gates path fidelity and must
# not silently truncate paths that contain spaces. Both `--- a/...` and
# `+++ b/...` lines are matched - this is intentional, since deletions
# only carry the source path on the `---` side.
_FILE_HEADER_RE = re.compile(r"^(?:---|\+\+\+) (?:[ab]/)?([^\t\r\n]+)")
_DEV_NULL = "/dev/null"


def _find_agent_diff(repo_path: Path) -> Path | None:
    for name in PATCH_ARTIFACT_NAMES:
        candidate = repo_path / name
        if candidate.is_file():
            return candidate
    return None


def _extract_touched_paths(diff_text: str) -> list[str]:
    """Return the de-duplicated set of paths the diff touches, ordered by
    first appearance. Both ``--- a/<path>`` and ``+++ b/<path>`` lines
    contribute (so deletions and renames are gated by the allowlist on
    every path they reference); the literal ``/dev/null`` token is
    dropped, and any trailing whitespace from git's tab-separated
    metadata is stripped."""
    # dict-as-ordered-set: insertion order is preserved (Python 3.7+) and
    # repeat assignments are no-ops for ordering.
    seen: dict[str, None] = {}
    for line in diff_text.splitlines():
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        match = _FILE_HEADER_RE.match(line)
        if match is None:
            continue
        path = match.group(1).rstrip()
        if path == _DEV_NULL:
            continue
        seen[path] = None
    return list(seen)


def run(repo_path: Path, quality_spec: QualitySpec) -> OracleVerdict:
    repo_path = Path(repo_path)
    allowlist = quality_spec.touched_paths_allowlist
    if not allowlist:
        return OracleVerdict(
            tier=TIER_NAME,
            passed=True,
            cost_usd=DEFAULT_COST_USD,
            details={"skipped": True, "reason": "no touched_paths_allowlist"},
        )
    agent_path = _find_agent_diff(repo_path)
    if agent_path is None:
        return OracleVerdict(
            tier=TIER_NAME,
            passed=True,
            cost_usd=DEFAULT_COST_USD,
            details={
                "skipped": True,
                "reason": "no agent patch artifact found",
            },
        )
    try:
        size = agent_path.stat().st_size
    except OSError:
        size = 0
    if size > MAX_DIFF_BYTES:
        return OracleVerdict(
            tier=TIER_NAME,
            passed=True,
            cost_usd=DEFAULT_COST_USD,
            details={
                "skipped": True,
                "reason": (
                    f"patch.diff exceeds MAX_DIFF_BYTES "
                    f"({size} > {MAX_DIFF_BYTES})"
                ),
            },
        )

    diff_text = agent_path.read_text(encoding="utf-8", errors="replace")
    touched = _extract_touched_paths(diff_text)
    violations = [
        path
        for path in touched
        if not any(fnmatch(path, pattern) for pattern in allowlist)
    ]
    mode = quality_spec.touched_paths_allowlist_mode
    passed = True if mode == "warn" else not violations
    details: dict[str, Any] = {
        "mode": mode,
        "allowlist": list(allowlist),
        "touched_paths": touched,
        "violations": violations,
    }
    return OracleVerdict(
        tier=TIER_NAME,
        passed=passed,
        cost_usd=DEFAULT_COST_USD,
        details=details,
    )


__all__ = ["TIER_NAME", "DEFAULT_COST_USD", "run"]
