"""Unit tests for src/migration_evals/calibration.py (m1w).

Covers the pure-logic surface of the calibration module:

- ``FixtureLabel`` constructor validation.
- ``compute_calibration`` confusion-matrix accounting on hand-built
  observations (a fixture that reaches every tier, a fixture short-
  circuited at tier 0, an off-target known-bad fixture, etc.).
- ``parse_calibration_thresholds`` extraction from a markdown doc.
- ``validate_against_thresholds`` violation detection.

Wiring the funnel through real fixtures lives in
``tests/test_calibrate_script.py``; this module deliberately constructs
:class:`FixtureObservation` values by hand so the math is exercised
without the full funnel surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from migration_evals.calibration import (
    CalibrationReport,
    CalibrationThresholds,
    FixtureLabel,
    FixtureObservation,
    TierCalibration,
    compute_calibration,
    observations_from_funnel_dicts,
    parse_calibration_thresholds,
    validate_against_thresholds,
)

TIER_ORDER = ("diff_valid", "compile_only", "tests")


# ---------------------------------------------------------------------------
# FixtureLabel
# ---------------------------------------------------------------------------


def test_fixture_label_known_good_no_reject_tier() -> None:
    label = FixtureLabel(fixture_id="g1", expected_outcome="pass_all")
    assert label.expected_reject_tier is None


def test_fixture_label_known_bad_requires_reject_tier() -> None:
    with pytest.raises(ValueError, match="expected_reject_tier"):
        FixtureLabel(fixture_id="b1", expected_outcome="reject")


def test_fixture_label_known_good_must_not_set_reject_tier() -> None:
    with pytest.raises(ValueError, match="must not"):
        FixtureLabel(
            fixture_id="g2",
            expected_outcome="pass_all",
            expected_reject_tier="diff_valid",
        )


def test_fixture_label_unknown_outcome_rejected() -> None:
    with pytest.raises(ValueError, match="expected_outcome"):
        FixtureLabel(fixture_id="x", expected_outcome="maybe")


def test_fixture_label_round_trips_via_dict(tmp_path: Path) -> None:
    src = {
        "fixture_id": "g1",
        "expected_outcome": "pass_all",
        "notes": "rewrite foo->bar",
    }
    label = FixtureLabel.from_dict(src)
    assert label.fixture_id == "g1"
    assert label.notes == "rewrite foo->bar"

    path = tmp_path / "label.json"
    path.write_text(json.dumps(src))
    assert FixtureLabel.from_path(path).fixture_id == "g1"


def test_fixture_label_applicable_tiers_defaults_to_all(tmp_path: Path) -> None:
    label = FixtureLabel(fixture_id="g", expected_outcome="pass_all")
    assert label.applicable_tiers is None
    assert label.applies_to("diff_valid")
    assert label.applies_to("compile_only")


def test_fixture_label_applicable_tiers_restricts_scope() -> None:
    label = FixtureLabel(
        fixture_id="g",
        expected_outcome="pass_all",
        applicable_tiers=("diff_valid",),
    )
    assert label.applies_to("diff_valid")
    assert not label.applies_to("compile_only")


def test_fixture_label_reject_tier_must_be_in_applicable_tiers() -> None:
    """A bad fixture targeting compile_only cannot opt out of compile_only."""
    with pytest.raises(ValueError, match="must appear in applicable_tiers"):
        FixtureLabel(
            fixture_id="b",
            expected_outcome="reject",
            expected_reject_tier="compile_only",
            applicable_tiers=("diff_valid",),
        )


def test_fixture_label_applicable_tiers_must_be_non_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        FixtureLabel.from_dict(
            {
                "fixture_id": "g",
                "expected_outcome": "pass_all",
                "applicable_tiers": [],
            }
        )


def test_compute_calibration_skips_off_scope_tiers() -> None:
    """A pass_all fixture with applicable_tiers=['diff_valid'] must not
    contribute to compile_only's known-good denominator even when the
    funnel ran compile_only for it."""
    obs = [
        FixtureObservation(
            label=FixtureLabel(
                fixture_id="g_legacy",
                expected_outcome="pass_all",
                applicable_tiers=("diff_valid",),
            ),
            # The funnel ran every tier, but the fixture intentionally
            # fails compile_only — it was never written to be compiled.
            tier_passed={
                "diff_valid": True,
                "compile_only": False,
                "tests": False,
            },
        ),
        FixtureObservation(
            label=FixtureLabel(
                fixture_id="g_modern",
                expected_outcome="pass_all",
            ),
            tier_passed={
                "diff_valid": True,
                "compile_only": True,
                "tests": True,
            },
        ),
    ]
    report = compute_calibration(obs, migration_id="recipe", tier_order=TIER_ORDER)
    diff = report.tier("diff_valid")
    # Both fixtures inform tier 0 (legacy explicitly, modern by default).
    assert diff.tn == 2
    assert diff.fp == 0

    compile_t = report.tier("compile_only")
    # Only g_modern informs compile_only; g_legacy is out of scope.
    assert compile_t.n_known_good_observed == 1
    assert compile_t.tn == 1
    assert compile_t.fp == 0
    assert compile_t.fpr == 0.0


# ---------------------------------------------------------------------------
# compute_calibration
# ---------------------------------------------------------------------------


def _good(fixture_id: str, **tier_passed: bool) -> FixtureObservation:
    return FixtureObservation(
        label=FixtureLabel(fixture_id=fixture_id, expected_outcome="pass_all"),
        tier_passed=dict(tier_passed),
    )


def _bad(fixture_id: str, expected_reject_tier: str, **tier_passed: bool) -> FixtureObservation:
    return FixtureObservation(
        label=FixtureLabel(
            fixture_id=fixture_id,
            expected_outcome="reject",
            expected_reject_tier=expected_reject_tier,
        ),
        tier_passed=dict(tier_passed),
    )


def test_compute_perfect_calibration() -> None:
    """All known-good pass every tier; all known-bad caught at expected tier."""
    obs = [
        _good("g1", diff_valid=True, compile_only=True, tests=True),
        _good("g2", diff_valid=True, compile_only=True, tests=True),
        # Bad targets tier 0; bad targets tier 1; bad targets tier 2.
        _bad("b0", "diff_valid", diff_valid=False),
        _bad(
            "b1",
            "compile_only",
            diff_valid=True,
            compile_only=False,
        ),
        _bad(
            "b2",
            "tests",
            diff_valid=True,
            compile_only=True,
            tests=False,
        ),
    ]
    report = compute_calibration(obs, migration_id="go_import_rewrite", tier_order=TIER_ORDER)
    assert report.n_known_good == 2
    assert report.n_known_bad == 3

    diff = report.tier("diff_valid")
    assert (diff.tp, diff.fp, diff.tn, diff.fn) == (1, 0, 2, 0)
    assert diff.fpr == 0.0
    assert diff.fnr == 0.0

    compile_t = report.tier("compile_only")
    assert (compile_t.tp, compile_t.fp, compile_t.tn, compile_t.fn) == (
        1,
        0,
        2,
        0,
    )
    assert compile_t.fpr == 0.0
    assert compile_t.fnr == 0.0

    tests_t = report.tier("tests")
    assert (tests_t.tp, tests_t.fp, tests_t.tn, tests_t.fn) == (1, 0, 2, 0)
    assert tests_t.fpr == 0.0
    assert tests_t.fnr == 0.0


def test_compute_charges_fp_to_overzealous_tier() -> None:
    """A known-good rejected at tier 0 charges FP to that tier and is absent
    from later tiers (the cascade short-circuited)."""
    obs = [
        _good("g1", diff_valid=False),  # cascade stops at diff_valid
        _good("g2", diff_valid=True, compile_only=True, tests=True),
    ]
    report = compute_calibration(obs, migration_id="recipe", tier_order=TIER_ORDER)
    diff = report.tier("diff_valid")
    assert diff.fp == 1
    assert diff.tn == 1
    assert diff.fpr == 0.5

    compile_t = report.tier("compile_only")
    # g1 never reached compile_only; g2 did and passed.
    assert compile_t.tn == 1
    assert compile_t.fp == 0
    assert compile_t.n_known_good_observed == 1
    assert compile_t.fpr == 0.0


def test_compute_charges_fn_to_undercatching_tier() -> None:
    """A known-bad targeted at tier 1 that slips through tier 1 charges FN
    to tier 1, and a TN to tier 0 (it correctly passed tier 0)."""
    obs = [
        _bad(
            "b1",
            "compile_only",
            diff_valid=True,
            compile_only=True,
            tests=True,
        ),
    ]
    report = compute_calibration(obs, migration_id="recipe", tier_order=TIER_ORDER)
    # diff_valid sees no known-good (n_kg_obs=0) and no targeted known-bad,
    # so fpr/fnr are None.
    diff = report.tier("diff_valid")
    assert diff.tp == 0 and diff.fp == 0 and diff.tn == 0 and diff.fn == 0
    assert diff.fpr is None
    assert diff.fnr is None

    compile_t = report.tier("compile_only")
    assert compile_t.fn == 1
    assert compile_t.tp == 0
    assert compile_t.fnr == 1.0


def test_compute_off_target_known_bad_does_not_affect_other_tier() -> None:
    """A known-bad targeted at tier 1 contributes nothing to tier 0's FNR
    even when tier 0 also passed it."""
    obs = [
        _bad(
            "b1",
            "compile_only",
            diff_valid=True,
            compile_only=False,
        ),
    ]
    report = compute_calibration(obs, migration_id="recipe", tier_order=TIER_ORDER)
    diff = report.tier("diff_valid")
    # No known-bad targeted at diff_valid -> FNR denominator = 0.
    assert diff.fnr is None
    assert diff.n_known_bad_targeted_observed == 0


def test_compute_skipped_tier_is_not_counted() -> None:
    """A fixture run with a restricted --stages set leaves later tiers
    absent. They do not count toward FPR/FNR for those tiers."""
    obs = [
        _good("g1", diff_valid=True),  # tier-0 only
        _good("g2", diff_valid=True),
    ]
    report = compute_calibration(obs, migration_id="recipe", tier_order=TIER_ORDER)
    compile_t = report.tier("compile_only")
    assert compile_t.n_known_good_observed == 0
    assert compile_t.fpr is None  # zero denominator -> None
    assert compile_t.fnr is None


def test_report_round_trips_through_json() -> None:
    obs = [
        _good("g1", diff_valid=True, compile_only=True),
        _bad("b1", "diff_valid", diff_valid=False),
    ]
    report = compute_calibration(obs, migration_id="x", tier_order=("diff_valid", "compile_only"))
    payload = report.to_json()
    restored = CalibrationReport.from_dict(json.loads(payload))
    assert restored.migration_id == "x"
    assert restored.tier("diff_valid").fp == 0
    assert restored.tier("diff_valid").tp == 1
    assert restored.n_known_good == 1
    assert restored.n_known_bad == 1


# ---------------------------------------------------------------------------
# observations_from_funnel_dicts
# ---------------------------------------------------------------------------


def test_observations_from_funnel_preserves_order_and_pass_state() -> None:
    label = FixtureLabel(fixture_id="g1", expected_outcome="pass_all")
    funnel_dict = {
        "per_tier_verdict": [
            {"tier": "diff_valid", "passed": True, "cost_usd": 0.001, "details": {}},
            {"tier": "compile_only", "passed": False, "cost_usd": 0.05, "details": {}},
        ]
    }
    obs = observations_from_funnel_dicts(label, funnel_dict)
    assert list(obs.tier_passed.keys()) == ["diff_valid", "compile_only"]
    assert obs.tier_passed["compile_only"] is False


# ---------------------------------------------------------------------------
# parse_calibration_thresholds
# ---------------------------------------------------------------------------


_DOC_WITH_TABLE = """\
# whatever

