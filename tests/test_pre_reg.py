"""Tests for pre-registration stamping and the publication gate.

Covers the work unit "pre-registration-and-publication-gate" acceptance
criteria:

- `compute_spec_sha` is stable for the same file bytes and distinguishes
  different bytes.
- `stamp_result` returns a new dict with the three SHA fields populated and
  never mutates the input dict.
- `publication_gate.py --check-run` exits 0 on a stamped fixture run, exits
  1 when a stamp is missing, exits 1 on a stale stamp, and exits 1 when
  `manifest.json` is absent.
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

import pytest  # noqa: E402

from migration_evals.pre_reg import (  # noqa: E402
    compute_spec_sha,
    stamp_result,
)

REPO_ROOT = _REPO_ROOT
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures"
RUN_STAMPED = FIXTURE_ROOT / "run_stamped"
RUN_UNSTAMPED = FIXTURE_ROOT / "run_unstamped"
GATE_SCRIPT = REPO_ROOT / "src" / "migration_evals" / "publication_gate.py"
HYPOTHESES_PATH = REPO_ROOT / "docs" / "hypotheses_and_thresholds.md"


def _run_gate(run_dir: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    # Invoke via -m so Python's import machinery does not put
    # src/migration_evals/ on sys.path[0]; otherwise this package's
    # types.py shadows the stdlib types module and breaks argparse.
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "migration_evals.publication_gate",
            "--check-run",
            str(run_dir),
            *extra_args,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )


def _stage_run(src: Path, dst: Path) -> Path:
    """Copy a fixture run into a tmp dir, absolutising the hypotheses path.

    The committed fixtures use a relative ``../../../../docs/...`` hypotheses
    path so the real run directories resolve correctly inside the repo. When
    we stage a copy under ``tmp_path`` for stale-stamp / missing-manifest
    tests, that relative path no longer lands on the real doc, so we rewrite
    it to an absolute path pointing at the committed file.
    """
    shutil.copytree(src, dst)
    manifest_path = dst / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["hypotheses"] = str(HYPOTHESES_PATH)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return dst


# -- compute_spec_sha ---------------------------------------------------------


def test_compute_spec_sha_stability(tmp_path: Path) -> None:
    target = tmp_path / "spec.yaml"
    target.write_bytes(b"version: 1\npayload: stable\n")
    first = compute_spec_sha(target)
    second = compute_spec_sha(target)
    assert first == second
    assert len(first) == 64
    assert all(c in "0123456789abcdef" for c in first)


def test_compute_spec_sha_distinguishes_files(tmp_path: Path) -> None:
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_bytes(b"alpha")
    b.write_bytes(b"beta")
    assert compute_spec_sha(a) != compute_spec_sha(b)


def test_compute_spec_sha_matches_committed_fixture() -> None:
    # Pinned against the stamped fixture so regressions in the hasher are
    # caught here as well as by the gate itself.
    expected = "79a65900fe2da5252ed173c18c125a06a99b25014e5398b545e167a7354e8b2f"
    assert compute_spec_sha(RUN_STAMPED / "oracle_spec.yaml") == expected


# -- stamp_result -------------------------------------------------------------


def test_stamp_result_populates_fields() -> None:
    original = {
        "task_id": "t-1",
        "agent_model": "claude-sonnet-4-6",
        "migration_id": "java8_17",
        "success": True,
    }
    stamped = stamp_result(
        original,
        oracle_spec=RUN_STAMPED / "oracle_spec.yaml",
        recipe_spec=RUN_STAMPED / "recipe_spec.yaml",
        hypotheses=HYPOTHESES_PATH,
    )
    assert stamped["oracle_spec_sha"] == compute_spec_sha(RUN_STAMPED / "oracle_spec.yaml")
    assert stamped["recipe_spec_sha"] == compute_spec_sha(RUN_STAMPED / "recipe_spec.yaml")
    assert stamped["pre_reg_sha"] == compute_spec_sha(HYPOTHESES_PATH)
    # Existing keys preserved.
    assert stamped["task_id"] == "t-1"
    assert stamped["success"] is True


def test_stamp_result_immutability() -> None:
    original = {"task_id": "t-imm", "success": True}
    before = dict(original)
    stamped = stamp_result(
        original,
        oracle_spec=RUN_STAMPED / "oracle_spec.yaml",
        recipe_spec=RUN_STAMPED / "recipe_spec.yaml",
        hypotheses=HYPOTHESES_PATH,
    )
    # Input dict must be unchanged.
    assert original == before
    assert "oracle_spec_sha" not in original
    assert "recipe_spec_sha" not in original
    assert "pre_reg_sha" not in original
    # Returned dict must be a distinct object.
    assert stamped is not original


def test_stamp_result_deep_copies_nested_structures() -> None:
    original = {"task_id": "t-nested", "extras": {"tool_calls": [1, 2, 3]}}
    stamped = stamp_result(
        original,
        oracle_spec=RUN_STAMPED / "oracle_spec.yaml",
        recipe_spec=RUN_STAMPED / "recipe_spec.yaml",
        hypotheses=HYPOTHESES_PATH,
    )
    stamped["extras"]["tool_calls"].append(4)
    assert original["extras"]["tool_calls"] == [1, 2, 3]


# -- publication gate end-to-end ---------------------------------------------


def test_gate_passes_on_stamped_run() -> None:
    result = _run_gate(RUN_STAMPED)
    assert (
        result.returncode == 0
    ), f"expected pass; stdout={result.stdout!r} stderr={result.stderr!r}"


def test_gate_fails_on_missing_stamp() -> None:
    result = _run_gate(RUN_UNSTAMPED)
    assert result.returncode == 1
    # The fixture leaves at least one stamp field empty; the gate must name it.
    assert "missing stamp" in result.stderr
    assert (
        "'oracle_spec_sha'" in result.stderr
        or "'recipe_spec_sha'" in result.stderr
        or "'pre_reg_sha'" in result.stderr
    )


def test_gate_fails_on_stale_stamp(tmp_path: Path) -> None:
    staged = _stage_run(RUN_STAMPED, tmp_path / "run_stale")
    # Corrupt one stored stamp so it no longer matches the committed file.
    trial_result = staged / "trial_001" / "result.json"
    payload = json.loads(trial_result.read_text())
    payload["oracle_spec_sha"] = "0" * 64
    trial_result.write_text(json.dumps(payload, indent=2))

    result = _run_gate(staged)
    assert result.returncode == 1
    assert "stale stamp" in result.stderr
    assert "'oracle_spec_sha'" in result.stderr


def test_gate_fails_on_missing_manifest(tmp_path: Path) -> None:
    staged = _stage_run(RUN_STAMPED, tmp_path / "run_no_manifest")
    (staged / "manifest.json").unlink()

    result = _run_gate(staged)
    assert result.returncode == 1
    assert "manifest.json" in result.stderr


def test_gate_fails_on_nonexistent_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    result = _run_gate(missing)
    assert result.returncode == 1
    assert "does not exist" in result.stderr


@pytest.mark.parametrize("field", ["oracle_spec_sha", "recipe_spec_sha", "pre_reg_sha"])
def test_gate_names_each_stale_stamp_field(tmp_path: Path, field: str) -> None:
    staged = _stage_run(RUN_STAMPED, tmp_path / f"run_stale_{field}")
    trial_result = staged / "trial_001" / "result.json"
    payload = json.loads(trial_result.read_text())
    payload[field] = "f" * 64
    trial_result.write_text(json.dumps(payload, indent=2))

    result = _run_gate(staged)
    assert result.returncode == 1
    assert f"'{field}'" in result.stderr


# -- gold-anchor correlation extension ---------------------------------------


def _write_summary(run_dir: Path, gold_anchor: dict | None) -> None:
    summary = {"gold_anchor_correlation": gold_anchor}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))


def _healthy_correlation() -> dict:
    return {
        "point": 0.82,
        "ci_low": 0.61,
        "ci_high": 0.94,
        "eval_broken": False,
    }


def test_gate_passes_when_summary_absent_default_mode(tmp_path: Path) -> None:
    """Back-compat: no summary.json -> gate keeps prior pass behaviour."""
    staged = _stage_run(RUN_STAMPED, tmp_path / "run_no_summary")
    result = _run_gate(staged)
    assert result.returncode == 0, result.stderr


def test_gate_passes_with_healthy_gold_anchor(tmp_path: Path) -> None:
    staged = _stage_run(RUN_STAMPED, tmp_path / "run_healthy_gold")
    _write_summary(staged, _healthy_correlation())
    result = _run_gate(staged)
    assert result.returncode == 0, result.stderr


def test_gate_fails_when_eval_broken_true(tmp_path: Path) -> None:
    staged = _stage_run(RUN_STAMPED, tmp_path / "run_eval_broken")
    broken = _healthy_correlation()
    broken["eval_broken"] = True
    _write_summary(staged, broken)
    result = _run_gate(staged)
    assert result.returncode == 1
    assert "eval_broken=true" in result.stderr


def test_gate_fails_on_missing_gold_anchor_section(tmp_path: Path) -> None:
    """summary.json present but gold_anchor_correlation is null -> fail."""
    staged = _stage_run(RUN_STAMPED, tmp_path / "run_null_gold")
    _write_summary(staged, None)
    result = _run_gate(staged)
    assert result.returncode == 1
    assert "missing gold_anchor_correlation" in result.stderr


def test_gate_fails_on_missing_gold_anchor_fields(tmp_path: Path) -> None:
    staged = _stage_run(RUN_STAMPED, tmp_path / "run_partial_gold")
    partial = {"point": 0.9}  # missing ci_low, ci_high, eval_broken
    _write_summary(staged, partial)
    result = _run_gate(staged)
    assert result.returncode == 1
    assert "missing gold_anchor_correlation" in result.stderr


def test_gate_require_gold_anchor_fails_when_summary_absent(
    tmp_path: Path,
) -> None:
    staged = _stage_run(RUN_STAMPED, tmp_path / "run_require_no_summary")
    result = _run_gate(staged, "--require-gold-anchor")
    assert result.returncode == 1
    assert "summary.json missing" in result.stderr


def test_gate_require_gold_anchor_passes_when_healthy(tmp_path: Path) -> None:
    staged = _stage_run(RUN_STAMPED, tmp_path / "run_require_healthy")
    _write_summary(staged, _healthy_correlation())
    result = _run_gate(staged, "--require-gold-anchor")
    assert result.returncode == 0, result.stderr


def test_gate_require_gold_anchor_fails_on_eval_broken(tmp_path: Path) -> None:
    staged = _stage_run(RUN_STAMPED, tmp_path / "run_require_broken")
    broken = _healthy_correlation()
    broken["eval_broken"] = True
    _write_summary(staged, broken)
    result = _run_gate(staged, "--require-gold-anchor")
    assert result.returncode == 1
    assert "eval_broken=true" in result.stderr
