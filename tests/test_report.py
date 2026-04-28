"""Tests for migration_evals.report (funnel report generator)."""

from __future__ import annotations

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
    _cost_aggregate,
    _efficiency_aggregate,
    _funnel_counts,
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
        {"path": str(REPO_ROOT / entry["path"]), "seed": entry["seed"]} for entry in raw["repos"]
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
# hd9: Wilson + bootstrap CIs, cost normalisation, iteration efficiency
# ---------------------------------------------------------------------------


def _verdict(tier: str, *, passed: bool) -> dict:
    return {"tier": tier, "passed": passed, "cost_usd": 0.0}


def _trial(
    *,
    success: bool,
    tiers: list[tuple[str, bool]],
    cost_usd: float | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    iterator_id: str | None = None,
    usage_per_tier: list[dict] | None = None,
) -> dict:
    """Synthesize a result.json-shaped trial for aggregation tests."""
    verdicts = [_verdict(t, passed=p) for t, p in tiers]
    if usage_per_tier:
        for v, usage in zip(verdicts, usage_per_tier):
            v["details"] = {"usage": usage}
    funnel: dict = {"per_tier_verdict": verdicts}
    if cost_usd is not None:
        funnel["total_cost_usd"] = cost_usd
    return {
        "task_id": "x",
        "agent_model": "m",
        "migration_id": "mig",
        "success": success,
        "failure_class": None if success else "agent_error",
        "oracle_tier": tiers[-1][0] if tiers else "compile_only",
        "started_at": started_at,
        "finished_at": finished_at,
        "iterator_id": iterator_id,
        "funnel": funnel,
    }


def test_funnel_counts_emits_wilson_and_bootstrap_cis() -> None:
    # 4 trials all reach compile_only; 3 pass, 1 fails (so 3 pass, 1 fails
    # at compile and the failer never enters tests).
    results = [
        _trial(success=True, tiers=[("compile_only", True), ("tests", True)]),
        _trial(success=True, tiers=[("compile_only", True), ("tests", True)]),
        _trial(success=True, tiers=[("compile_only", True), ("tests", True)]),
        _trial(success=False, tiers=[("compile_only", False)]),
    ]
    rows = _funnel_counts(results, n_bootstrap=200, bootstrap_seed=7)
    by_tier = {r["tier_name"]: r for r in rows}

    compile_row = by_tier["compile_only"]
    assert compile_row["n_entered"] == 4
    assert compile_row["n_passed"] == 3
    # Wilson CI strictly bounded in [0,1] and brackets 0.75
    assert 0.0 < compile_row["rate_ci_low"] < 0.75
    assert 0.75 < compile_row["rate_ci_high"] < 1.0
    # Cumulative bootstrap CI: 3/4 = 0.75 average, CI keeps within [0,1]
    assert 0.0 <= compile_row["cumulative_ci_low"] <= 0.75
    assert 0.75 <= compile_row["cumulative_ci_high"] <= 1.0

    # Tiers nobody entered: rate CI is (0,0); cumulative CI is also (0,0)
    daikon_row = by_tier["daikon"]
    assert daikon_row["n_entered"] == 0
    assert daikon_row["rate_ci_low"] == 0.0
    assert daikon_row["rate_ci_high"] == 0.0
    assert daikon_row["cumulative_ci_low"] == 0.0
    assert daikon_row["cumulative_ci_high"] == 0.0


def test_cost_aggregate_dollars_per_success_and_latency() -> None:
    results = [
        _trial(
            success=True,
            tiers=[("compile_only", True)],
            cost_usd=0.12,
            started_at="2026-04-26T12:00:00Z",
            finished_at="2026-04-26T12:00:30Z",
        ),
        _trial(
            success=False,
            tiers=[("compile_only", False)],
            cost_usd=0.08,
            started_at="2026-04-26T12:01:00Z",
            finished_at="2026-04-26T12:02:00Z",
        ),
        _trial(
            success=True,
            tiers=[("compile_only", True)],
            cost_usd=0.20,
            started_at="2026-04-26T12:03:00Z",
            finished_at="2026-04-26T12:03:10Z",
        ),
    ]
    cost = _cost_aggregate(results)
    assert cost["n_total"] == 3
    assert cost["n_success"] == 2
    assert cost["total_cost_usd"] == pytest.approx(0.40, abs=1e-6)
    # Spent $0.40 on 2 successes -> $0.20 each
    assert cost["dollars_per_success"] == pytest.approx(0.20, abs=1e-6)
    # Latencies: 30s, 60s, 10s -> sorted [10, 30, 60]; p50 = 30, p95 ≈ 57
    assert cost["p50_latency_s"] == pytest.approx(30.0, abs=0.01)
    assert cost["p95_latency_s"] == pytest.approx(57.0, abs=1.0)
    # No usage payloads -> tokens absent
    assert cost["total_tokens"] is None
    assert cost["tokens_per_success"] is None


def test_cost_aggregate_handles_missing_cost_data() -> None:
    results = [
        _trial(success=True, tiers=[("compile_only", True)]),
        _trial(success=False, tiers=[("compile_only", False)]),
    ]
    cost = _cost_aggregate(results)
    assert cost["n_total"] == 2
    assert cost["n_success"] == 1
    assert cost["total_cost_usd"] is None
    assert cost["dollars_per_success"] is None
    assert cost["p50_latency_s"] is None
    assert cost["p95_latency_s"] is None


