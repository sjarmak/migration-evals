"""Tests for the config-driven runner + CLI smoke path."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft7Validator

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from migration_evals.runner import run_from_config  # noqa: E402

REPO_ROOT = _REPO_ROOT
SMOKE_CONFIG = REPO_ROOT / "configs" / "java8_17_smoke.yaml"
SCHEMA_PATH = REPO_ROOT / "schemas" / "mig_result.schema.json"


def _validator() -> Draft7Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    return Draft7Validator(schema)


def _smoke_config(tmp_path: Path, output_root: Path) -> Path:
    """Return a tmp-path copy of the smoke config pointed at ``output_root``.

    We rewrite ``output_root`` so tests don't pollute the committed
    ``runs/analysis/`` tree.
    """
    raw = yaml.safe_load(SMOKE_CONFIG.read_text())
    raw["output_root"] = str(output_root)
    # Resolve repo paths against REPO_ROOT so tests can run from any cwd.
    raw["repos"] = [
        {"path": str(REPO_ROOT / entry["path"]), "seed": entry["seed"]}
        for entry in raw["repos"]
    ]
    # Same for cassette + stamp paths.
    for key in ("anthropic_cassette_dir", "sandbox_cassette_dir"):
        if key in raw["adapters"]:
            raw["adapters"][key] = str(REPO_ROOT / raw["adapters"][key])
    for key in ("oracle_spec", "recipe_spec", "hypotheses"):
        raw["stamps"][key] = str(REPO_ROOT / raw["stamps"][key])

    cfg_path = tmp_path / "smoke.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return cfg_path


# ---------------------------------------------------------------------------
# Config is committed and parseable
# ---------------------------------------------------------------------------


def test_smoke_config_exists_and_parses() -> None:
    assert SMOKE_CONFIG.is_file(), f"smoke config missing at {SMOKE_CONFIG}"
    raw = yaml.safe_load(SMOKE_CONFIG.read_text())
    assert raw["migration_id"] == "java8_17"
    assert raw["agent_model"] == "claude-sonnet-4-6"
    assert raw["variant"] == "smoke"
    assert len(raw["repos"]) == 3


# ---------------------------------------------------------------------------
# Programmatic runner
# ---------------------------------------------------------------------------


def test_run_from_config_writes_three_valid_results(tmp_path: Path) -> None:
    output_root = tmp_path / "smoke_out"
    cfg_path = _smoke_config(tmp_path, output_root)

    rc = run_from_config(cfg_path)
    assert rc == 0

    trial_dirs = sorted(p for p in output_root.iterdir() if p.is_dir())
    assert [d.name for d in trial_dirs] == ["repo01_1", "repo02_2", "repo03_3"]

    validator = _validator()
    for trial in trial_dirs:
        payload = json.loads((trial / "result.json").read_text())
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
        assert not errors, (
            f"{trial}/result.json fails schema: {[e.message for e in errors]}"
        )
        assert payload["migration_id"] == "java8_17"
        assert payload["agent_model"] == "claude-sonnet-4-6"
        assert payload["task_id"].startswith("java8_17::")
        # Stamps are populated (non-empty) and deterministic.
        assert payload["oracle_spec_sha"]
        assert payload["recipe_spec_sha"]
        assert payload["pre_reg_sha"]

    summary = json.loads((output_root / "summary.json").read_text())
    assert summary["n_trials"] == 3
    assert summary["migration_id"] == "java8_17"


def test_run_from_config_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    rc = run_from_config(missing)
    assert rc == 2


def test_run_from_config_invalid_mapping(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- not a mapping\n")
    rc = run_from_config(bad)
    assert rc == 2


def test_run_from_config_missing_required_key(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"migration_id": "x"}))
    rc = run_from_config(bad)
    assert rc == 2


# ---------------------------------------------------------------------------
# CLI smoke - AC#1 + AC#2 (emits 3 schema-valid result.json in < 2 minutes)
# ---------------------------------------------------------------------------


def test_cli_run_config_smoke_under_two_minutes(tmp_path: Path) -> None:
    output_root = tmp_path / "smoke_cli_out"
    cfg_path = _smoke_config(tmp_path, output_root)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    start = time.perf_counter()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "migration_evals.cli",
            "run",
            "--config",
            str(cfg_path),
        ],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed = time.perf_counter() - start

    assert proc.returncode == 0, (
        f"CLI failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert elapsed < 120, f"smoke took {elapsed:.1f}s; AC caps at 120s"

    validator = _validator()
    trial_dirs = sorted(p for p in output_root.iterdir() if p.is_dir())
    assert len(trial_dirs) == 3
    for trial in trial_dirs:
        payload = json.loads((trial / "result.json").read_text())
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
        assert not errors


# ---------------------------------------------------------------------------
# Harness + probe subcommand wiring (AC#8 - all subcommands exist)
# ---------------------------------------------------------------------------


def test_cli_harness_validate_smoke() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "migration_evals.cli",
            "harness",
            "validate",
            "--repo",
            str(REPO_ROOT / "tests/fixtures/funnel_repos/repo01"),
        ],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
