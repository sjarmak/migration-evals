"""Unit tests for the iterator-batch report module.

Covers:
  - load_results: walks a run dir and skips malformed JSON
  - build_iterator_reports: groups by iterator_id, computes aggregates
  - format_report: renders markdown
  - generate_report: end-to-end CLI entry
  - CLI subcommand wiring (`iterator-report`)
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from migration_evals.iterator_report import (  # noqa: E402
    UNBATCHED_KEY,
    build_iterator_reports,
    format_report,
    generate_report,
    load_results,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trial(
    *,
    iterator_id: str | None = "iter-001",
    success: bool = True,
    failure_class: str | None = None,
    oracle_tier: str = "tests",
    cost: float | None = 0.04,
    duration_s: float | None = 12.5,
    agent_model: str = "claude-sonnet-4-6",
    agent_runner: str | None = "claude_code",
) -> dict:
    started = datetime(2026, 4, 24, tzinfo=timezone.utc)
    finished = started + timedelta(seconds=duration_s) if duration_s is not None else None
    payload: dict = {
        "task_id": "java8_17::repo01",
        "agent_model": agent_model,
        "agent_runner": agent_runner,
        "iterator_id": iterator_id,
        "migration_id": "java8_17",
        "success": success,
        "failure_class": failure_class if not success else None,
        "oracle_tier": oracle_tier,
        "score_pre_cutoff": 1.0 if success else 0.0,
        "score_post_cutoff": 1.0 if success else 0.0,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat() if finished is not None else None,
        "funnel": {
            "per_tier_verdict": [],
            "final_verdict": {
                "tier": oracle_tier,
                "passed": success,
                "cost_usd": cost or 0.0,
                "details": {},
            },
            "total_cost_usd": cost,
            "failure_class": failure_class,
        },
    }
    return payload


def _stage_run(tmp_path: Path, trials: list[dict]) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for i, trial in enumerate(trials):
        trial_dir = run_dir / f"trial_{i:03d}"
        trial_dir.mkdir()
        (trial_dir / "result.json").write_text(json.dumps(trial))
    return run_dir


# ---------------------------------------------------------------------------
# load_results
# ---------------------------------------------------------------------------


def test_load_results_walks_recursively(tmp_path: Path) -> None:
    run_dir = _stage_run(tmp_path, [_make_trial(), _make_trial()])
    loaded = load_results(run_dir)
    assert len(loaded) == 2
    assert all(isinstance(r, dict) for r in loaded)


def test_load_results_skips_malformed_json(tmp_path: Path) -> None:
    run_dir = _stage_run(tmp_path, [_make_trial()])
    bad_dir = run_dir / "bad"
    bad_dir.mkdir()
    (bad_dir / "result.json").write_text("{ this is not json")
    loaded = load_results(run_dir)
    assert len(loaded) == 1


def test_load_results_raises_on_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_results(tmp_path / "nope")


# ---------------------------------------------------------------------------
# build_iterator_reports
# ---------------------------------------------------------------------------


def test_build_groups_by_iterator_id() -> None:
    trials = [
        _make_trial(iterator_id="iter-A", success=True),
        _make_trial(iterator_id="iter-A", success=True),
        _make_trial(iterator_id="iter-B", success=False, failure_class="agent_error"),
    ]
    reports = build_iterator_reports(trials)
    assert [r.iterator_id for r in reports] == ["iter-A", "iter-B"]
    assert reports[0].n_total == 2
    assert reports[0].n_completed == 2
    assert reports[1].n_total == 1
    assert reports[1].n_failed == 1
    assert reports[1].failure_class_breakdown == {"agent_error": 1}


def test_build_treats_missing_iterator_id_as_unbatched() -> None:
    trials = [_make_trial(iterator_id=None), _make_trial(iterator_id=None)]
    reports = build_iterator_reports(trials)
    assert len(reports) == 1
    assert reports[0].iterator_id == UNBATCHED_KEY


def test_build_completion_rate_is_correct() -> None:
    trials = [
        _make_trial(iterator_id="i1", success=True),
        _make_trial(iterator_id="i1", success=True),
        _make_trial(iterator_id="i1", success=True),
        _make_trial(iterator_id="i1", success=False, failure_class="harness_error"),
    ]
    reports = build_iterator_reports(trials)
    assert reports[0].completion_rate == 0.75


def test_build_aggregates_failure_class_breakdown() -> None:
    trials = [
        _make_trial(iterator_id="i1", success=False, failure_class="agent_error"),
        _make_trial(iterator_id="i1", success=False, failure_class="agent_error"),
        _make_trial(iterator_id="i1", success=False, failure_class="harness_error"),
        _make_trial(iterator_id="i1", success=True),
    ]
    reports = build_iterator_reports(trials)
    assert reports[0].failure_class_breakdown == {
        "agent_error": 2,
        "harness_error": 1,
    }


def test_build_computes_cost_p50_p95_total() -> None:
    trials = [
        _make_trial(iterator_id="i1", cost=0.01),
        _make_trial(iterator_id="i1", cost=0.05),
        _make_trial(iterator_id="i1", cost=0.10),
        _make_trial(iterator_id="i1", cost=0.50),
    ]
    reports = build_iterator_reports(trials)
    assert reports[0].total_cost_usd == pytest.approx(0.66)
    # p50 of [0.01, 0.05, 0.10, 0.50] is the average of the middle two = 0.075
    assert reports[0].p50_cost_usd == pytest.approx(0.075)
    assert reports[0].p95_cost_usd == pytest.approx(0.44, rel=0.05)


def test_build_handles_missing_cost_data() -> None:
    trials = [
        _make_trial(iterator_id="i1", cost=None),
        _make_trial(iterator_id="i1", cost=None),
    ]
    reports = build_iterator_reports(trials)
    assert reports[0].total_cost_usd is None
    assert reports[0].p50_cost_usd is None


def test_build_computes_duration_p50_p95() -> None:
    trials = [
        _make_trial(iterator_id="i1", duration_s=10.0),
        _make_trial(iterator_id="i1", duration_s=20.0),
        _make_trial(iterator_id="i1", duration_s=100.0),
    ]
    reports = build_iterator_reports(trials)
    assert reports[0].p50_duration_s == pytest.approx(20.0)
    assert reports[0].p95_duration_s == pytest.approx(92.0, rel=0.05)


def test_build_handles_missing_duration_data() -> None:
    trials = [_make_trial(iterator_id="i1", duration_s=None)]
    reports = build_iterator_reports(trials)
    assert reports[0].p50_duration_s is None


def test_build_aggregates_oracle_tier_breakdown() -> None:
    trials = [
        _make_trial(iterator_id="i1", oracle_tier="diff_valid"),
        _make_trial(iterator_id="i1", oracle_tier="compile_only"),
        _make_trial(iterator_id="i1", oracle_tier="compile_only"),
        _make_trial(iterator_id="i1", oracle_tier="tests"),
    ]
    reports = build_iterator_reports(trials)
    assert reports[0].oracle_tier_breakdown == {
        "compile_only": 2,
        "diff_valid": 1,
        "tests": 1,
    }


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


def test_format_report_empty() -> None:
    out = format_report([])
    assert "# Iterator-Batch Report" in out
    assert "No result.json" in out


def test_format_report_emits_table_and_breakdowns() -> None:
    trials = [
        _make_trial(iterator_id="i1", success=True),
        _make_trial(iterator_id="i1", success=False, failure_class="agent_error"),
    ]
    reports = build_iterator_reports(trials)
    out = format_report(reports)
    assert "| iterator_id" in out
    assert "| i1 | 2 | 1 | 1 |" in out
    assert "## Per-batch breakdowns" in out
    assert "### `i1`" in out
    assert "agent_error=1" in out


# ---------------------------------------------------------------------------
# generate_report end-to-end
# ---------------------------------------------------------------------------


def test_generate_report_writes_markdown(tmp_path: Path) -> None:
    run_dir = _stage_run(
        tmp_path,
        [
            _make_trial(iterator_id="iter-X", success=True),
            _make_trial(iterator_id="iter-X", success=True),
            _make_trial(iterator_id="iter-X", success=False, failure_class="oracle_error"),
        ],
    )
    out = tmp_path / "out" / "iter.md"
    rc = generate_report(run_dir, out)
    assert rc == 0
    assert out.is_file()
    text = out.read_text()
    assert "iter-X" in text
    assert "oracle_error=1" in text


def test_generate_report_returns_nonzero_on_missing_run_dir(tmp_path: Path) -> None:
    rc = generate_report(tmp_path / "missing", tmp_path / "out.md")
    assert rc == 2


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_iterator_report_subcommand(tmp_path: Path) -> None:
    run_dir = _stage_run(
        tmp_path,
        [_make_trial(iterator_id="b1"), _make_trial(iterator_id="b1")],
    )
    out = tmp_path / "report.md"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "migration_evals.cli",
            "iterator-report",
            "--run",
            str(run_dir),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
    )
    assert proc.returncode == 0, proc.stderr
    assert out.is_file()
    assert "b1" in out.read_text()
