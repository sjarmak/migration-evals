"""Tests for the OpenAI-backed AnthropicAdapter (cross-family Tier-3 judge).

Mirrors :mod:`tests.test_adapters_anthropic`. The adapter wraps the
``openai`` SDK's ``chat.completions.create`` call but emits the
:class:`~migration_evals.adapters.AnthropicAdapter` envelope shape so
:mod:`migration_evals.oracles.tier3_judge` can read it without knowing
which family answered. Tests inject a fake client so the suite never
imports the real SDK.

Why this exists: same-family judge bias is built in when a Claude judge
scores a Claude-produced diff. A non-Claude (OpenAI/GPT) judge breaks the
same-family loop and lets the dual-judge mode flag disagreement.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from migration_evals.adapters import AnthropicAdapter  # noqa: E402
from migration_evals.adapters_openai import (  # noqa: E402
    DEFAULT_OPENAI_COST_RATES,
    OpenAIBudgetExceededError,
    OpenAIJudgeAdapter,
    build_openai_judge_adapter,
)

# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------


class _FakeCompletions:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[Mapping[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._response


class _FakeChat:
    def __init__(self, response: Any) -> None:
        self.completions = _FakeCompletions(response)


class _FakeOpenAIClient:
    def __init__(self, response: Any) -> None:
        self.chat = _FakeChat(response)


def _stub_chat_response(
    text: str = "PASS judge agrees",
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
) -> Any:
    """Mimic the OpenAI SDK Pydantic shape with ``model_dump`` + nested fields."""

    def model_dump() -> dict:
        return {
            "id": "chatcmpl-test",
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    return SimpleNamespace(model_dump=model_dump)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_openai_adapter_satisfies_anthropic_protocol() -> None:
    """The OpenAI judge adapter must structurally satisfy AnthropicAdapter so
    the funnel can swap it in without touching tier3_judge."""
    adapter = OpenAIJudgeAdapter(client=_FakeOpenAIClient(_stub_chat_response()))
    assert isinstance(adapter, AnthropicAdapter)


# ---------------------------------------------------------------------------
# messages_create envelope
# ---------------------------------------------------------------------------


def test_messages_create_returns_anthropic_envelope_shape() -> None:
    """The wrapper must emit ``content=[{type:'text', text:...}]`` so
    tier3_judge's ``_extract_text`` can read it without knowing the
    upstream family."""
    client = _FakeOpenAIClient(_stub_chat_response("PASS rubric ok"))
    adapter = OpenAIJudgeAdapter(client=client)

    envelope = adapter.messages_create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hello"}],
        system=[{"type": "text", "text": "rubric here", "cache_control": {"type": "ephemeral"}}],
        max_tokens=64,
    )

    assert isinstance(envelope, dict)
    assert envelope["content"] == [{"type": "text", "text": "PASS rubric ok"}]
    # Family disambiguator so the judge tier can label dual-mode details.
    assert envelope.get("_judge_family") == "openai"


def test_messages_create_flattens_system_blocks_to_system_role() -> None:
    """Anthropic ``system`` blocks (with cache_control) must be flattened to
    a single ``role: system`` message; OpenAI Chat Completions does not have
    a separate system parameter and ignores cache_control markers."""
    client = _FakeOpenAIClient(_stub_chat_response())
    adapter = OpenAIJudgeAdapter(client=client)

    adapter.messages_create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "user payload"}],
        system=[
            {"type": "text", "text": "RUBRIC line 1", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "RUBRIC line 2"},
        ],
        max_tokens=8,
    )

    forwarded = client.chat.completions.calls[0]
    assert forwarded["model"] == "gpt-4o-mini"
    sent_messages = forwarded["messages"]
    assert sent_messages[0]["role"] == "system"
    # Both rubric blocks are present; cache_control is dropped.
    assert "RUBRIC line 1" in sent_messages[0]["content"]
    assert "RUBRIC line 2" in sent_messages[0]["content"]
    assert sent_messages[1] == {"role": "user", "content": "user payload"}


def test_messages_create_omits_system_when_none() -> None:
    client = _FakeOpenAIClient(_stub_chat_response())
    adapter = OpenAIJudgeAdapter(client=client)
    adapter.messages_create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    sent_messages = client.chat.completions.calls[0]["messages"]
    assert sent_messages[0]["role"] != "system"


def test_messages_create_strips_cassette_kwarg() -> None:
    """``cassette`` is a Protocol artefact for replay adapters; never forward
    it to the live SDK."""
    client = _FakeOpenAIClient(_stub_chat_response())
    adapter = OpenAIJudgeAdapter(client=client)
    adapter.messages_create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
        cassette=object(),
    )
    assert "cassette" not in client.chat.completions.calls[0]


def test_messages_create_uses_max_completion_tokens() -> None:
    """Modern OpenAI models (GPT-5+) require ``max_completion_tokens``; the
    legacy ``max_tokens`` parameter is rejected. Forward both for safety —
    SDK accepts the modern key on every supported model."""
    client = _FakeOpenAIClient(_stub_chat_response())
    adapter = OpenAIJudgeAdapter(client=client)
    adapter.messages_create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=64,
    )
    forwarded = client.chat.completions.calls[0]
    assert forwarded["max_completion_tokens"] == 64


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------


def test_messages_create_accumulates_cost_from_usage() -> None:
    client = _FakeOpenAIClient(
        _stub_chat_response(prompt_tokens=1_000_000, completion_tokens=200_000)
    )
    rates = {"gpt-4o-mini": {"input": 0.15, "output": 0.60}}
    adapter = OpenAIJudgeAdapter(client=client, cost_rates=rates)
    adapter.messages_create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=128,
    )
    # 0.15 (1M input) + 0.12 (200K output × $0.60/M) = 0.27
    assert adapter.total_cost_usd == pytest.approx(0.27)
    assert adapter.call_count == 1


def test_messages_create_unknown_model_skips_cost() -> None:
    client = _FakeOpenAIClient(_stub_chat_response())
    adapter = OpenAIJudgeAdapter(client=client, cost_rates={})
    adapter.messages_create(
        model="never-heard-of-this-model",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=64,
    )
    assert adapter.total_cost_usd == 0.0


# ---------------------------------------------------------------------------
# Budget guard (pre-call)
# ---------------------------------------------------------------------------


def test_budget_guard_refuses_oversize_call() -> None:
    client = _FakeOpenAIClient(_stub_chat_response())
    rates = {"gpt-4o-mini": {"input": 0.15, "output": 0.60}}
    adapter = OpenAIJudgeAdapter(
        client=client,
        cost_rates=rates,
        per_call_budget_usd=0.0001,
    )
    with pytest.raises(OpenAIBudgetExceededError, match="per-call budget"):
        adapter.messages_create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "a" * 4000}],
            max_tokens=10_000,
        )
    assert client.chat.completions.calls == []


def test_budget_guard_allows_call_under_budget() -> None:
    client = _FakeOpenAIClient(_stub_chat_response())
    rates = {"gpt-4o-mini": {"input": 0.15, "output": 0.60}}
    adapter = OpenAIJudgeAdapter(
        client=client,
        cost_rates=rates,
        per_call_budget_usd=1.0,
    )
    envelope = adapter.messages_create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "tiny"}],
        max_tokens=64,
    )
    assert envelope["content"][0]["text"]
    assert client.chat.completions.calls


def test_budget_guard_disabled_when_unset() -> None:
    client = _FakeOpenAIClient(_stub_chat_response())
    adapter = OpenAIJudgeAdapter(client=client, per_call_budget_usd=None)
    adapter.messages_create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x" * 100_000}],
        max_tokens=10_000,
    )
    assert adapter.call_count == 1


def test_budget_guard_skipped_for_unknown_model() -> None:
    """Without rates we cannot compute worst-case; let the call through."""
    client = _FakeOpenAIClient(_stub_chat_response())
    adapter = OpenAIJudgeAdapter(
        client=client,
        cost_rates={},
        per_call_budget_usd=0.0001,
    )
    adapter.messages_create(
        model="some-future-model",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    assert adapter.call_count == 1


# ---------------------------------------------------------------------------
# Lazy import
# ---------------------------------------------------------------------------


def test_constructor_does_not_import_openai_when_client_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests must work without ``openai`` installed when a client is
    injected. Sentinel: replacing ``openai`` in sys.modules with a poisoned
    object must not break adapter construction."""
    poisoned = object()
    monkeypatch.setitem(sys.modules, "openai", poisoned)  # type: ignore[arg-type]
    adapter = OpenAIJudgeAdapter(client=_FakeOpenAIClient(_stub_chat_response()))
    assert adapter.call_count == 0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_build_openai_judge_adapter_returns_cassette_default(tmp_path: Path) -> None:
    """``provider=cassette`` returns an offline cassette adapter so smoke
    runs need no API key."""
    adapter = build_openai_judge_adapter(
        repo_path=tmp_path,
        adapters_cfg={"openai_provider": "cassette"},
        cassette_dir=None,
    )
    # Cassette adapter still satisfies AnthropicAdapter shape.
    assert isinstance(adapter, AnthropicAdapter)
    envelope = adapter.messages_create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    assert envelope["content"]


