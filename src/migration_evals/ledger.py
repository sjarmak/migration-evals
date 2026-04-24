"""Ledger + regression diff utilities for the migration eval framework.

PRD milestone M5: deterministic content-hash ledger of trial results, plus a
regression diff that compares two result-set roots and surfaces tasks whose
success flipped from True to False.

The ledger layout is:

    <ledger_root>/<task_id>/<content_hash>.json

`content_hash` is sha256 of the JSON-serialized result payload with sorted
keys. Identical payloads collapse into a single file; any change to any field
produces a new entry - this gives us path-independent dedup and an audit trail
of every distinct trial outcome observed for a task.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional


# ---------------------------------------------------------------------------
# Hashing + IO
# ---------------------------------------------------------------------------

def _canonical_json(payload: dict) -> str:
    """Return a deterministic JSON serialization of `payload`.

    Sorted keys and a stable separator so equivalent dicts always hash the
    same regardless of insertion order.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_content_hash(payload: dict) -> str:
    """Content hash for a result payload (sha256 over canonical JSON)."""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _load_result_json(trial_dir: Path) -> dict:
    """Load `trial_dir/result.json`, raising FileNotFoundError if missing."""
    result_path = trial_dir / "result.json"
    if not result_path.is_file():
        raise FileNotFoundError(
            f"result.json not found in trial_dir={trial_dir}"
        )
    with result_path.open("r", encoding="utf-8") as f:
        return json.loads(f.read())


# ---------------------------------------------------------------------------
# Ledger write path
# ---------------------------------------------------------------------------

def write_ledger_entry(trial_dir: Path, ledger_root: Path) -> Path:
    """Write a ledger entry for a single trial.

    Returns the path of the written ledger file. If the content hash already
    exists, the file is still written (idempotent overwrite) but no new entry
    is created - the file count for the task stays the same.
    """
    trial_dir = Path(trial_dir)
    ledger_root = Path(ledger_root)

    payload = _load_result_json(trial_dir)
    if "task_id" not in payload:
        raise ValueError(f"result.json at {trial_dir} is missing 'task_id'")

    task_id = str(payload["task_id"])
    content_hash = compute_content_hash(payload)

    task_dir = ledger_root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    entry_path = task_dir / f"{content_hash}.json"

    # Persist the raw payload plus a provenance sidecar block so later
    # tooling (regression, reporting) can link back to the source trial
    # without re-walking the original directory.
    envelope = dict(payload)
    envelope["__ledger_meta__"] = {
        "trial_dir": str(trial_dir.resolve()),
        "content_hash": content_hash,
    }

    with entry_path.open("w", encoding="utf-8") as f:
        json.dump(envelope, f, sort_keys=True, indent=2)
        f.write("\n")

    return entry_path


# ---------------------------------------------------------------------------
# Regression diff
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegressionEntry:
    """A task that passed in the baseline and failed in the candidate."""

    task_id: str
    trial_dir: Path  # candidate-side trial dir (the failing one)
    prior_agent_version: str
    prior_model: str


def iter_trial_results(root: Path) -> Iterator[tuple[Path, dict]]:
    """Yield `(trial_dir, payload)` for every `result.json` under `root`."""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"root directory not found: {root}")
    for result_path in sorted(root.rglob("result.json")):
        trial_dir = result_path.parent
        try:
            with result_path.open("r", encoding="utf-8") as f:
                payload = json.loads(f.read())
        except (OSError, json.JSONDecodeError):
            # Corrupt files are skipped - do NOT mask the situation;
            # surface via a deterministic marker so the caller can audit.
            continue
        yield trial_dir, payload


def _index_by_task_id(root: Path) -> dict[str, tuple[Path, dict]]:
    """Return `{task_id: (trial_dir, payload)}` for every trial under root.

    When multiple trials share the same task_id, the last one (lexicographic
    order) wins. This matches the typical convention that later trial
    directories supersede earlier ones.
    """
    index: dict[str, tuple[Path, dict]] = {}
    for trial_dir, payload in iter_trial_results(root):
        task_id = payload.get("task_id")
        if isinstance(task_id, str):
            index[task_id] = (trial_dir, payload)
    return index


def compute_regression(
    from_dir: Path,
    to_dir: Path,
) -> list[RegressionEntry]:
    """Return one entry per task that passed in `from_dir` and failed in `to_dir`."""
    baseline = _index_by_task_id(Path(from_dir))
    candidate = _index_by_task_id(Path(to_dir))

    entries: list[RegressionEntry] = []
    for task_id, (prior_trial_dir, prior_payload) in sorted(baseline.items()):
        if not bool(prior_payload.get("success")):
            continue
        if task_id not in candidate:
            continue
        cand_trial_dir, cand_payload = candidate[task_id]
        if bool(cand_payload.get("success")):
            continue
        entries.append(
            RegressionEntry(
                task_id=task_id,
                trial_dir=cand_trial_dir,
                prior_agent_version=str(
                    prior_payload.get("agent_version", "unknown")
                ),
                prior_model=str(prior_payload.get("agent_model", "unknown")),
            )
        )
    return entries


def render_regression_markdown(
    entries: Iterable[RegressionEntry],
    from_dir: Path,
    to_dir: Path,
    out_path: Optional[Path] = None,
) -> str:
    """Render a regression report as Markdown.

    When `out_path` is supplied, trial_dir links are rendered as paths
    relative to `out_path`'s parent directory so the report is portable.
    """
    entries = list(entries)

    def _rel(p: Path) -> str:
        if out_path is None:
            return str(p)
        # Use os.path.relpath so the report works regardless of whether the
        # out_path and trial_dir share a common parent - it will produce
        # '../../foo' style relative paths when needed.
        return os.path.relpath(
            str(Path(p).resolve()),
            start=str(Path(out_path).resolve().parent),
        )

    lines: list[str] = []
    lines.append("# Regression Report")
    lines.append("")
    lines.append(f"Baseline (from): `{from_dir}`")
    lines.append(f"Candidate (to): `{to_dir}`")
    lines.append("")
    lines.append("| task_id | trial_dir | prior_agent_version | prior_model |")
    lines.append("|---------|-----------|---------------------|-------------|")
    for entry in entries:
        link_target = _rel(entry.trial_dir)
        lines.append(
            f"| {entry.task_id} | [{entry.trial_dir.name}]({link_target}) "
            f"| {entry.prior_agent_version} | {entry.prior_model} |"
        )
    lines.append("")
    lines.append(f"Total regressions: {len(entries)}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI helper (invoked from migration_evals.cli)
# ---------------------------------------------------------------------------

def run_regression(
    from_dir: Path,
    to_dir: Path,
    out_path: Path,
) -> int:
    """Compute regression, write markdown, return exit code.

    Exit 0 on success, non-zero on error. This is the canonical CLI handler
    that `migration_evals.cli.regression` delegates to.
    """
    from_dir = Path(from_dir)
    to_dir = Path(to_dir)
    out_path = Path(out_path)

    if not from_dir.is_dir():
        print(f"error: --from directory does not exist: {from_dir}")
        return 2
    if not to_dir.is_dir():
        print(f"error: --to directory does not exist: {to_dir}")
        return 2

    entries = compute_regression(from_dir, to_dir)
    markdown = render_regression_markdown(entries, from_dir, to_dir, out_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(markdown)

    print(
        f"regression: wrote {len(entries)} regressions to {out_path}"
    )
    return 0


__all__ = [
    "RegressionEntry",
    "compute_content_hash",
    "compute_regression",
    "iter_trial_results",
    "render_regression_markdown",
    "run_regression",
    "write_ledger_entry",
]
