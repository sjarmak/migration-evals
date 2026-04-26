"""Report-rendering tests for the dsm batch-change quality section."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from migration_evals.report import (  # noqa: E402
    _quality_aggregate,
    build_report_data,
    format_report,
)


def _trial(
    *,
    success: bool = True,
    quality: list[dict] | None = None,
) -> dict:
    return {
        "task_id": "t",
        "success": success,
        "failure_class": None if success else "agent_error",
        "oracle_spec_sha": "0" * 64,
        "recipe_spec_sha": "0" * 64,
        "pre_reg_sha": "0" * 64,
        "agent_model": "x",
        "migration_id": "go_import_rewrite",
        "funnel": {
            "per_tier_verdict": [
                {"tier": "compile_only", "passed": True, "details": {}}
            ],
            "quality_verdicts": quality or [],
        },
    }


def _quality_verdict(
    tier: str,
    *,
    passed: bool = True,
    details: dict | None = None,
) -> dict:
    return {
        "tier": tier,
        "passed": passed,
        "cost_usd": 0.0,
        "details": details or {},
    }


def test_quality_aggregate_counts_pass_and_skip() -> None:
    results = [
        _trial(quality=[
            _quality_verdict(
                "diff_minimality",
                passed=True,
                details={
                    "diff_size_ratio": 1.2,
                    "over_edit_pct": 0.1,
                    "touched_files_overlap": 0.9,
                },
            ),
            _quality_verdict(
                "idempotency", passed=True,
                details={"idempotent": True},
            ),
            _quality_verdict(
                "baseline_comparison", passed=True,
                details={"baseline_passed": True, "agent_lift": 0.0},
            ),
        ]),
        _trial(quality=[
            _quality_verdict(
                "diff_minimality", passed=True,
                details={"skipped": True, "reason": "no gt"},
            ),
            _quality_verdict(
                "idempotency", passed=False,
                details={"idempotent": False},
            ),
            _quality_verdict(
                "baseline_comparison", passed=True,
                details={"baseline_passed": False, "agent_lift": 1.0},
            ),
        ]),
    ]
    rows = _quality_aggregate(results)
    by_tier = {r["tier_name"]: r for r in rows}
    assert by_tier["diff_minimality"]["n_observed"] == 2
    assert by_tier["diff_minimality"]["n_passed"] == 2
    assert by_tier["diff_minimality"]["n_skipped"] == 1
    # mean is over the non-skipped trial only (the second was skipped =>
    # no diff_size_ratio in details).
    assert by_tier["diff_minimality"]["mean_diff_size_ratio"] == 1.2
    assert by_tier["idempotency"]["n_passed"] == 1
    assert by_tier["idempotency"]["n_observed"] == 2
    assert by_tier["baseline_comparison"]["baseline_passed_rate"] == 0.5


def test_quality_aggregate_handles_legacy_results_without_field() -> None:
    """Older result.json without quality_verdicts should report 0 observed
    rather than crashing."""
    results = [
        {
            "task_id": "legacy",
            "success": True,
            "failure_class": None,
            "agent_model": "x",
            "migration_id": "go_import_rewrite",
            "funnel": {"per_tier_verdict": []},
        }
    ]
    rows = _quality_aggregate(results)
    assert all(r["n_observed"] == 0 for r in rows)


def test_format_report_includes_quality_section(tmp_path: Path) -> None:
    """End-to-end: build_report_data + format_report on a run dir with
    one trial that emitted all three quality verdicts."""
    run_dir = tmp_path / "run"
    trial_dir = run_dir / "trial_001"
    trial_dir.mkdir(parents=True)
    payload = _trial(quality=[
        _quality_verdict(
            "diff_minimality", passed=True,
            details={"diff_size_ratio": 1.0,
                     "over_edit_pct": 0.0,
                     "touched_files_overlap": 1.0},
        ),
        _quality_verdict(
            "idempotency", passed=True,
            details={"idempotent": True},
        ),
        _quality_verdict(
            "baseline_comparison", passed=True,
            details={"baseline_passed": True, "agent_lift": 0.0},
        ),
    ])
    (trial_dir / "result.json").write_text(json.dumps(payload))
    data = build_report_data(run_dir)
    md = format_report(data)
    assert "Batch-change quality" in md
    assert "diff_minimality" in md
    assert "idempotency" in md
    assert "baseline_comparison" in md
    # Confirm the means surface.
    assert "mean diff_size_ratio" in md


def test_format_report_quality_empty_when_no_verdicts(tmp_path: Path) -> None:
    """A run dir whose trials never emitted quality verdicts gets a
    no-quality-block notice rather than an empty table."""
    run_dir = tmp_path / "run"
    trial_dir = run_dir / "trial_001"
    trial_dir.mkdir(parents=True)
    payload = _trial(quality=[])
    (trial_dir / "result.json").write_text(json.dumps(payload))
    data = build_report_data(run_dir)
    md = format_report(data)
    assert "Batch-change quality" in md
    assert "no quality verdicts emitted" in md
