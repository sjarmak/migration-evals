"""Idempotency oracle (dsm).

A correct migration patch should be a *no-op* when re-applied to the
already-migrated tree. The oracle:

1. Snapshots the file bytes at every path the patch claims to touch.
2. Pretends the patch has already been applied (its targets reflect
   the post-state) and re-applies the patch line-by-line via a
   tolerant in-memory matcher: each ``-`` line in a hunk should be
   absent and each ``+`` line should already be present.

If the post-state already contains the additions and lacks the deletions,
the patch is idempotent. Any drift means the diff has internal
inconsistency (rare even for hand-authored patches; common with agent
hallucinations that re-edit lines a second pass).

This deliberately does NOT shell out to ``git apply``: many calibration
fixtures have no ``.git``, and a no-shell implementation makes the
oracle robust on minimal containers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from migration_evals.oracles.tier0_diff import PATCH_ARTIFACT_NAMES
from migration_evals.oracles.verdict import OracleVerdict
from migration_evals.quality_spec import QualitySpec

TIER_NAME = "idempotency"
DEFAULT_COST_USD = 0.0

_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(\S+)")
_HUNK_RE = re.compile(r"^@@ ")


def _find_agent_diff(repo_path: Path) -> Path | None:
    for name in PATCH_ARTIFACT_NAMES:
        candidate = repo_path / name
        if candidate.is_file():
            return candidate
    return None


def _iter_patch_per_file(diff_text: str) -> Iterable[tuple[str, list[str]]]:
    """Yield (target_path, hunk_body_lines) per file in the diff.

    ``hunk_body_lines`` lists every line *inside* hunks (with leading
    +/-/space), excluding the ``@@`` headers themselves. Lines outside
    any hunk are ignored.
    """
    current_path: str | None = None
    in_hunk = False
    body: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            # Flush previous file's body.
            if current_path is not None and body:
                yield current_path, body
            match = _FILE_HEADER_RE.match(line)
            current_path = match.group(1) if match else None
            in_hunk = False
            body = []
            continue
        if line.startswith("--- "):
            continue
        if _HUNK_RE.match(line):
            in_hunk = True
            continue
        if not in_hunk or current_path is None:
            continue
        body.append(line)
    if current_path is not None and body:
        yield current_path, body


def run(repo_path: Path, quality_spec: QualitySpec) -> OracleVerdict:
    repo_path = Path(repo_path)
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

    text = agent_path.read_text(encoding="utf-8", errors="replace")
    drifts: list[str] = []
    files_checked = 0
    files_with_drift = 0
    for target_path, body in _iter_patch_per_file(text):
        # Treat the repo as the post-state; the oracle assumes
        # ``patch.diff`` describes a transformation already applied.
        absolute = repo_path / target_path
        if not absolute.is_file():
            # Patch claims to edit a path the post-state doesn't have.
            drifts.append(f"{target_path}: missing on post-state tree")
            files_with_drift += 1
            files_checked += 1
            continue
        files_checked += 1
        try:
            file_text = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            drifts.append(f"{target_path}: unreadable ({exc})")
            files_with_drift += 1
            continue
        added_lines = [
            line[1:] for line in body if line.startswith("+") and not line.startswith("+++")
        ]
        removed_lines = [
            line[1:] for line in body if line.startswith("-") and not line.startswith("---")
        ]
        # Every "+" line should already be present in the post-state file
        # (re-applying would be a no-op for it). Every "-" line should be
        # absent (re-applying would not find it to remove).
        local_drifts: list[str] = []
        for plus_line in added_lines:
            if plus_line and plus_line not in file_text:
                local_drifts.append(f"missing expected '+': {plus_line[:60]!r}")
        for minus_line in removed_lines:
            if minus_line and minus_line in file_text:
                local_drifts.append(f"unexpected '-' still present: {minus_line[:60]!r}")
        if local_drifts:
            files_with_drift += 1
            drifts.extend(f"{target_path}: {d}" for d in local_drifts[:3])

    idempotent = not drifts
    details: dict[str, Any] = {
        "idempotent": idempotent,
        "files_checked": files_checked,
        "files_with_drift": files_with_drift,
    }
    if drifts:
        details["drift_examples"] = drifts[:8]
    return OracleVerdict(
        tier=TIER_NAME,
        passed=idempotent,
        cost_usd=DEFAULT_COST_USD,
        details=details,
    )


__all__ = ["TIER_NAME", "DEFAULT_COST_USD", "run"]
