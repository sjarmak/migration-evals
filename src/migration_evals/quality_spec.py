"""Per-recipe quality-oracle configuration (dsm).

A :class:`QualitySpec` carries the optional fields a recipe needs to drive
the batch-change quality oracles (diff_minimality, idempotency,
baseline_comparison, touched_paths). Every field is optional - an oracle
that needs a field but doesn't get one emits a ``skipped`` verdict rather
than failing the trial.

Loaded from the recipe YAML (``configs/recipes/<mig>.yaml``):

    quality:
      ground_truth_diff: configs/recipes/go_import_rewrite.ground_truth.diff
      touched_paths_allowlist:
        - "**/*.go"
      touched_paths_allowlist_mode: warn   # or "enforce"
      baseline_tool: sed
      baseline_pattern:
        match: 'github\\.com/foo/oldpkg'
        replace: 'github.com/foo/newpkg'
        files: '**/*.go'
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALLOWED_BASELINE_TOOLS = ("sed", "comby", "gopls")
ALLOWED_TOUCHED_PATHS_MODES = ("warn", "enforce")


@dataclass(frozen=True)
class BaselinePattern:
    """sed-style match/replace pattern used by ``baseline_comparison``."""

    match: str
    replace: str
    files: str = "**/*"  # glob applied within the repo

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BaselinePattern:
        return cls(
            match=str(data["match"]),
            replace=str(data["replace"]),
            files=str(data.get("files", "**/*")),
        )


@dataclass(frozen=True)
class QualitySpec:
    """Recipe-level quality-oracle configuration.

    All fields are optional. The recipe-author opts in by populating the
    bits they have ground truth for.
    """

    ground_truth_diff: Path | None = None
    touched_paths_allowlist: tuple[str, ...] | None = None
    touched_paths_allowlist_mode: str = "warn"
    baseline_tool: str | None = None
    baseline_pattern: BaselinePattern | None = None

    def __post_init__(self) -> None:
        if self.baseline_tool is not None and self.baseline_tool not in ALLOWED_BASELINE_TOOLS:
            raise ValueError(
                f"baseline_tool must be one of {ALLOWED_BASELINE_TOOLS}; "
                f"got {self.baseline_tool!r}"
            )
        if self.touched_paths_allowlist_mode not in ALLOWED_TOUCHED_PATHS_MODES:
            raise ValueError(
                "touched_paths_allowlist_mode must be one of "
                f"{ALLOWED_TOUCHED_PATHS_MODES}; got "
                f"{self.touched_paths_allowlist_mode!r}"
            )

    @classmethod
    def empty(cls) -> QualitySpec:
        return cls()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> QualitySpec:
        if not data:
            return cls.empty()
        ground_truth_raw = data.get("ground_truth_diff")
        ground_truth = Path(str(ground_truth_raw)) if ground_truth_raw else None
        allowlist_raw = data.get("touched_paths_allowlist")
        allowlist: tuple[str, ...] | None = (
            tuple(str(item) for item in allowlist_raw) if allowlist_raw else None
        )
        baseline_tool = data.get("baseline_tool")
        baseline_pattern_raw = data.get("baseline_pattern")
        baseline_pattern = (
            BaselinePattern.from_dict(baseline_pattern_raw) if baseline_pattern_raw else None
        )
        mode_raw = data.get("touched_paths_allowlist_mode") or "warn"
        return cls(
            ground_truth_diff=ground_truth,
            touched_paths_allowlist=allowlist,
            touched_paths_allowlist_mode=str(mode_raw),
            baseline_tool=str(baseline_tool) if baseline_tool else None,
            baseline_pattern=baseline_pattern,
        )


__all__ = [
    "ALLOWED_BASELINE_TOOLS",
    "ALLOWED_TOUCHED_PATHS_MODES",
    "BaselinePattern",
    "QualitySpec",
]
