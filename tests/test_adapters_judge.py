"""Tests for the dual-family judge adapter (bead migration_evals-cns).

The :class:`DualFamilyJudgeAdapter` wraps an Anthropic-shaped judge and a
non-Claude (e.g. OpenAI) judge so Tier 3 can score every trial twice and
require pairwise agreement before passing. Bead spec: per-tier verdicts
must surface ``verdict_anthropic + verdict_other + verdicts_disagreed``;
the trial PASSes only when both judges agree.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from migration_evals.adapters import AnthropicAdapter  # noqa: E402
from migration_evals.adapters_judge import (  # noqa: E402
    DualFamilyJudgeAdapter,
    build_judge_adapter,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeJudge:
    """Records calls, replays a fixed envelope. Used as either side of dual."""

    def __init__(self, response_text: str, *, family: str = "anthropic") -> None:
        self._family = family
        self._response_text = response_text
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
            "cassette": cassette,
            **kwargs,
        }
        return {
            "content": [{"type": "text", "text": self._response_text}],
            "_judge_family": self._family,
        }


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_dual_judge_satisfies_anthropic_protocol() -> None:
    adapter = DualFamilyJudgeAdapter(
        anthropic_adapter=_FakeJudge("PASS"),
        other_adapter=_FakeJudge("PASS", family="openai"),
        other_model="gpt-4o-mini",
    )
    assert isinstance(adapter, AnthropicAdapter)


# ---------------------------------------------------------------------------
# Both judges called, envelope carries _dual_family payload
# ---------------------------------------------------------------------------


def test_dual_judge_calls_both_adapters() -> None:
    anthropic = _FakeJudge("PASS rubric ok")
    other = _FakeJudge("PASS rubric ok", family="openai")
    adapter = DualFamilyJudgeAdapter(
        anthropic_adapter=anthropic,
        other_adapter=other,
        other_model="gpt-4o-mini",
    )
    adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        system=[{"type": "text", "text": "rubric"}],
        max_tokens=64,
    )
    assert anthropic.call_count == 1
    assert other.call_count == 1


def test_dual_judge_routes_other_model_separately() -> None:
    """The Anthropic side gets the caller's model (claude-*); the Other side
    gets its own configured model. Tier3_judge passes ``DEFAULT_MODEL`` which
    is a Claude name — the OpenAI adapter would error on that name."""
    anthropic = _FakeJudge("PASS")
    other = _FakeJudge("PASS", family="openai")
    adapter = DualFamilyJudgeAdapter(
        anthropic_adapter=anthropic,
        other_adapter=other,
        other_model="gpt-4o-mini",
    )
    adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    assert anthropic.last_request["model"] == "claude-haiku-4-5"
    assert other.last_request["model"] == "gpt-4o-mini"


def test_dual_judge_envelope_carries_both_envelopes() -> None:
    anthropic = _FakeJudge("PASS anthropic side")
    other = _FakeJudge("FAIL openai dissents", family="openai")
    adapter = DualFamilyJudgeAdapter(
        anthropic_adapter=anthropic,
        other_adapter=other,
        other_model="gpt-4o-mini",
    )
    envelope = adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    dual = envelope.get("_dual_family")
    assert isinstance(dual, dict), "envelope must carry _dual_family payload"
    assert dual["anthropic_envelope"]["content"][0]["text"] == "PASS anthropic side"
    assert dual["other_envelope"]["content"][0]["text"] == "FAIL openai dissents"
    assert dual["other_model"] == "gpt-4o-mini"


def test_dual_judge_envelope_content_defaults_to_anthropic_text() -> None:
    """The top-level ``content`` field stays anthropic-shaped so any consumer
    that ignores _dual_family still sees a coherent verdict text. The judge
    tier itself reads the per-judge texts out of _dual_family."""
    anthropic = _FakeJudge("PASS anthropic")
    other = _FakeJudge("PASS openai", family="openai")
    adapter = DualFamilyJudgeAdapter(
        anthropic_adapter=anthropic,
        other_adapter=other,
        other_model="gpt-4o-mini",
    )
    envelope = adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    assert envelope["content"][0]["text"] == "PASS anthropic"


def test_dual_judge_has_no_cassette_parameter() -> None:
    """Replay is a construction-time provider choice, not a per-call hook;
    the dead ``cassette`` kwarg was removed from the adapter surface."""
    import inspect

    sig = inspect.signature(DualFamilyJudgeAdapter.messages_create)
    assert "cassette" not in sig.parameters


# ---------------------------------------------------------------------------
# Cost accounting passthrough
# ---------------------------------------------------------------------------


def test_dual_judge_aggregates_cost_from_both_sides() -> None:
    """``total_cost_usd`` (when present) must include both sides."""

    class _CostingJudge:
        def __init__(self, cost: float, family: str) -> None:
            self._cost = cost
            self._family = family
            self.total_cost_usd = 0.0
            self.call_count = 0
            self.last_request: dict[str, Any] = {}

        def messages_create(
            self, *, model, messages, system=None, max_tokens=1024, cassette=None, **kwargs
        ):
            self.total_cost_usd += self._cost
            self.call_count += 1
            self.last_request = {"model": model}
            return {
                "content": [{"type": "text", "text": "PASS"}],
                "_judge_family": self._family,
            }

    anthropic = _CostingJudge(0.08, "anthropic")
    other = _CostingJudge(0.02, "openai")
    adapter = DualFamilyJudgeAdapter(
        anthropic_adapter=anthropic,
        other_adapter=other,
        other_model="gpt-4o-mini",
    )
    adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    assert adapter.total_cost_usd == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# Factory: build_judge_adapter
# ---------------------------------------------------------------------------


def test_build_judge_adapter_single_family_default(tmp_path: Path) -> None:
    """``judge.dual_family`` unset → factory falls back to single-family
    Anthropic adapter (the existing behaviour)."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    adapter = build_judge_adapter(
        repo_path=repo_path,
        adapters_cfg={},
        anthropic_cassette_dir=None,
        openai_cassette_dir=None,
    )
    # Single-family path: not wrapped.
    assert not isinstance(adapter, DualFamilyJudgeAdapter)
    assert isinstance(adapter, AnthropicAdapter)


