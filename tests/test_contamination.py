"""Contamination split tests (PRD M7).

Covers the pre/post-cutoff bucketing, the 5pp warning threshold, and the
edge cases (empty buckets, malformed dates).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from migration_evals.contamination import (  # noqa: E402
    ContaminationReport,
    WARNING_THRESHOLD_PP,
    split_scores,
)

CUTOFF = date(2025, 1, 1)


def _row(passed: bool, created: str | None) -> dict:
    return {"success": passed, "repo_created_at": created}


def test_returns_contamination_report_dataclass() -> None:
    report = split_scores([], CUTOFF)
    assert isinstance(report, ContaminationReport)
    assert report.score_pre == 0.0
    assert report.score_post == 0.0
    assert report.gap_pp == 0.0
    assert report.warning_flag is False


def test_gap_under_threshold_no_warning() -> None:
    """Gap of 3pp must not raise the warning flag."""
    # Pre-cutoff: 50/100 = 50%
    pre = [_row(True, "2020-06-01")] * 50 + [_row(False, "2020-06-01")] * 50
    # Post-cutoff: 47/100 = 47% → gap = 3pp
    post = [_row(True, "2025-06-01")] * 47 + [_row(False, "2025-06-01")] * 53
    report = split_scores(pre + post, CUTOFF)
    assert report.score_pre == pytest.approx(0.5)
    assert report.score_post == pytest.approx(0.47)
    assert report.gap_pp == pytest.approx(3.0, abs=1e-6)
    assert report.warning_flag is False


def test_gap_over_threshold_triggers_warning() -> None:
    """Gap of 7pp must raise the warning flag."""
    pre = [_row(True, "2020-06-01")] * 60 + [_row(False, "2020-06-01")] * 40
    # 53/100 = 53% → gap = 7pp
    post = [_row(True, "2025-06-01")] * 53 + [_row(False, "2025-06-01")] * 47
    report = split_scores(pre + post, CUTOFF)
    assert report.gap_pp == pytest.approx(7.0, abs=1e-6)
    assert report.warning_flag is True


def test_negative_gap_also_triggers_warning() -> None:
    """abs(gap) > 5 → warning, even if post > pre (unusual but flagged)."""
    pre = [_row(True, "2020-06-01")] * 40 + [_row(False, "2020-06-01")] * 60
    post = [_row(True, "2025-06-01")] * 50 + [_row(False, "2025-06-01")] * 50
    report = split_scores(pre + post, CUTOFF)
    assert report.gap_pp == pytest.approx(-10.0, abs=1e-6)
    assert report.warning_flag is True


def test_threshold_is_strictly_greater_than_5() -> None:
    """Exactly 5pp does NOT trigger; the threshold is strict >."""
    pre = [_row(True, "2020-06-01")] * 55 + [_row(False, "2020-06-01")] * 45
    post = [_row(True, "2025-06-01")] * 50 + [_row(False, "2025-06-01")] * 50
    report = split_scores(pre + post, CUTOFF)
    assert report.gap_pp == pytest.approx(5.0, abs=1e-6)
    assert report.warning_flag is False
    assert WARNING_THRESHOLD_PP == 5.0


def test_empty_pre_bucket_defaults_to_zero() -> None:
    rows = [_row(True, "2025-06-01"), _row(False, "2025-09-01")]
    report = split_scores(rows, CUTOFF)
    assert report.n_pre == 0
    assert report.score_pre == 0.0
    assert report.n_post == 2
    assert report.score_post == pytest.approx(0.5)


def test_empty_post_bucket_defaults_to_zero() -> None:
    rows = [_row(True, "2020-06-01"), _row(True, "2021-09-01")]
    report = split_scores(rows, CUTOFF)
    assert report.n_post == 0
    assert report.score_post == 0.0
    assert report.score_pre == pytest.approx(1.0)


def test_malformed_dates_are_skipped() -> None:
    rows = [
        _row(True, "2020-06-01"),
        _row(True, "not a date"),
        _row(True, None),
        _row(True, ""),
    ]
    report = split_scores(rows, CUTOFF)
    assert report.n_pre == 1
    assert report.n_post == 0
    assert report.score_pre == pytest.approx(1.0)


def test_iso_timestamp_strings_are_parsed() -> None:
    rows = [
        _row(True, "2020-06-01T12:34:56Z"),
        _row(False, "2025-09-15T00:00:00Z"),
    ]
    report = split_scores(rows, CUTOFF)
    assert report.n_pre == 1
    assert report.n_post == 1


def test_cutoff_boundary_date_goes_to_post_bucket() -> None:
    """A repo created exactly on the cutoff is treated as post-cutoff (>=)."""
    rows = [_row(True, "2025-01-01")]
    report = split_scores(rows, CUTOFF)
    assert report.n_post == 1
    assert report.n_pre == 0


def test_to_dict_round_trip() -> None:
    rows = [_row(True, "2020-06-01"), _row(False, "2025-09-15")]
    report = split_scores(rows, CUTOFF)
    payload = report.to_dict()
    assert set(payload.keys()) == {
        "score_pre",
        "score_post",
        "gap_pp",
        "warning_flag",
        "n_pre",
        "n_post",
    }


def test_invalid_cutoff_type_raises() -> None:
    with pytest.raises(TypeError):
        split_scores([], "2025-01-01")  # type: ignore[arg-type]
