"""End-to-end tests for the calibration driver and the gate's
``--require-calibration`` mode (m1w).

These tests execute ``scripts/calibrate.py`` against the committed
``go_import_rewrite`` corpus and the ``--require-calibration`` flag of the
publication gate against synthesised manifests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from migration_evals.calibration import CalibrationReport  # noqa: E402

REPO_ROOT = _REPO_ROOT
CALIBRATE_SCRIPT = REPO_ROOT / "scripts" / "calibrate.py"
GATE_MODULE = "migration_evals.publication_gate"
CALIBRATION_FIXTURES = (
    REPO_ROOT / "tests" / "fixtures" / "calibration" / "go_import_rewrite"
)
HYPOTHESES_PATH = REPO_ROOT / "docs" / "hypotheses_and_thresholds.md"
RUN_STAMPED = REPO_ROOT / "tests" / "fixtures" / "run_stamped"


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}


# ---------------------------------------------------------------------------
# scripts/calibrate.py
# ---------------------------------------------------------------------------


def test_calibrate_emits_clean_tier_zero_calibration(tmp_path: Path) -> None:
    out = tmp_path / "calibration.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(CALIBRATE_SCRIPT),
            "--migration", "go_import_rewrite",
            "--fixtures", str(CALIBRATION_FIXTURES),
            "--output", str(out),
            "--stages", "diff",
        ],
        capture_output=True, text=True,
        cwd=str(REPO_ROOT), env=_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert out.is_file()
    report = CalibrationReport.from_path(out)
    assert report.migration_id == "go_import_rewrite"
    assert report.n_known_good == 10
    assert report.n_known_bad == 10
    diff = report.tier("diff_valid")
    # Corpus is hand-vetted for tier-0; both rates must be zero.
    assert diff.fpr == 0.0
    assert diff.fnr == 0.0
    assert diff.tp == 10 and diff.tn == 10
    # Tiers above tier 0 weren't run; their rates are unobserved.
    assert report.tier("compile_only").fpr is None
    assert report.tier("compile_only").fnr is None


def test_calibrate_fails_on_missing_fixtures_dir(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable, str(CALIBRATE_SCRIPT),
            "--migration", "go_import_rewrite",
            "--fixtures", str(tmp_path / "does_not_exist"),
            "--output", str(tmp_path / "out.json"),
        ],
        capture_output=True, text=True,
        cwd=str(REPO_ROOT), env=_env(),
    )
    assert proc.returncode == 1
    assert "does not exist" in proc.stderr


def test_calibrate_fails_on_unknown_stage(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable, str(CALIBRATE_SCRIPT),
            "--migration", "x",
            "--fixtures", str(CALIBRATION_FIXTURES),
            "--output", str(tmp_path / "out.json"),
            "--stages", "diff,brunch",
        ],
        capture_output=True, text=True,
        cwd=str(REPO_ROOT), env=_env(),
    )
    assert proc.returncode == 1
    assert "unknown --stages" in proc.stderr


# ---------------------------------------------------------------------------
# publication_gate --require-calibration
# ---------------------------------------------------------------------------


def _stage_run_with_calibration(
    *,
    src: Path,
    dst: Path,
    calibration_payload: dict | None,
    declare_in_manifest: bool,
) -> Path:
    """Copy the run_stamped fixture into ``dst`` and (optionally) add a
    committed ``calibration.json`` plus the manifest pointer to it.

    Mirrors ``test_pre_reg._stage_run`` for the hypotheses-path absolutising
    so the gate resolves the doc against the real committed file.
    """
    shutil.copytree(src, dst)
    manifest_path = dst / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["hypotheses"] = str(HYPOTHESES_PATH)
    if calibration_payload is not None:
        cal_path = dst / "calibration.json"
        cal_path.write_text(json.dumps(calibration_payload, indent=2))
        if declare_in_manifest:
            manifest["calibration_report"] = str(cal_path)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return dst


def _run_gate(run_dir: Path, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, "-m", GATE_MODULE,
            "--check-run", str(run_dir),
            *extra_args,
        ],
        capture_output=True, text=True,
        cwd=str(REPO_ROOT), env=_env(),
    )


def _passing_calibration_payload() -> dict:
    return {
        "migration_id": "go_import_rewrite",
        "schema_version": "v1",
        "n_known_good": 10,
        "n_known_bad": 10,
        "notes": "fixture",
        "per_tier": [
            {
                "tier": "diff_valid",
                "tp": 10, "fp": 0, "tn": 10, "fn": 0,
                "n_known_good_observed": 10,
                "n_known_bad_targeted_observed": 10,
                "fpr": 0.0, "fnr": 0.0,
            }
        ],
    }


def test_gate_default_mode_does_not_check_calibration(
    tmp_path: Path,
) -> None:
    """Without --require-calibration, the gate ignores calibration entirely."""
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED, dst=tmp_path / "run_default",
        calibration_payload=None, declare_in_manifest=False,
    )
    proc = _run_gate(staged)
    assert proc.returncode == 0, proc.stderr


def test_gate_require_calibration_passes_when_clean(tmp_path: Path) -> None:
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED, dst=tmp_path / "run_clean",
        calibration_payload=_passing_calibration_payload(),
        declare_in_manifest=True,
    )
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 0, proc.stderr


def test_gate_require_calibration_fails_on_missing_pointer(
    tmp_path: Path,
) -> None:
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED, dst=tmp_path / "run_no_pointer",
        calibration_payload=None, declare_in_manifest=False,
    )
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 1
    assert "missing 'calibration_report'" in proc.stderr


def test_gate_require_calibration_fails_on_missing_file(
    tmp_path: Path,
) -> None:
    """Manifest declares a calibration_report path but the file isn't there."""
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED, dst=tmp_path / "run_missing_file",
        calibration_payload=None, declare_in_manifest=False,
    )
    manifest_path = staged / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["calibration_report"] = str(staged / "calibration.json")
    manifest_path.write_text(json.dumps(manifest))
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 1
    assert "calibration_report file missing" in proc.stderr