def test_build_judge_adapter_dual_family_wraps(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    adapter = build_judge_adapter(
        repo_path=repo_path,
        adapters_cfg={
            "judge": {
                "dual_family": True,
                "other_provider": "openai",
                "other_model": "gpt-4o-mini",
            },
            "anthropic_provider": "cassette",
            "openai_provider": "cassette",
        },
        anthropic_cassette_dir=None,
        openai_cassette_dir=None,
    )
    assert isinstance(adapter, DualFamilyJudgeAdapter)
    # Both sides produce envelopes when called.
    envelope = adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    assert envelope["_dual_family"]["other_model"] == "gpt-4o-mini"


def test_build_judge_adapter_rejects_unknown_other_provider(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    with pytest.raises(ValueError, match="other_provider"):
        build_judge_adapter(
            repo_path=repo_path,
            adapters_cfg={
                "judge": {"dual_family": True, "other_provider": "kale", "other_model": "x"},
                "anthropic_provider": "cassette",
            },
            anthropic_cassette_dir=None,
            openai_cassette_dir=None,
        )


def test_build_judge_adapter_requires_other_model_when_dual(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    with pytest.raises(ValueError, match="other_model"):
        build_judge_adapter(
            repo_path=repo_path,
            adapters_cfg={
                "judge": {"dual_family": True, "other_provider": "openai"},
                "anthropic_provider": "cassette",
            },
            anthropic_cassette_dir=None,
            openai_cassette_dir=None,
        )


# ---------------------------------------------------------------------------
# One-sided failure contract (bead migration_evals-7bo)
# ---------------------------------------------------------------------------


class _RaisingJudge:
    """A judge whose API call always raises."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.call_count = 0

    def messages_create(self, **kwargs: Any) -> Mapping[str, Any]:
        self.call_count += 1
        raise self._exc


def test_dual_judge_other_side_failure_surfaces_error_not_envelope() -> None:
    adapter = DualFamilyJudgeAdapter(
        anthropic_adapter=_FakeJudge("PASS anthropic"),
        other_adapter=_RaisingJudge(TimeoutError("openai down")),
        other_model="gpt-4o-mini",
    )
    envelope = adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    dual = envelope["_dual_family"]
    assert "other_envelope" not in dual
    assert "TimeoutError" in dual["other_error"]
    assert "openai down" in dual["other_error"]
    # The healthy side's envelope is still present and content is coherent.
    assert dual["anthropic_envelope"]["content"][0]["text"] == "PASS anthropic"
    assert envelope["content"][0]["text"] == "PASS anthropic"


def test_dual_judge_anthropic_side_failure_surfaces_error() -> None:
    adapter = DualFamilyJudgeAdapter(
        anthropic_adapter=_RaisingJudge(ConnectionError("anthropic down")),
        other_adapter=_FakeJudge("PASS openai", family="openai"),
        other_model="gpt-4o-mini",
    )
    envelope = adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    dual = envelope["_dual_family"]
    assert "anthropic_envelope" not in dual
    assert "ConnectionError" in dual["anthropic_error"]
    assert dual["other_envelope"]["content"][0]["text"] == "PASS openai"
    # Top-level content is empty (anthropic side never answered).
    assert envelope["content"] == []


def test_dual_judge_both_sides_failing_raises() -> None:
    adapter = DualFamilyJudgeAdapter(
        anthropic_adapter=_RaisingJudge(ConnectionError("anthropic down")),
        other_adapter=_RaisingJudge(TimeoutError("openai down")),
        other_model="gpt-4o-mini",
    )
    with pytest.raises(RuntimeError, match="both adapters failed"):
        adapter.messages_create(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=8,
        )


def test_dual_judge_one_sided_failure_still_calls_other_side() -> None:
    """The healthy side must still be consulted (no fail-fast skip)."""
    other = _FakeJudge("PASS openai", family="openai")
    adapter = DualFamilyJudgeAdapter(
        anthropic_adapter=_RaisingJudge(ConnectionError("anthropic down")),
        other_adapter=other,
        other_model="gpt-4o-mini",
    )
    adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    assert other.call_count == 1