prelude

## Calibration thresholds (per tier)

| tier         | max_fpr | max_fnr |
|--------------|---------|---------|
| diff_valid   | 0.05    | 0.10    |
| compile_only | 0.10    | 0.20    |
| tests        | 0.15    |         |

## Next section

other table

| tier   | max_fpr |
|--------|---------|
| ignore | 0.99    |
"""


def test_parse_thresholds_reads_only_calibration_section() -> None:
    thresholds = parse_calibration_thresholds(_DOC_WITH_TABLE)
    assert "ignore" not in thresholds.per_tier
    assert thresholds.per_tier["diff_valid"] == {
        "max_fpr": 0.05,
        "max_fnr": 0.10,
    }
    assert thresholds.per_tier["compile_only"] == {
        "max_fpr": 0.10,
        "max_fnr": 0.20,
    }
    # Empty cell means no constraint for that rate.
    assert thresholds.per_tier["tests"] == {"max_fpr": 0.15}


def test_parse_thresholds_returns_empty_when_section_missing() -> None:
    thresholds = parse_calibration_thresholds("# no section here")
    assert thresholds.per_tier == {}


# ---------------------------------------------------------------------------
# validate_against_thresholds
# ---------------------------------------------------------------------------


def _report_with_rates(fpr: float | None, fnr: float | None) -> CalibrationReport:
    tier = TierCalibration(
        tier="diff_valid",
        tp=0,
        fp=0,
        tn=0,
        fn=0,
        n_known_good_observed=0,
        n_known_bad_targeted_observed=0,
        fpr=fpr,
        fnr=fnr,
    )
    return CalibrationReport(
        migration_id="x",
        schema_version="v1",
        n_known_good=0,
        n_known_bad=0,
        per_tier=(tier,),
    )


def test_validate_no_thresholds_no_violations() -> None:
    report = _report_with_rates(0.99, 0.99)
    assert validate_against_thresholds(report, CalibrationThresholds(per_tier={})) == []


def test_validate_passes_when_rates_below_thresholds() -> None:
    report = _report_with_rates(0.0, 0.05)
    thresholds = CalibrationThresholds(per_tier={"diff_valid": {"max_fpr": 0.10, "max_fnr": 0.20}})
    assert validate_against_thresholds(report, thresholds) == []


def test_validate_fails_on_fpr_breach() -> None:
    report = _report_with_rates(0.30, 0.0)
    thresholds = CalibrationThresholds(per_tier={"diff_valid": {"max_fpr": 0.10}})
    violations = validate_against_thresholds(report, thresholds)
    assert any("fpr=0.300" in v for v in violations)
    assert any("max_fpr=0.1" in v for v in violations)


def test_validate_fails_on_null_rate_when_threshold_set() -> None:
    report = _report_with_rates(None, None)
    thresholds = CalibrationThresholds(per_tier={"diff_valid": {"max_fpr": 0.10, "max_fnr": 0.20}})
    violations = validate_against_thresholds(report, thresholds)
    # Both null rates blocked by their respective thresholds.
    assert len(violations) == 2
    assert any("fpr is null" in v for v in violations)
    assert any("fnr is null" in v for v in violations)


def test_validate_missing_tier_in_report() -> None:
    report = _report_with_rates(0.0, 0.0)
    thresholds = CalibrationThresholds(per_tier={"compile_only": {"max_fpr": 0.10}})
    violations = validate_against_thresholds(report, thresholds)
    assert any("compile_only" in v for v in violations)
    assert any("missing from calibration report" in v for v in violations)
