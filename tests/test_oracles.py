"""Per-tier oracle tests (PRD M1).

Each test exercises a single tier in isolation using a fake adapter; the
funnel-orchestration tests live in ``test_funnel.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from migration_evals.harness.recipe import Recipe  # noqa: E402
from migration_evals.oracles import (  # noqa: E402
    tier1_compile,
    tier2_tests,
    tier3_judge,
    tier4_daikon,
)
from migration_evals.oracles.verdict import OracleVerdict  # noqa: E402

REPO_ROOT = _REPO_ROOT
FIXTURE_REPOS = REPO_ROOT / "tests" / "fixtures" / "funnel_repos"


def _make_recipe(build_cmd: str = "mvn -B -e compile", test_cmd: str = "mvn -B -e test") -> Recipe:
    return Recipe(
        dockerfile="FROM maven:3.9-eclipse-temurin-17\n",
        build_cmd=build_cmd,
        test_cmd=test_cmd,
        harness_provenance={
            "model": "claude-haiku-4-5",
            "prompt_version": "v1",
            "timestamp": "2026-04-24T00:00:00Z",
        },
    )


class FakeSandboxCassette:
    """Replay-cassette implementation of the SandboxAdapter Protocol.

    Maps ``cmd -> (exit_code, stdout, stderr)``. Unknown commands raise so
    a misconfigured test fails loudly instead of silently passing.
    """

    def __init__(self, records: Mapping[str, tuple[int, str, str]]) -> None:
        self._records = dict(records)
        self.created: list[str] = []
        self.executed: list[tuple[str, str]] = []
        self.destroyed: list[str] = []

    def create_sandbox(self, *, image: str, env: Any = None, cassette: Any = None) -> str:
        sid = f"sandbox-{len(self.created) + 1}"
        self.created.append(sid)
        return sid

    def exec(self, sandbox_id: str, *, command: str, timeout_s: int = 600, cassette: Any = None) -> Mapping[str, Any]:
        self.executed.append((sandbox_id, command))
        if command not in self._records:
            raise AssertionError(f"FakeSandboxCassette: no record for command {command!r}")
        exit_code, stdout, stderr = self._records[command]
        return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}

    def destroy_sandbox(self, sandbox_id: str) -> None:
        self.destroyed.append(sandbox_id)


class FakeAnthropicCassette:
    """Records the last request and replays a fixed response envelope."""

    def __init__(self, response: Mapping[str, Any]) -> None:
        self._response = dict(response)
        self.last_request: dict[str, Any] = {}
        self.call_count = 0

    def messages_create(
        self,
        *,
        model: str,
        messages: Iterable[Mapping[str, Any]],
        system: Any = None,
        max_tokens: int = 1024,
        cassette: Any = None,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        self.call_count += 1
        self.last_request = {
            "model": model,
            "messages": list(messages),
            "system": system,
            "max_tokens": max_tokens,
            **kwargs,
        }
        return self._response


# -- Tier 1 -------------------------------------------------------------------


def test_tier1_compile_pass() -> None:
    recipe = _make_recipe()
    sandbox = FakeSandboxCassette({recipe.build_cmd: (0, "BUILD SUCCESS", "")})
    verdict = tier1_compile.run(FIXTURE_REPOS / "repo01", recipe, sandbox)
    assert verdict.tier == "compile_only"
    assert verdict.passed is True
    assert verdict.cost_usd == pytest.approx(0.01)
    assert verdict.details["exit_code"] == 0
    assert sandbox.destroyed == sandbox.created  # cleanup happened


def test_tier1_compile_fail() -> None:
    recipe = _make_recipe()
    sandbox = FakeSandboxCassette({recipe.build_cmd: (1, "", "compile error: ;")})
    verdict = tier1_compile.run(FIXTURE_REPOS / "repo02", recipe, sandbox)
    assert verdict.passed is False
    assert verdict.details["exit_code"] == 1


def test_tier1_compile_missing_exit_code_treated_as_fail() -> None:
    recipe = _make_recipe()

    class WeirdSandbox(FakeSandboxCassette):
        def exec(self, sandbox_id, *, command, timeout_s=600, cassette=None):
            return {"stdout": "", "stderr": ""}  # no exit_code key

    verdict = tier1_compile.run(
        FIXTURE_REPOS / "repo03", recipe, WeirdSandbox({recipe.build_cmd: (0, "", "")})
    )
    assert verdict.passed is False


# -- Tier 2 -------------------------------------------------------------------


def test_tier2_tests_pass() -> None:
    recipe = _make_recipe()
    sandbox = FakeSandboxCassette({recipe.test_cmd: (0, "Tests run: 5, Failures: 0", "")})
    verdict = tier2_tests.run(FIXTURE_REPOS / "repo01", recipe, sandbox)
    assert verdict.tier == "tests"
    assert verdict.passed is True
    assert verdict.cost_usd == pytest.approx(0.03)


def test_tier2_tests_fail() -> None:
    recipe = _make_recipe()
    sandbox = FakeSandboxCassette({recipe.test_cmd: (1, "", "Tests failed: 2")})
    verdict = tier2_tests.run(FIXTURE_REPOS / "repo01", recipe, sandbox)
    assert verdict.passed is False


# -- Tier 3 -------------------------------------------------------------------


def test_tier3_judge_attaches_cache_control() -> None:
    """The system prompt must be sent as a list of blocks with cache_control set."""
    recipe = _make_recipe()
    cassette = FakeAnthropicCassette(
        {"content": [{"type": "text", "text": "PASS clean migration."}]}
    )
    verdict = tier3_judge.run(FIXTURE_REPOS / "repo01", recipe, cassette)
    assert verdict.tier == "judge"
    assert verdict.passed is True
    assert verdict.cost_usd == pytest.approx(0.08)

    system_payload = cassette.last_request["system"]
    assert isinstance(system_payload, list), "system must be a list of content blocks"
    assert system_payload, "system payload must not be empty"
    block = system_payload[0]
    assert "cache_control" in block, f"cache_control block missing: {block}"
    assert block["cache_control"]["type"] == "ephemeral"
    # The rubric lives in the cached block.
    assert "RUBRIC" in block["text"]


def test_tier3_judge_fail_envelope() -> None:
    recipe = _make_recipe()
    cassette = FakeAnthropicCassette(
        {"content": [{"type": "text", "text": "FAIL the dockerfile is wrong."}]}
    )
    verdict = tier3_judge.run(FIXTURE_REPOS / "repo01", recipe, cassette)
    assert verdict.passed is False
    assert "judge_text" in verdict.details


def test_tier3_judge_empty_envelope_is_fail() -> None:
    recipe = _make_recipe()
    cassette = FakeAnthropicCassette({"content": []})
    verdict = tier3_judge.run(FIXTURE_REPOS / "repo01", recipe, cassette)
    assert verdict.passed is False


# -- Tier 4 -------------------------------------------------------------------


def test_tier4_daikon_is_importable_and_raises() -> None:
    recipe = _make_recipe()
    with pytest.raises(NotImplementedError):
        tier4_daikon.run(FIXTURE_REPOS / "repo01", recipe, sandbox_adapter=None)


# -- OracleVerdict immutability ----------------------------------------------


def test_oracle_verdict_details_is_read_only() -> None:
    verdict = OracleVerdict(tier="compile_only", passed=True, cost_usd=0.01, details={"k": 1})
    with pytest.raises(TypeError):
        verdict.details["k"] = 2  # type: ignore[index]
