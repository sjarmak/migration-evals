"""Tests for migration_evals.stats — Wilson interval + bootstrap proportion CI.

Both helpers are exercised by report.py to attach 95% CIs to per-tier
funnel rates. Wilson is the closed-form check on each tier in
isolation; bootstrap is the empirical check on the cumulative pass-
through rate. Properties tested here are the ones the report relies
on:

* Wilson centers near k/n and shrinks as n grows.
* Both CIs are clamped to [0, 1] (no NaNs, no negatives).
* Degenerate inputs (n=0, all-zero, all-one) yield sensible bounds.
* Bootstrap is deterministic given a fixed seed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from migration_evals.stats import (  # noqa: E402
    Z_95,
    _percentile,
    bootstrap_mean_ci,
    bootstrap_proportion_ci,
    wilson_interval,
)

# ---------------------------------------------------------------------------
# Wilson
# ---------------------------------------------------------------------------


def test_wilson_n_zero_returns_zero_zero() -> None:
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_wilson_clamps_to_unit_interval() -> None:
    lo, hi = wilson_interval(0, 5)
    assert lo == pytest.approx(0.0, abs=1e-9)
    assert 0.0 <= hi <= 1.0
    lo, hi = wilson_interval(5, 5)
    assert hi == pytest.approx(1.0, abs=1e-9)
    assert 0.0 <= lo <= 1.0


def test_wilson_brackets_point_estimate() -> None:
    # 7 of 10 ~= 0.7; CI must straddle 0.7 with positive width
    lo, hi = wilson_interval(7, 10)
    assert lo < 0.7 < hi
    assert hi - lo > 0.0


def test_wilson_shrinks_with_more_data() -> None:
    lo_small, hi_small = wilson_interval(7, 10)
    lo_big, hi_big = wilson_interval(700, 1000)
    assert (hi_big - lo_big) < (hi_small - lo_small)


def test_wilson_known_value() -> None:
    # 10 of 20 with z=1.96 → standard reference: roughly (0.299, 0.701)
    lo, hi = wilson_interval(10, 20)
    assert lo == pytest.approx(0.299, abs=0.005)
    assert hi == pytest.approx(0.701, abs=0.005)


def test_wilson_z_95_constant_matches_normal_quantile() -> None:
    # Just a smoke check that the constant is the 97.5 percentile of N(0,1)
    assert 1.95 < Z_95 < 1.97


# ---------------------------------------------------------------------------
# Bootstrap proportion CI
# ---------------------------------------------------------------------------


def test_bootstrap_empty_returns_zero_zero() -> None:
    assert bootstrap_proportion_ci([]) == (0.0, 0.0)


def test_bootstrap_all_success_returns_unit_endpoint() -> None:
    lo, hi = bootstrap_proportion_ci([True] * 30, n_bootstrap=500, seed=1)
    assert lo == pytest.approx(1.0)
    assert hi == pytest.approx(1.0)


def test_bootstrap_all_failure_returns_zero_endpoint() -> None:
    lo, hi = bootstrap_proportion_ci([False] * 30, n_bootstrap=500, seed=1)
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx(0.0)


def test_bootstrap_brackets_observed_rate() -> None:
    flags = [True] * 7 + [False] * 3
    lo, hi = bootstrap_proportion_ci(flags, n_bootstrap=2000, seed=42)
    assert 0.0 <= lo <= 0.7 <= hi <= 1.0


def test_bootstrap_deterministic_under_fixed_seed() -> None:
    flags = [True, False, True, True, False, True, False, True, True, False]
    a = bootstrap_proportion_ci(flags, n_bootstrap=1000, seed=42)
    b = bootstrap_proportion_ci(flags, n_bootstrap=1000, seed=42)
    assert a == b


def test_bootstrap_changes_under_different_seed() -> None:
    flags = [True, False, True, True, False, True, False, True, True, False]
    a = bootstrap_proportion_ci(flags, n_bootstrap=200, seed=42)
    b = bootstrap_proportion_ci(flags, n_bootstrap=200, seed=7)
    # Not strictly required, but very likely to differ; if this ever
    # triggers a flake, raise n_bootstrap.
    assert a != b


# ---------------------------------------------------------------------------
# bootstrap_mean_ci (continuous)
# ---------------------------------------------------------------------------


def test_bootstrap_mean_empty_returns_zero_zero() -> None:
    assert bootstrap_mean_ci([]) == (0.0, 0.0)


def test_bootstrap_mean_constant_returns_that_value() -> None:
    lo, hi = bootstrap_mean_ci([0.05] * 20, n_bootstrap=500, seed=1)
    assert lo == pytest.approx(0.05)
    assert hi == pytest.approx(0.05)


def test_bootstrap_mean_brackets_observed_mean() -> None:
    values = [0.01, 0.02, 0.03, 0.04, 0.05]
    lo, hi = bootstrap_mean_ci(values, n_bootstrap=2000, seed=42)
    assert lo <= 0.03 <= hi
    assert 0.01 <= lo and hi <= 0.05


def test_bootstrap_mean_matches_proportion_on_indicator() -> None:
    # A proportion is just the mean of a 0/1 indicator, so the two
    # entry points must agree bit-for-bit under the same seed — this is
    # the contract that lets bootstrap_proportion_ci delegate.
    flags = [True, False, True, True, False, True, False, True, True, False]
    prop = bootstrap_proportion_ci(flags, n_bootstrap=1000, seed=42)
    mean = bootstrap_mean_ci([1.0 if f else 0.0 for f in flags], n_bootstrap=1000, seed=42)
    assert prop == mean


# ---------------------------------------------------------------------------
# _percentile boundaries (the bootstrap CI endpoints rely on these)
# ---------------------------------------------------------------------------


def test_percentile_empty_raises() -> None:
    """An empty sample has no percentile; the helper fails loud rather than
    returning a misleading 0.0 that would silently widen a CI."""
    with pytest.raises(ValueError, match="empty input"):
        _percentile([], 50.0)


def test_percentile_clamps_low_and_high_boundaries() -> None:
    """pct<=0 returns the min and pct>=100 returns the max with no
    interpolation — this is what pins the 2.5/97.5 bootstrap-CI endpoints
    to real observed values."""
    vals = [1.0, 2.0, 3.0, 4.0]
    assert _percentile(vals, 0.0) == 1.0
    assert _percentile(vals, -5.0) == 1.0
    assert _percentile(vals, 100.0) == 4.0
    assert _percentile(vals, 150.0) == 4.0


def test_percentile_exact_index_no_interpolation() -> None:
    """When the rank lands on an exact index (lo==hi) the value is returned
    directly rather than interpolated against itself."""
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    # 50th percentile of 5 evenly-spaced points lands exactly on the median.
    assert _percentile(vals, 50.0) == 30.0


def test_percentile_interpolates_between_points() -> None:
    """A rank between two indices linearly interpolates (the non-boundary
    path) — keeps the CI endpoints smooth as the bootstrap distribution
    shifts."""
    vals = [0.0, 10.0]
    assert _percentile(vals, 25.0) == 2.5
