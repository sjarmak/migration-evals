"""Funnel orchestration + CLI integration tests (PRD M1, AC#7).

Covers cascade, short-circuit semantics, AST tier injection for synthetic
repos, Daikon stub skip, and the end-to-end CLI run that emits 10 valid
result.json files in <3 minutes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402
from jsonschema import Draft7Validator  # noqa: E402

from migration_evals.funnel import run_funnel  # noqa: E402
from migration_evals.harness.recipe import Recipe  # noqa: E402
from migration_evals.oracles.verdict import (  # noqa: E402
    FunnelResult,
    OracleVerdict,
)

REPO_ROOT = _REPO_ROOT
FIXTURE_REPOS = REPO_ROOT / "tests" / "fixtures" / "funnel_repos"
JUDGE_CASSETTE_DIR = REPO_ROOT / "tests" / "fixtures" / "judge_cassettes"
SCHEMA_PATH = REPO_ROOT / "schemas" / "mig_result.schema.json"


def _make_recipe() -> Recipe:
    return Recipe(
        dockerfile="FROM maven:3.9-eclipse-temurin-17\n",
        build_cmd="mvn -B -e compile",
        test_cmd="mvn -B -e test",
        harness_provenance={
            "model": "claude-haiku-4-5",
            "prompt_version": "v1",
            "timestamp": "2026-04-24T00:00:00Z",
        },
    )


class StubDaytona:
    """Records every command and returns a fixed exit envelope per command."""

    def __init__(self, exit_for_cmd: Mapping[str, int] | None = None) -> None:
        self._exit_for_cmd = dict(exit_for_cmd or {})
        self.calls: list[str] = []

    def create_sandbox(self, *, image, env=None, cassette=None):
        return "sandbox-1"

    def exec(self, sandbox_id, *, command, timeout_s=600, cassette=None):
        self.calls.append(command)
        return {
            "exit_code": self._exit_for_cmd.get(command, 0),
            "stdout": "",
            "stderr": "",
        }

    def destroy_sandbox(self, sandbox_id):
        return None


class StubAnthropic:
    def __init__(self, text: str = "PASS judge default") -> None:
        self._text = text
        self.last_request: dict[str, Any] = {}
        self.call_count = 0

    def messages_create(self, *, model, messages, system=None, max_tokens=1024, cassette=None, **kwargs):
        self.call_count += 1
        self.last_request = {"model": model, "messages": list(messages), "system": system, "max_tokens": max_tokens}
        return {"content": [{"type": "text", "text": self._text}]}


# -- cascade behavior --------------------------------------------------------


def test_funnel_cascade_all_pass() -> None:
    recipe = _make_recipe()
    adapters = {
        "daytona": StubDaytona(),
        "anthropic": StubAnthropic("PASS"),
        "enable_daikon": False,
    }
    result = run_funnel(FIXTURE_REPOS / "repo01", recipe, adapters)
    assert isinstance(result, FunnelResult)
    tier_names = [name for name, _ in result.per_tier_verdict]
    # AST not injected (is_synthetic=False), Daikon disabled.
    assert tier_names == ["compile_only", "tests", "judge"]
    assert result.final_verdict.tier == "judge"
    assert result.final_verdict.passed is True
    assert result.failure_class is None
    assert result.total_cost_usd == pytest.approx(0.01 + 0.03 + 0.08)


def test_funnel_short_circuits_on_t1_failure() -> None:
    recipe = _make_recipe()
    daytona = StubDaytona({recipe.build_cmd: 1})
    anthropic = StubAnthropic()
    adapters = {"daytona": daytona, "anthropic": anthropic, "enable_daikon": False}
    result = run_funnel(FIXTURE_REPOS / "repo01", recipe, adapters)

    tier_names = [name for name, _ in result.per_tier_verdict]
    assert tier_names == ["compile_only"]
    assert result.final_verdict.passed is False
    assert result.failure_class == "harness_error"
    assert anthropic.call_count == 0  # judge never reached
    assert recipe.test_cmd not in daytona.calls


def test_funnel_short_circuits_on_t2_failure_classifies_agent_error() -> None:
    recipe = _make_recipe()
    daytona = StubDaytona({recipe.test_cmd: 1})
    adapters = {
        "daytona": daytona,
        "anthropic": StubAnthropic(),
        "enable_daikon": False,
    }
    result = run_funnel(FIXTURE_REPOS / "repo01", recipe, adapters)
    tier_names = [name for name, _ in result.per_tier_verdict]
    assert tier_names == ["compile_only", "tests"]
    assert result.failure_class == "agent_error"


def test_funnel_synthetic_runs_ast_tier(tmp_path: Path) -> None:
    """When is_synthetic=True the AST tier is interleaved between T2 and T3."""
    repo = tmp_path / "synthetic-repo"
    repo.mkdir()
    (repo / "orig").mkdir()
    (repo / "migrated").mkdir()
    (repo / "orig" / "Trivial.java").write_text("class Trivial {}\n")
    (repo / "migrated" / "Trivial.java").write_text("class Trivial {}\n")

    recipe = _make_recipe()
    adapters = {
        "daytona": StubDaytona(),
        "anthropic": StubAnthropic("PASS"),
        "enable_daikon": False,
    }
    result = run_funnel(repo, recipe, adapters, is_synthetic=True)
    tier_names = [name for name, _ in result.per_tier_verdict]
    assert tier_names == ["compile_only", "tests", "ast_conformance", "judge"]


def test_funnel_skips_daikon_by_default() -> None:
    recipe = _make_recipe()
    adapters = {
        "daytona": StubDaytona(),
        "anthropic": StubAnthropic("PASS"),
        "enable_daikon": False,
    }
    result = run_funnel(FIXTURE_REPOS / "repo01", recipe, adapters)
    tier_names = [name for name, _ in result.per_tier_verdict]
    assert "daikon" not in tier_names


def test_funnel_with_daikon_enabled_skips_stub_gracefully() -> None:
    """Even when enable_daikon=True the stub raises NotImplementedError; cascade survives."""
    recipe = _make_recipe()
    adapters = {
        "daytona": StubDaytona(),
        "anthropic": StubAnthropic("PASS"),
        "enable_daikon": True,
    }
    result = run_funnel(FIXTURE_REPOS / "repo01", recipe, adapters)
    tier_names = [name for name, _ in result.per_tier_verdict]
    # Daikon attempted but raised → not recorded; final tier is judge.
    assert "daikon" not in tier_names
    assert result.final_verdict.tier == "judge"
    assert result.final_verdict.passed is True


def test_funnel_stage_filter_compile_only() -> None:
    recipe = _make_recipe()
    daytona = StubDaytona()
    anthropic = StubAnthropic("PASS")
    adapters = {"daytona": daytona, "anthropic": anthropic, "enable_daikon": False}
    result = run_funnel(
        FIXTURE_REPOS / "repo01",
        recipe,
        adapters,
        stages=("compile_only",),
    )
    tier_names = [name for name, _ in result.per_tier_verdict]
    assert tier_names == ["compile_only"]
    assert result.final_verdict.tier == "compile_only"
    assert anthropic.call_count == 0


# -- CLI end-to-end (AC#7) ---------------------------------------------------


def _validator() -> Draft7Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    return Draft7Validator(schema)


def test_cli_run_stage_compile_emits_10_valid_results(tmp_path: Path) -> None:
    out_dir = tmp_path / "funnel_run"
    env = os.environ.copy()
    env["MIGRATION_EVAL_FAKE_DAYTONA_CASSETTE_DIR"] = ""  # default success envelope
    env["MIGRATION_EVAL_FAKE_JUDGE_CASSETTE_DIR"] = str(JUDGE_CASSETTE_DIR)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    start = time.perf_counter()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "migration_evals.cli",
            "run",
            "--stage=compile",
            "--repos",
            str(FIXTURE_REPOS),
            "--limit",
            "10",
            "--out",
            str(out_dir),
        ],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    elapsed = time.perf_counter() - start

    assert proc.returncode == 0, f"CLI failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert elapsed < 180, f"CLI took {elapsed:.1f}s; AC#7 caps at 3 minutes"

    validator = _validator()
    repo_dirs = sorted(p for p in out_dir.iterdir() if p.is_dir())
    assert len(repo_dirs) == 10, f"expected 10 result dirs; got {len(repo_dirs)}"
    for repo_dir in repo_dirs:
        result_path = repo_dir / "result.json"
        assert result_path.is_file(), f"missing result.json under {repo_dir}"
        payload = json.loads(result_path.read_text())
        errors = sorted(validator.iter_errors(payload), key=lambda e: e.path)
        assert not errors, f"{result_path} fails schema: {[e.message for e in errors]}"
        assert payload["oracle_tier"] == "compile_only"
        assert payload["success"] is True
        assert payload["failure_class"] is None
