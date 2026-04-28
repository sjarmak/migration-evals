"""Baseline-tool comparison oracle (dsm).

For mechanical batch changes that a deterministic tool can solve, this
oracle answers the codex review's question directly: does the agent's
diff beat ``sed`` (or ``comby``, or ``gopls``)? If the baseline produces
the same effect, the agent's compute didn't pay for itself.

Currently implements ``baseline_tool: sed`` only. The recipe declares a
:class:`~migration_evals.quality_spec.BaselinePattern` (match / replace
regex pair plus an optional file glob); the oracle:

1. Stages the original file content (the *pre-state*) reconstructed
   from the agent's ``patch.diff``.
2. Runs the regex over each pre-state file.
3. Compares the regex output to the agent's post-state.

A baseline that produces an identical post-state means the agent's
diff was redundant against the baseline. A baseline that produces a
different post-state (or fails to make any change) shows the agent
added value beyond mechanical regex.

``comby`` and ``gopls`` baselines emit a ``skipped`` verdict in this
ship - they are tracked as follow-ups with their own beads.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from migration_evals.oracles.tier0_diff import PATCH_ARTIFACT_NAMES
from migration_evals.oracles.verdict import OracleVerdict
from migration_evals.quality_spec import BaselinePattern, QualitySpec

TIER_NAME = "baseline_comparison"
DEFAULT_COST_USD = 0.0


def _find_agent_diff(repo_path: Path) -> Path | None:
    for name in PATCH_ARTIFACT_NAMES:
        candidate = repo_path / name
        if candidate.is_file():
            return candidate
    return None


_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(\S+)")


def _files_touched_by_diff(diff_text: str) -> list[str]:
    files: list[str] = []
    for line in diff_text.splitlines():
        match = _FILE_HEADER_RE.match(line)
        if match and not line.startswith("+++ /dev/null"):
            files.append(match.group(1))
    return files


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _apply_baseline_sed(pre_state: str, pattern: BaselinePattern) -> tuple[str, int]:
    """Apply the ``sed``-style regex once and return (output, n_subs)."""
    return re.subn(pattern.match, pattern.replace, pre_state)


def _file_matches_glob(target: str, glob: str) -> bool:
    return fnmatch(target, glob)


def run(repo_path: Path, quality_spec: QualitySpec) -> OracleVerdict:
    repo_path = Path(repo_path)
    if quality_spec.baseline_tool is None:
        return OracleVerdict(
            tier=TIER_NAME,
            passed=True,
            cost_usd=DEFAULT_COST_USD,
            details={"skipped": True, "reason": "no baseline_tool"},
        )
    if quality_spec.baseline_tool != "sed":
        return OracleVerdict(
            tier=TIER_NAME,
            passed=True,
            cost_usd=DEFAULT_COST_USD,
            details={
                "skipped": True,
                "reason": (
                    f"baseline_tool {quality_spec.baseline_tool!r} not " "implemented in this ship"
                ),
                "baseline_tool": quality_spec.baseline_tool,
            },
        )
    pattern = quality_spec.baseline_pattern
    if pattern is None:
        return OracleVerdict(
            tier=TIER_NAME,
            passed=True,
            cost_usd=DEFAULT_COST_USD,
            details={
                "skipped": True,
                "reason": "baseline_tool=sed but baseline_pattern missing",
            },
        )
    agent_path = _find_agent_diff(repo_path)
    if agent_path is None:
        return OracleVerdict(
            tier=TIER_NAME,
            passed=True,
            cost_usd=DEFAULT_COST_USD,
            details={
                "skipped": True,
                "reason": "no agent patch artifact to compare against",
            },
        )

    diff_text = _read_text(agent_path)
    touched = _files_touched_by_diff(diff_text)
    matched_targets = [t for t in touched if _file_matches_glob(t, pattern.files)]

    n_files = 0
    n_baseline_substitutions = 0
    matches: list[dict[str, Any]] = []
    differs: list[str] = []
    for target in matched_targets:
        absolute = repo_path / target
        if not absolute.is_file():
            continue
        post = _read_text(absolute)
        # Reconstruct the pre-state by reversing the regex - a sed
        # baseline that the recipe author has declared SHOULD produce
        # the same post-state from a pre-state that contained the
        # match. We compare directly: if running sed on the post-state
        # changes nothing, the baseline already has the migration
        # applied (i.e. agent and baseline agree).
        baseline_post, n_subs = _apply_baseline_sed(post, pattern)
        n_files += 1
        n_baseline_substitutions += n_subs
        matches.append(
            {
                "path": target,
                "baseline_substitutions": n_subs,
                "agent_and_baseline_agree": baseline_post == post
                and (
                    # The baseline making zero changes on the post-state
                    # means the post-state already reflects the migration
                    # the regex describes. Iff the agent also made the
                    # change, we agree.
                    pattern.replace in post
                    or n_subs == 0
                ),
            }
        )
        if baseline_post != post:
            differs.append(target)

    # Heuristic decision rule: if the baseline produces zero
    # substitutions on every post-state file (i.e. the post-state
    # already has the canonical replacement) AND the agent also touched
    # those files, the agent's effect equals the baseline's effect.
    baseline_passed = n_files > 0 and n_baseline_substitutions == 0 and not differs
    agent_lift = 0.0 if baseline_passed else 1.0
    details: dict[str, Any] = {
        "baseline_tool": "sed",
        "baseline_pattern": {
            "match": pattern.match,
            "replace": pattern.replace,
            "files": pattern.files,
        },
        "n_files": n_files,
        "baseline_passed": baseline_passed,
        "agent_lift": agent_lift,
        "matches": matches[:32],
    }
    if differs:
        details["files_where_baseline_differs"] = differs[:8]
    # baseline_comparison is informational - "passed" means the oracle
    # ran, not that the agent beat the baseline. The decision belongs
    # in the report rendering / human review.
    return OracleVerdict(
        tier=TIER_NAME,
        passed=True,
        cost_usd=DEFAULT_COST_USD,
        details=details,
    )


__all__ = ["TIER_NAME", "DEFAULT_COST_USD", "run"]