def test_build_openai_judge_adapter_selects_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import migration_evals.adapters_openai as mod

    def fake_factory(**_kwargs: Any) -> _FakeOpenAIClient:
        return _FakeOpenAIClient(_stub_chat_response())

    monkeypatch.setattr(mod, "_load_openai_client", fake_factory)

    adapter = build_openai_judge_adapter(
        repo_path=tmp_path,
        adapters_cfg={"openai_provider": "sdk"},
        cassette_dir=None,
    )
    assert isinstance(adapter, OpenAIJudgeAdapter)


def test_build_openai_judge_adapter_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="openai_provider"):
        build_openai_judge_adapter(
            repo_path=tmp_path,
            adapters_cfg={"openai_provider": "banana"},
            cassette_dir=None,
        )


# ---------------------------------------------------------------------------
# Default cost rates sanity
# ---------------------------------------------------------------------------


def test_default_cost_rates_have_required_keys() -> None:
    required = {"input", "output"}
    for model_id, rates in DEFAULT_OPENAI_COST_RATES.items():
        assert required <= set(rates.keys()), f"{model_id} missing keys"


# ---------------------------------------------------------------------------
# Cassette adapter (offline replay)
# ---------------------------------------------------------------------------


def test_cassette_adapter_replays_envelope(tmp_path: Path) -> None:
    """Cassette adapter reads ``<dir>/<repo>.json`` and replays the
    envelope. Mirrors the existing _CassetteAnthropicAdapter behaviour."""
    cassette_dir = tmp_path / "openai_cassettes"
    cassette_dir.mkdir()
    (cassette_dir / "repo01.json").write_text(
        '{"content": [{"type": "text", "text": "PASS recorded"}]}'
    )
    repo_path = tmp_path / "repo01"
    repo_path.mkdir()

    adapter = build_openai_judge_adapter(
        repo_path=repo_path,
        adapters_cfg={"openai_provider": "cassette"},
        cassette_dir=cassette_dir,
    )
    envelope = adapter.messages_create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    assert envelope["content"][0]["text"] == "PASS recorded"