def test_cost_aggregate_sums_token_usage_across_verdicts() -> None:
    usage_a = [
        {"input_tokens": 100, "output_tokens": 50},
        {"input_tokens": 200, "output_tokens": 75},
    ]
    usage_b = [{"input_tokens": 80, "output_tokens": 20}]
    results = [
        _trial(
            success=True,
            tiers=[("compile_only", True), ("tests", True)],
            cost_usd=0.10,
            usage_per_tier=usage_a,
        ),
        _trial(
            success=True,
            tiers=[("compile_only", True)],
            cost_usd=0.05,
            usage_per_tier=usage_b,
        ),
    ]
    cost = _cost_aggregate(results)
    # 100+50+200+75 + 80+20 = 525
    assert cost["total_tokens"] == 525
    # 2 successes -> 262.5 tokens each
    assert cost["tokens_per_success"] == pytest.approx(262.5, abs=1e-3)


def test_efficiency_aggregate_returns_none_when_no_iterator_ids() -> None:
    results = [
        _trial(success=True, tiers=[("compile_only", True)]),
        _trial(success=False, tiers=[("compile_only", False)]),
    ]
    assert _efficiency_aggregate(results) is None


def test_efficiency_aggregate_overall_and_per_iterator() -> None:
    results = [
        _trial(success=True, tiers=[("compile_only", True)], iterator_id="batch-A"),
        _trial(success=False, tiers=[("compile_only", False)], iterator_id="batch-A"),
        _trial(success=False, tiers=[("compile_only", False)], iterator_id="batch-A"),
        _trial(success=True, tiers=[("compile_only", True)], iterator_id="batch-B"),
        _trial(success=True, tiers=[("compile_only", True)], iterator_id="batch-B"),
    ]
    eff = _efficiency_aggregate(results)
    assert eff is not None
    assert eff["n_total"] == 5
    assert eff["n_success"] == 3
    assert eff["tries_per_success"] == pytest.approx(5 / 3, abs=1e-4)
    by_iter = {p["iterator_id"]: p for p in eff["per_iterator"]}
    assert by_iter["batch-A"]["n_total"] == 3
    assert by_iter["batch-A"]["n_success"] == 1
    assert by_iter["batch-A"]["tries_per_success"] == pytest.approx(3.0)
    assert by_iter["batch-B"]["n_total"] == 2
    assert by_iter["batch-B"]["n_success"] == 2
    assert by_iter["batch-B"]["tries_per_success"] == pytest.approx(1.0)


def test_efficiency_handles_zero_successes_in_an_iterator() -> None:
    results = [
        _trial(success=False, tiers=[("compile_only", False)], iterator_id="batch-X"),
        _trial(success=False, tiers=[("compile_only", False)], iterator_id="batch-X"),
    ]
    eff = _efficiency_aggregate(results)
    assert eff is not None
    assert eff["tries_per_success"] is None
    assert eff["per_iterator"][0]["tries_per_success"] is None


def test_format_report_includes_cost_and_efficiency_sections() -> None:
    data = build_report_data_dict(
        funnel_rows=[
            {
                "tier_name": "compile_only",
                "n_entered": 2,
                "n_passed": 1,
                "n_failed": 1,
                "cumulative_pass_rate": 0.5,
                "rate_ci_low": 0.1,
                "rate_ci_high": 0.9,
                "cumulative_ci_low": 0.1,
                "cumulative_ci_high": 0.9,
            }
        ],
        cost={
            "n_total": 2,
            "n_success": 1,
            "total_cost_usd": 0.30,
            "dollars_per_success": 0.30,
            "p50_latency_s": 12.5,
            "p95_latency_s": 30.0,
            "total_tokens": None,
            "tokens_per_success": None,
        },
        efficiency={
            "n_total": 2,
            "n_success": 1,
            "tries_per_success": 2.0,
            "per_iterator": [
                {
                    "iterator_id": "batch-A",
                    "n_total": 2,
                    "n_success": 1,
                    "tries_per_success": 2.0,
                }
            ],
        },
    )
    md = format_report(data)
    assert "Cost" in md
    assert "dollars_per_success" in md
    assert "p50_latency_s" in md
    assert "Iteration efficiency" in md
    assert "tries_per_success" in md
    assert "batch-A" in md
    # rate_ci appears in funnel table
    assert "[0.100, 0.900]" in md


def build_report_data_dict(*, funnel_rows, cost, efficiency) -> dict:
    """Minimal data-dict factory for format_report tests."""
    return {
        "summary": {
            "migration_id": "mig",
            "agent_model": "m",
            "variant": "smoke",
            "n_trials": 2,
        },
        "n_trials": 2,
        "funnel": funnel_rows,
        "contamination": {
            "score_pre": 0.0,
            "score_post": 0.0,
            "gap_pp": 0.0,
            "warning_flag": False,
            "n_pre": 0,
            "n_post": 0,
        },
        "gold_anchor": None,
        "stamps": {
            "oracle_spec_sha": "a",
            "recipe_spec_sha": "b",
            "pre_reg_sha": "c",
        },
        "failure_classes": {},
        "quality": [],
        "cost": cost,
        "efficiency": efficiency,
    }


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
