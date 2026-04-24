"""Unit tests for the optional prompt_sha pre-registration stamp.

prompt_sha is the 4th stamp (alongside oracle_spec_sha / recipe_spec_sha /
pre_reg_sha). It is enforced by the publication gate ONLY when manifest.json
declares a 'prompt_spec' key — keeping all existing recipe-driven runs
backward-compatible.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from migration_evals.pre_reg import compute_spec_sha, stamp_result  # noqa: E402

GATE_SCRIPT = _REPO_ROOT / "src" / "migration_evals" / "publication_gate.py"


# -- stamp_result(prompt_spec=...) -----------------------------------------


def _make_specs(tmp_path: Path) -> tuple[Path, Path, Path]:
    oracle = tmp_path / "oracle.yaml"
    oracle.write_text("oracle_tier: compile_only\n")
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("build_cmd: mvn compile\n")
    hyp = tmp_path / "hypotheses.md"
    hyp.write_text("# H1: things work\n")
    return oracle, recipe, hyp


def test_stamp_result_omits_prompt_sha_when_prompt_spec_absent(tmp_path: Path) -> None:
    oracle, recipe, hyp = _make_specs(tmp_path)
    payload = {"task_id": "t1", "success": True}
    stamped = stamp_result(payload, oracle, recipe, hyp)
    assert "prompt_sha" not in stamped
    assert "oracle_spec_sha" in stamped


def test_stamp_result_includes_prompt_sha_when_prompt_spec_supplied(tmp_path: Path) -> None:
    oracle, recipe, hyp = _make_specs(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Migrate Java 8 to 17. Preserve semantics.\n")
    payload = {"task_id": "t1", "success": True}
    stamped = stamp_result(payload, oracle, recipe, hyp, prompt_spec=prompt)
    assert stamped["prompt_sha"] == compute_spec_sha(prompt)
    expected = hashlib.sha256(prompt.read_bytes()).hexdigest()
    assert stamped["prompt_sha"] == expected


def test_stamp_result_does_not_mutate_input(tmp_path: Path) -> None:
    oracle, recipe, hyp = _make_specs(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi")
    payload = {"task_id": "t1", "success": True}
    stamped = stamp_result(payload, oracle, recipe, hyp, prompt_spec=prompt)
    assert payload == {"task_id": "t1", "success": True}
    assert stamped is not payload


# -- publication gate enforcement -------------------------------------------


def _stage_minimal_run(tmp_path: Path, *, with_prompt_spec: bool) -> Path:
    """Build a tiny self-contained run dir that the gate can verify."""
    oracle = tmp_path / "oracle.yaml"
    oracle.write_text("oracle_tier: compile_only\n")
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("build_cmd: echo build\n")
    hyp = tmp_path / "hypotheses.md"
    hyp.write_text("# H1: things work\n")
    manifest = {
        "oracle_spec": str(oracle),
        "recipe_spec": str(recipe),
        "hypotheses": str(hyp),
    }
    prompt = None
    if with_prompt_spec:
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Migrate Java 8 to 17.\n")
        manifest["prompt_spec"] = str(prompt)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    trial_dir = run_dir / "trial_001"
    trial_dir.mkdir()
    payload = {
        "task_id": "t1",
        "agent_model": "claude-sonnet-4-6",
        "migration_id": "java8_17",
        "success": True,
        "failure_class": None,
        "oracle_tier": "compile_only",
        "score_pre_cutoff": 1.0,
        "score_post_cutoff": 1.0,
        "oracle_spec_sha": compute_spec_sha(oracle),
        "recipe_spec_sha": compute_spec_sha(recipe),
        "pre_reg_sha": compute_spec_sha(hyp),
    }
    if prompt is not None:
        payload["prompt_sha"] = compute_spec_sha(prompt)
    (trial_dir / "result.json").write_text(json.dumps(payload))
    return run_dir


def _run_gate(run_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE_SCRIPT), "--check-run", str(run_dir)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
    )


def test_gate_passes_without_prompt_spec(tmp_path: Path) -> None:
    """Backward-compat: existing 3-stamp runs still pass."""
    run_dir = _stage_minimal_run(tmp_path, with_prompt_spec=False)
    proc = _run_gate(run_dir)
    assert proc.returncode == 0, proc.stderr


def test_gate_passes_with_valid_prompt_sha(tmp_path: Path) -> None:
    run_dir = _stage_minimal_run(tmp_path, with_prompt_spec=True)
    proc = _run_gate(run_dir)
    assert proc.returncode == 0, proc.stderr


def test_gate_fails_when_prompt_sha_missing(tmp_path: Path) -> None:
    """When manifest declares prompt_spec, every result.json must carry prompt_sha."""
    run_dir = _stage_minimal_run(tmp_path, with_prompt_spec=True)
    # Strip the prompt_sha from the trial.
    trial_path = run_dir / "trial_001" / "result.json"
    payload = json.loads(trial_path.read_text())
    del payload["prompt_sha"]
    trial_path.write_text(json.dumps(payload))
    proc = _run_gate(run_dir)
    assert proc.returncode == 1
    assert "prompt_sha" in proc.stderr


def test_gate_fails_when_prompt_sha_stale(tmp_path: Path) -> None:
    run_dir = _stage_minimal_run(tmp_path, with_prompt_spec=True)
    trial_path = run_dir / "trial_001" / "result.json"
    payload = json.loads(trial_path.read_text())
    payload["prompt_sha"] = "0" * 64  # stale
    trial_path.write_text(json.dumps(payload))
    proc = _run_gate(run_dir)
    assert proc.returncode == 1
    assert "prompt_sha" in proc.stderr
    assert "stale" in proc.stderr.lower()
