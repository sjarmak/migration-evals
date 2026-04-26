"""Diff-minimality oracle (dsm).

Compares the agent's ``patch.diff`` against a recipe-provided
``ground_truth.diff`` and emits the three measurements the codex
review called out:

- ``diff_size_ratio``      = (agent lines added + lines removed)
                              / (ground-truth lines added + lines removed)
- ``touched_files_overlap`` = jaccard(agent files, ground-truth files)
- ``over_edit_pct``        = (files agent touched that ground truth did NOT)
                              / (files agent touched)

The oracle ``passes`` when a hand-tunable threshold per metric is met:
``diff_size_ratio <= 2.0``, ``over_edit_pct <= 0.25``,
``touched_files_overlap >= 0.5``. Any failure marks ``passed=False`` and
reports the breach in ``details``. When the recipe declares no
``ground_truth_diff`` we cannot judge minimality - the oracle returns a
``passed=True`` verdict tagged ``skipped=True``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from migration_evals.oracles.tier0_diff import PATCH_ARTIFACT_NAMES
from migration_evals.oracles.verdict import OracleVerdict
from migration_evals.quality_spec import QualitySpec

TIER_NAME = "diff_minimality"
DEFAULT_COST_USD = 0.0

# Calibrated as starting points - revise as data accumulates.
DEFAULT_MAX_DIFF_SIZE_RATIO = 2.0
DEFAULT_MAX_OVER_EDIT_PCT = 0.25
DEFAULT_MIN_FILES_OVERLAP = 0.5

_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(\S+)")


def _read_diff(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _find_agent_diff(repo_path: Path) -> Path | None:
    for name in PATCH_ARTIFACT_NAMES:
        candidate = repo_path / name
        if candidate.is_file():
            return candidate
    return None


def _diff_summary(diff_text: str) -> tuple[int, int, set[str]]:
    """Return (lines_added, lines_removed, touched_files)."""
    added = removed = 0
    files: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            match = _FILE_HEADER_RE.match(line)
            if match:
                files.add(match.group(1))
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("+++"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
            continue
        if line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed, files


def run(repo_path: Path, quality_spec: QualitySpec) -> OracleVerdict:
    repo_path = Path(repo_path)
    if quality_spec.ground_truth_diff is None:
        return OracleVerdict(
            tier=TIER_NAME, passed=True, cost_usd=DEFAULT_COST_USD,
            details={"skipped": True, "reason": "no ground_truth_diff"},
        )
    ground_truth = Path(quality_spec.ground_truth_diff)
    if not ground_truth.is_file():
        return OracleVerdict(
            tier=TIER_NAME, passed=True, cost_usd=DEFAULT_COST_USD,
            details={
                "skipped": True,
                "reason": "ground_truth_diff missing on disk",
                "ground_truth_path": str(ground_truth),
            },
        )

    agent_path = _find_agent_diff(repo_path)
    if agent_path is None:
        return OracleVerdict(
            tier=TIER_NAME, passed=False, cost_usd=DEFAULT_COST_USD,
            details={"reason": "no agent patch artifact found"},
        )

    agent_added, agent_removed, agent_files = _diff_summary(
        _read_diff(agent_path)
    )
    gt_added, gt_removed, gt_files = _diff_summary(_read_diff(ground_truth))

    agent_total = agent_added + agent_removed
    gt_total = gt_added + gt_removed
    diff_size_ratio: float | None
    if gt_total == 0:
        diff_size_ratio = None
    else:
        diff_size_ratio = agent_total / gt_total

    union_files = agent_files | gt_files
    touched_files_overlap: float | None
    if not union_files:
        touched_files_overlap = None
    else:
        touched_files_overlap = (
            len(agent_files & gt_files) / len(union_files)
        )

    over_edit_pct: float | None
    if not agent_files:
        over_edit_pct = None
    else:
        over_edit_pct = len(agent_files - gt_files) / len(agent_files)

    breaches: list[str] = []
    if (
        diff_size_ratio is not None
        and diff_size_ratio > DEFAULT_MAX_DIFF_SIZE_RATIO
    ):
        breaches.append(
            f"diff_size_ratio={diff_size_ratio:.2f} > "
            f"{DEFAULT_MAX_DIFF_SIZE_RATIO}"
        )
    if (
        over_edit_pct is not None
        and over_edit_pct > DEFAULT_MAX_OVER_EDIT_PCT
    ):
        breaches.append(
            f"over_edit_pct={over_edit_pct:.2f} > "
            f"{DEFAULT_MAX_OVER_EDIT_PCT}"
        )
    if (
        touched_files_overlap is not None
        and touched_files_overlap < DEFAULT_MIN_FILES_OVERLAP
    ):
        breaches.append(
            f"touched_files_overlap={touched_files_overlap:.2f} < "
            f"{DEFAULT_MIN_FILES_OVERLAP}"
        )
    passed = not breaches
    details: dict[str, Any] = {
        "diff_size_ratio": diff_size_ratio,
        "touched_files_overlap": touched_files_overlap,
        "over_edit_pct": over_edit_pct,
        "agent_lines_added": agent_added,
        "agent_lines_removed": agent_removed,
        "ground_truth_lines_added": gt_added,
        "ground_truth_lines_removed": gt_removed,
        "agent_files": sorted(agent_files),
        "ground_truth_files": sorted(gt_files),
        "thresholds": {
            "max_diff_size_ratio": DEFAULT_MAX_DIFF_SIZE_RATIO,
            "max_over_edit_pct": DEFAULT_MAX_OVER_EDIT_PCT,
            "min_touched_files_overlap": DEFAULT_MIN_FILES_OVERLAP,
        },
    }
    if breaches:
        details["breaches"] = breaches
    return OracleVerdict(
        tier=TIER_NAME, passed=passed, cost_usd=DEFAULT_COST_USD,
        details=details,
    )


__all__ = ["TIER_NAME", "DEFAULT_COST_USD", "run"]
