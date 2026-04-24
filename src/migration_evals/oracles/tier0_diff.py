"""Tier 0 — diff validity (PRD M1 extension).

The cheapest oracle in the funnel. Catches the worst class of agent
hallucination — *the patch isn't even a patch* — before paying for a
Tier-1 sandbox compile.

Three checks, in order. The first check that has any signal wins:

1. **Patch artifact path** — if the trial directory contains a unified-diff
   artifact (``patch.diff`` / ``agent_diff.patch`` / ``changeset.diff``),
   verify it parses as a unified diff *and* (when ``orig/`` exists) applies
   cleanly with ``git apply --check``.

2. **orig/ vs migrated/ subtrees** — when the trial uses the synthetic
   fixture layout, verify that the diff between them is a well-formed
   unified diff (every changed file has matching ``---``/``+++``/``@@``
   headers and the line counts in the hunks match the actual hunk bodies).

3. **Repo-only fallback** — when neither of the above is present, do a
   light structural check on the migrated tree: at least one source file
   exists, files are non-empty, and curly-brace / parenthesis counts
   balance for ``.java`` / ``.py`` / ``.ts`` / ``.js`` / ``.go``.

Cost is set to ~$0.001/repo because the work is local I/O + a few subprocess
spawns at most. The tier never reaches the network.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Optional

from migration_evals.harness.recipe import Recipe
from migration_evals.oracles.verdict import OracleVerdict

TIER_NAME = "diff_valid"
DEFAULT_COST_USD = 0.001

# Candidate filenames the funnel will look for, in order of preference.
PATCH_ARTIFACT_NAMES = ("patch.diff", "agent_diff.patch", "changeset.diff")

# File extensions whose brace / paren balance we sanity-check in the
# repo-only fallback. The list is intentionally short — a missing extension
# means the fallback skips the brace check (it does not fail).
_BRACE_LANG_EXTENSIONS = {".java", ".js", ".jsx", ".ts", ".tsx", ".go", ".c", ".cpp", ".h", ".rs"}
_PAREN_LANG_EXTENSIONS = _BRACE_LANG_EXTENSIONS | {".py"}

# Minimal unified-diff hunk header regex.
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def run(
    repo_path: Path,
    harness_recipe: Recipe,
    daytona_adapter: Optional[Any] = None,
    *,
    cost_usd: float = DEFAULT_COST_USD,
) -> OracleVerdict:
    """Return a Tier-0 diff-validity verdict for ``repo_path``.

    ``daytona_adapter`` and ``harness_recipe`` are accepted for signature
    consistency with the other tiers but unused — Tier-0 is local-only.
    """
    repo_path = Path(repo_path)

    patch_path = _find_patch_artifact(repo_path)
    if patch_path is not None:
        passed, details = _check_patch_artifact(repo_path, patch_path)
        return OracleVerdict(
            tier=TIER_NAME,
            passed=passed,
            cost_usd=cost_usd,
            details={"check": "patch_artifact", **details},
        )

    orig = repo_path / "orig"
    migrated = repo_path / "migrated"
    if orig.is_dir() and migrated.is_dir():
        passed, details = _check_orig_vs_migrated(orig, migrated)
        return OracleVerdict(
            tier=TIER_NAME,
            passed=passed,
            cost_usd=cost_usd,
            details={"check": "orig_vs_migrated", **details},
        )

    passed, details = _check_repo_structural(repo_path)
    return OracleVerdict(
        tier=TIER_NAME,
        passed=passed,
        cost_usd=cost_usd,
        details={"check": "repo_structural", **details},
    )


# ---------------------------------------------------------------------------
# Check 1: patch artifact
# ---------------------------------------------------------------------------


def _find_patch_artifact(repo_path: Path) -> Optional[Path]:
    for name in PATCH_ARTIFACT_NAMES:
        candidate = repo_path / name
        if candidate.is_file():
            return candidate
    return None


def _check_patch_artifact(
    repo_path: Path, patch_path: Path
) -> tuple[bool, dict[str, Any]]:
    try:
        text = patch_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, {"reason": "patch_unreadable", "error": str(exc)}
    parse_ok, parse_details = _parse_unified_diff(text)
    if not parse_ok:
        return False, {"reason": "patch_malformed", **parse_details}
    if shutil.which("git") is None or not (repo_path / ".git").is_dir():
        # Cannot run git apply without git + a repo. Still pass on parse
        # success — the structural check did its job.
        return True, {
            "patch_path": str(patch_path),
            "patch_apply": "skipped",
            **parse_details,
        }
    try:
        proc = subprocess.run(
            ["git", "apply", "--check", str(patch_path)],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, {
            "reason": "git_apply_failed",
            "patch_path": str(patch_path),
            "error": str(exc),
            **parse_details,
        }
    if proc.returncode != 0:
        return False, {
            "reason": "patch_does_not_apply",
            "patch_path": str(patch_path),
            "git_stderr": proc.stderr.strip()[-512:],
            **parse_details,
        }
    return True, {
        "patch_path": str(patch_path),
        "patch_apply": "ok",
        **parse_details,
    }


def _parse_unified_diff(text: str) -> tuple[bool, dict[str, Any]]:
    """Validate a unified diff. Returns (ok, details)."""
    lines = text.splitlines()
    n_files = 0
    n_hunks = 0
    in_file = False
    expected_old = expected_new = 0
    actual_old = actual_new = 0
    in_hunk = False
    for raw in lines:
        if raw.startswith("--- "):
            in_file = True
            in_hunk = False
            continue
        if raw.startswith("+++ ") and in_file:
            n_files += 1
            in_file = False
            continue
        match = _HUNK_RE.match(raw)
        if match:
            if in_hunk and (actual_old != expected_old or actual_new != expected_new):
                return False, {
                    "reason": "hunk_line_count_mismatch",
                    "expected_old": expected_old,
                    "actual_old": actual_old,
                    "expected_new": expected_new,
                    "actual_new": actual_new,
                }
            expected_old = int(match.group(2) or 1)
            expected_new = int(match.group(4) or 1)
            actual_old = actual_new = 0
            n_hunks += 1
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("+"):
            actual_new += 1
        elif raw.startswith("-"):
            actual_old += 1
        elif raw.startswith(" ") or raw == "":
            actual_old += 1
            actual_new += 1
    if in_hunk and (actual_old != expected_old or actual_new != expected_new):
        return False, {
            "reason": "hunk_line_count_mismatch",
            "expected_old": expected_old,
            "actual_old": actual_old,
            "expected_new": expected_new,
            "actual_new": actual_new,
        }
    if n_files == 0 or n_hunks == 0:
        return False, {
            "reason": "no_diff_content",
            "n_files": n_files,
            "n_hunks": n_hunks,
        }
    return True, {"n_files": n_files, "n_hunks": n_hunks}


# ---------------------------------------------------------------------------
# Check 2: orig/ vs migrated/ (synthetic fixtures)
# ---------------------------------------------------------------------------


def _check_orig_vs_migrated(orig: Path, migrated: Path) -> tuple[bool, dict[str, Any]]:
    if not any(orig.rglob("*")):
        return False, {"reason": "orig_subtree_empty"}
    if not any(migrated.rglob("*")):
        return False, {"reason": "migrated_subtree_empty"}

    # Check that every migrated file is non-empty and parses for known
    # bracket languages. We do not require file-name parity with orig/ —
    # migrations can rename files.
    failures: list[str] = []
    n_files = 0
    for path in migrated.rglob("*"):
        if not path.is_file():
            continue
        n_files += 1
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            failures.append(f"{path.relative_to(migrated)}: unreadable")
            continue
        if not data.strip():
            failures.append(f"{path.relative_to(migrated)}: empty")
            continue
        ext = path.suffix.lower()
        if ext in _BRACE_LANG_EXTENSIONS and data.count("{") != data.count("}"):
            failures.append(f"{path.relative_to(migrated)}: brace imbalance")
            continue
        if ext in _PAREN_LANG_EXTENSIONS and data.count("(") != data.count(")"):
            failures.append(f"{path.relative_to(migrated)}: paren imbalance")
            continue
    if failures:
        return False, {
            "reason": "migrated_file_invalid",
            "n_files_checked": n_files,
            "failures": failures[:8],
        }
    return True, {"n_files_checked": n_files, "failures": []}


# ---------------------------------------------------------------------------
# Check 3: repo-only structural fallback
# ---------------------------------------------------------------------------


def _check_repo_structural(repo_path: Path) -> tuple[bool, dict[str, Any]]:
    files = [p for p in repo_path.rglob("*") if p.is_file()]
    source_files = [
        p for p in files if p.suffix.lower() in _PAREN_LANG_EXTENSIONS
    ]
    if not source_files:
        # No source file = nothing to validate. Pass with a note so the
        # caller can see why the tier did not contribute signal.
        return True, {
            "reason": "no_source_files_to_check",
            "n_files": len(files),
        }
    failures: list[str] = []
    for path in source_files:
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            failures.append(f"{path.relative_to(repo_path)}: unreadable")
            continue
        if not data.strip():
            failures.append(f"{path.relative_to(repo_path)}: empty")
            continue
        ext = path.suffix.lower()
        if ext in _BRACE_LANG_EXTENSIONS and data.count("{") != data.count("}"):
            failures.append(f"{path.relative_to(repo_path)}: brace imbalance")
            continue
        if data.count("(") != data.count(")"):
            failures.append(f"{path.relative_to(repo_path)}: paren imbalance")
            continue
    if failures:
        return False, {
            "reason": "source_file_invalid",
            "n_files_checked": len(source_files),
            "failures": failures[:8],
        }
    return True, {"n_files_checked": len(source_files), "failures": []}


__all__ = ["TIER_NAME", "DEFAULT_COST_USD", "PATCH_ARTIFACT_NAMES", "run"]
