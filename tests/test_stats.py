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
