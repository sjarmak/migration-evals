"""Tests for migration_evals.ledger (PRD M5).

Covers:
- Content-hash dedup when two trials for the same task_id produce different
  result payloads.
- `write_ledger_entry` round-trips payloads correctly.
- `compute_regression` detects tasks that flipped from pass -> fail.
- CLI subcommand `regression` emits a markdown report with the expected rows.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from migration_evals.ledger import (
    compute_content_hash,
    compute_regression,
    iter_trial_results,
    render_regression_markdown,
    run_regression,
    write_ledger_entry,
)

FIXTURES = _REPO_ROOT / "tests" / "fixtures"
LEDGER_V1 = FIXTURES / "ledger_v1"
LEDGER_V2 = FIXTURES / "ledger_v2"


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------


def test_content_hash_is_deterministic() -> None:
    payload_a = {"task_id": "x", "success": True, "n": 1}
    payload_b = {"n": 1, "success": True, "task_id": "x"}
    assert compute_content_hash(payload_a) == compute_content_hash(payload_b)


def test_content_hash_changes_with_any_field() -> None:
    payload = {"task_id": "x", "success": True}
    mutated = {"task_id": "x", "success": False}
    assert compute_content_hash(payload) != compute_content_hash(mutated)


# ---------------------------------------------------------------------------
# write_ledger_entry + dedup
# ---------------------------------------------------------------------------


def test_write_ledger_entry_creates_file(tmp_path: Path) -> None:
    ledger_root = tmp_path / "ledger"
    entry = write_ledger_entry(LEDGER_V1 / "trial_001", ledger_root)

    assert entry.is_file()
    # path shape: <root>/<task_id>/<hash>.json
    assert entry.parent.name == "task_a"
    assert entry.parent.parent == ledger_root

    written = json.loads(entry.read_text())
    assert written["task_id"] == "task_a"
    assert "__ledger_meta__" in written
    assert written["__ledger_meta__"]["content_hash"] == entry.stem


def test_write_ledger_entry_dedups_by_content_hash(tmp_path: Path) -> None:
    """Two trials same task_id, different agent_version -> two distinct entries."""
    ledger_root = tmp_path / "ledger"

    # Both trials are task_a but v1 has agent_version=v1.0, v2 has v1.1.
    write_ledger_entry(LEDGER_V1 / "trial_001", ledger_root)
    write_ledger_entry(LEDGER_V2 / "trial_001", ledger_root)

    task_a_dir = ledger_root / "task_a"
    entries = sorted(task_a_dir.glob("*.json"))
    assert len(entries) == 2, f"expected 2 distinct entries, got {len(entries)}"


def test_write_ledger_entry_idempotent_same_payload(tmp_path: Path) -> None:
    """Writing the same trial twice produces exactly one ledger file."""
    ledger_root = tmp_path / "ledger"
    write_ledger_entry(LEDGER_V1 / "trial_001", ledger_root)
    write_ledger_entry(LEDGER_V1 / "trial_001", ledger_root)

    task_a_dir = ledger_root / "task_a"
    entries = list(task_a_dir.glob("*.json"))
    assert len(entries) == 1


def test_write_ledger_entry_missing_result(tmp_path: Path) -> None:
    empty_trial = tmp_path / "empty_trial"
    empty_trial.mkdir()
    with pytest.raises(FileNotFoundError):
        write_ledger_entry(empty_trial, tmp_path / "ledger")


# ---------------------------------------------------------------------------
# iter_trial_results
# ---------------------------------------------------------------------------


def test_iter_trial_results_finds_all_trials() -> None:
    found = list(iter_trial_results(LEDGER_V1))
    task_ids = {payload["task_id"] for _, payload in found}
    assert task_ids == {"task_a", "task_b", "task_c"}


# ---------------------------------------------------------------------------
# compute_regression
# ---------------------------------------------------------------------------


def test_compute_regression_detects_only_newly_failing() -> None:
    entries = compute_regression(LEDGER_V1, LEDGER_V2)
    task_ids = [e.task_id for e in entries]
    # task_a still passes; task_b and task_c are new regressions.
    assert set(task_ids) == {"task_b", "task_c"}


def test_regression_entry_captures_prior_provenance() -> None:
    entries = {e.task_id: e for e in compute_regression(LEDGER_V1, LEDGER_V2)}
    for task_id in ("task_b", "task_c"):
        entry = entries[task_id]
        assert entry.prior_agent_version == "v1.0"
        assert entry.prior_model == "claude-sonnet-4-6"
        assert "trial_00" in entry.trial_dir.name


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def test_render_regression_markdown_has_one_row_per_entry(tmp_path: Path) -> None:
    entries = compute_regression(LEDGER_V1, LEDGER_V2)
    out_path = tmp_path / "report.md"
    markdown = render_regression_markdown(entries, LEDGER_V1, LEDGER_V2, out_path)

    # Header + separator + two data rows.
    data_rows = [
        line
        for line in markdown.splitlines()
        if line.startswith("|") and ("task_a" in line or "task_b" in line or "task_c" in line)
    ]
    assert len(data_rows) == 2
    body = "\n".join(data_rows)
    assert "task_b" in body
    assert "task_c" in body
    assert "v1.0" in body
    assert "claude-sonnet-4-6" in body


# ---------------------------------------------------------------------------
# run_regression + CLI entry
# ---------------------------------------------------------------------------


def test_run_regression_writes_report(tmp_path: Path) -> None:
    out_path = tmp_path / "regressed_tasks.md"
    rc = run_regression(LEDGER_V1, LEDGER_V2, out_path)
    assert rc == 0
    assert out_path.is_file()
    body = out_path.read_text()
    assert "task_b" in body
    assert "task_c" in body
    assert "Total regressions: 2" in body


def test_cli_regression_subcommand(tmp_path: Path) -> None:
    out_path = tmp_path / "regressed_tasks.md"
    env = {"PYTHONPATH": str(_REPO_ROOT / "src")}
    # Inherit PATH for python binary resolution.
    import os

    env = {**os.environ, **env}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "migration_evals.cli",
            "regression",
            "--from",
            str(LEDGER_V1),
            "--to",
            str(LEDGER_V2),
            "--out",
            str(out_path),
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert out_path.is_file()
    body = out_path.read_text()
    assert "task_b" in body
    assert "task_c" in body