def test_cassette_adapter_defaults_when_no_recording(tmp_path: Path) -> None:
    """Missing cassette must not break the funnel — return a default PASS."""
    repo_path = tmp_path / "unrecorded"
    repo_path.mkdir()
    adapter = build_openai_judge_adapter(
        repo_path=repo_path,
        adapters_cfg={"openai_provider": "cassette"},
        cassette_dir=None,
    )
    envelope = adapter.messages_create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
    )
    assert envelope["content"]


# ---------------------------------------------------------------------------
# Live integration (opt-in)
# ---------------------------------------------------------------------------


_LIVE_OK = (
    os.environ.get("OPENAI_API_KEY") and os.environ.get("MIGRATION_EVAL_OPENAI_INTEGRATION") == "1"
)


@pytest.mark.skipif(
    not _LIVE_OK,
    reason="set OPENAI_API_KEY + MIGRATION_EVAL_OPENAI_INTEGRATION=1 to run",
)
def test_live_messages_create_smoke() -> None:
    """One real call against the OpenAI API. Costs ~$0.0001 on gpt-4o-mini."""
    adapter = build_openai_judge_adapter(
        repo_path=Path("."),
        adapters_cfg={"openai_provider": "sdk"},
        cassette_dir=None,
    )
    envelope = adapter.messages_create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
        max_tokens=8,
    )
    assert envelope["content"]
    assert "PONG" in envelope["content"][0]["text"].upper()