def test_gate_require_calibration_fails_on_fpr_breach(
    tmp_path: Path,
) -> None:
    payload = _passing_calibration_payload()
    payload["per_tier"][0]["fpr"] = 0.30  # threshold for diff_valid is 0.05
    payload["per_tier"][0]["fp"] = 3
    payload["per_tier"][0]["tn"] = 7
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED, dst=tmp_path / "run_fpr_breach",
        calibration_payload=payload, declare_in_manifest=True,
    )
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 1
    assert "violates thresholds" in proc.stderr
    assert "diff_valid" in proc.stderr
    assert "max_fpr" in proc.stderr


def test_gate_require_calibration_fails_on_fnr_breach(
    tmp_path: Path,
) -> None:
    payload = _passing_calibration_payload()
    payload["per_tier"][0]["fnr"] = 0.50  # threshold for diff_valid is 0.10
    payload["per_tier"][0]["fn"] = 5
    payload["per_tier"][0]["tp"] = 5
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED, dst=tmp_path / "run_fnr_breach",
        calibration_payload=payload, declare_in_manifest=True,
    )
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 1
    assert "violates thresholds" in proc.stderr
    assert "max_fnr" in proc.stderr


def test_gate_require_calibration_fails_on_null_rate_when_threshold_set(
    tmp_path: Path,
) -> None:
    """A tier whose calibration produced no observations cannot satisfy a
    numeric threshold even though the file is present."""
    payload = _passing_calibration_payload()
    payload["per_tier"][0]["fpr"] = None
    payload["per_tier"][0]["fnr"] = None
    payload["per_tier"][0]["tp"] = 0
    payload["per_tier"][0]["fp"] = 0
    payload["per_tier"][0]["tn"] = 0
    payload["per_tier"][0]["fn"] = 0
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED, dst=tmp_path / "run_null_rates",
        calibration_payload=payload, declare_in_manifest=True,
    )
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 1
    assert "fpr is null" in proc.stderr or "fnr is null" in proc.stderr


def test_gate_require_calibration_fails_on_corrupt_json(
    tmp_path: Path,
) -> None:
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED, dst=tmp_path / "run_corrupt",
        calibration_payload=None, declare_in_manifest=False,
    )
    cal_path = staged / "calibration.json"
    cal_path.write_text("{ this is not valid json")
    manifest_path = staged / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["calibration_report"] = str(cal_path)
    manifest_path.write_text(json.dumps(manifest))
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 1
    assert "cannot load calibration report" in proc.stderr


# ---------------------------------------------------------------------------
# Canonical committed calibration.json sanity check
# ---------------------------------------------------------------------------


def test_committed_calibration_satisfies_thresholds() -> None:
    """The shipped calibration.json must already pass the docs thresholds.

    Without this guard, a published headline run could ship with a
    calibration that subtly fails one threshold and only get caught in CI."""
    cal_path = (
        REPO_ROOT
        / "configs" / "recipes" / "go_import_rewrite.calibration.json"
    )
    assert cal_path.is_file()
    from migration_evals.calibration import (
        load_calibration_thresholds,
        validate_against_thresholds,
    )
    report = CalibrationReport.from_path(cal_path)
    thresholds = load_calibration_thresholds(HYPOTHESES_PATH)
    violations = validate_against_thresholds(report, thresholds)
    assert violations == [], violations
