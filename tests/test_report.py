"""Tests for migration_evals.report (funnel report generator)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from migration_evals.report import (  # noqa: E402
    build_report_data,
    format_report,
    generate_report,
)
from migration_evals.runner import run_from_config  # noqa: E402

REPO_ROOT = _REPO_ROOT
SMOKE_CONFIG = REPO_ROOT / "configs" / "java8_17_smoke.yaml"


def _smoke_config(tmp_path: Path, output_root: Path) -> Path:
    raw = yaml.safe_load(SMOKE_CONFIG.read_text())
    raw["output_root"] = str(output_root)
    raw["repos"] = [
        {"path": str(REPO_ROOT / entry["path"]), "seed": entry["seed"]}
        for entry in raw["repos"]
    ]
    for key in ("anthropic_cassette_dir", "sandbox_cassette_dir"):
        raw["adapters"][key] = str(REPO_ROOT / raw["adapters"][key])
    for key in ("oracle_spec", "recipe_spec", "hypotheses"):
        raw["stamps"][key] = str(REPO_ROOT / raw["stamps"][key])
    cfg_path = tmp_path / "smoke.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return cfg_path


# ---------------------------------------------------------------------------
# format_report - direct rendering
# ---------------------------------------------------------------------------


def test_format_report_emits_all_required_sections() -> None:
    data = {
        "summary": {
            "migration_id": "java8_17",
            "agent_model": "claude-sonnet-4-6",
            "variant": "smoke",
            "n_trials": 3,
        },
        "n_trials": 3,
        "funnel": [
            {
                "tier_name": "compile_only",
                "n_entered": 3,
                "n_passed": 3,
                "n_failed": 0,
                "cumulative_pass_rate": 1.0,
            },
            {
                "tier_name": "tests",
                "n_entered": 3,
                "n_passed": 2,
                "n_failed": 1,
                "cumulative_pass_rate": 0.6667,
            },
            {
                "tier_name": "ast_conformance",
                "n_entered": 0,
                "n_passed": 0,
                "n_failed": 0,
                "cumulative_pass_rate": 0.0,
            },
            {
                "tier_name": "judge",
                "n_entered": 2,
                "n_passed": 2,
                "n_failed": 0,
                "cumulative_pass_rate": 0.6667,
            },
            {
                "tier_name": "daikon",
                "n_entered": 0,
                "n_passed": 0,
                "n_failed": 0,
                "cumulative_pass_rate": 0.0,
            },
        ],
        "contamination": {
            "score_pre": 0.8,
            "score_post": 0.6,
            "gap_pp": 20.0,
            "warning_flag": True,
            "n_pre": 5,
            "n_post": 5,
        },
        "gold_anchor": None,
        "stamps": {
            "oracle_spec_sha": "abc123",
            "recipe_spec_sha": "def456",
            "pre_reg_sha": "ghi789",
        },
        "failure_classes": {"agent_error": 1},
    }
    md = format_report(data)

    # Required headers and fields.
    assert "# Migration Eval Funnel Report" in md
    assert "## 1. Funnel" in md
    assert "## 2. Contamination Split" in md
    assert "Spec Stamps" in md  # numbered 3 or 4 depending on gold
    assert "Failure Class Breakdown" in md
    assert "compile_only" in md
    assert "abc123" in md
    assert "agent_error" in md

    # Gold section absent when data["gold_anchor"] is None.
    assert "## 3. Gold Anchor Correlation" not in md


# ---------------------------------------------------------------------------
# End-to-end - run smoke, then generate report
# ---------------------------------------------------------------------------


def test_generate_report_smoke_end_to_end(tmp_path: Path) -> None:
    output_root = tmp_path / "smoke_out"
    cfg_path = _smoke_config(tmp_path, output_root)
    assert run_from_config(cfg_path) == 0

    report_path = tmp_path / "report.md"
    rc = generate_report(output_root, report_path)
    assert rc == 0
    assert report_path.is_file()

    body = report_path.read_text()
    # Funnel header + all expected tiers listed (ast/daikon appear as
    # zero-entered rows, keeping the report shape constant).
    assert "| tier_name |" in body
    assert "compile_only" in body
    assert "tests" in body
    assert "judge" in body
    assert "ast_conformance" in body
    assert "daikon" in body
    # Contamination section contains the score_pre_cutoff + gap_pp fields.
    assert "score_pre_cutoff" in body
    assert "gap_pp" in body
    # Stamps are populated from summary.json (non-empty SHA values).
    assert "oracle_spec_sha" in body
    assert "pre_reg_sha" in body


def test_build_report_data_skips_gold_when_absent(tmp_path: Path) -> None:
    output_root = tmp_path / "smoke_out"
    cfg_path = _smoke_config(tmp_path, output_root)
    assert run_from_config(cfg_path) == 0

    data = build_report_data(output_root)
    assert data["gold_anchor"] is None
    assert data["n_trials"] == 3
    assert len(data["funnel"]) == 5


def test_build_report_data_respects_cutoff_override(tmp_path: Path) -> None:
    output_root = tmp_path / "smoke_out"
    cfg_path = _smoke_config(tmp_path, output_root)
    assert run_from_config(cfg_path) == 0

    # Cutoff far in the future -> every repo is pre-cutoff.
    data = build_report_data(
        output_root,
        model_cutoff_date=date(2099, 1, 1),
    )
    contam = data["contamination"]
    assert contam["n_pre"] == 3
    assert contam["n_post"] == 0


# ---------------------------------------------------------------------------
# CLI invocation - AC#3
# ---------------------------------------------------------------------------


def test_cli_report_subcommand_smoke(tmp_path: Path) -> None:
    output_root = tmp_path / "smoke_out"
    cfg_path = _smoke_config(tmp_path, output_root)
    assert run_from_config(cfg_path) == 0

    report_path = tmp_path / "report.md"

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "migration_evals.cli",
            "report",
            "--run",
            str(output_root),
            "--out",
            str(report_path),
        ],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert report_path.is_file()
    body = report_path.read_text()
    assert "# Migration Eval Funnel Report" in body


# ---------------------------------------------------------------------------
# Regression subcommand still works on a smoke ledger (AC#6)
# ---------------------------------------------------------------------------


def test_cli_regression_on_smoke_ledger(tmp_path: Path) -> None:
    # Build ledger_v1 (the committed fixture) against a synthesized v2 that
    # flips task_b and task_c to failures. This reuses the committed
    # fixtures rather than introducing a new set.
    ledger_v1 = REPO_ROOT / "tests/fixtures/ledger_v1"
    ledger_v2 = REPO_ROOT / "tests/fixtures/ledger_v2"
    report_path = tmp_path / "regressions.md"

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "migration_evals.cli",
            "regression",
            "--from",
            str(ledger_v1),
            "--to",
            str(ledger_v2),
            "--out",
            str(report_path),
        ],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    body = report_path.read_text()
    assert "Regression Report" in body
