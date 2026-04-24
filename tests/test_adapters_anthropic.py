"""Tests for the live Anthropic-SDK-backed AnthropicAdapter (vj9.6).

The adapter wraps :mod:`anthropic`'s ``messages.create`` call. Tests
inject a fake client so the suite never imports the real SDK and never
issues a network call. A live integration test is gated on
``ANTHROPIC_API_KEY`` and ``MIGRATION_EVAL_ANTHROPIC_INTEGRATION=1``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Mapping

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from migration_evals.adapters import AnthropicAdapter  # noqa: E402
from migration_evals.adapters_anthropic import (  # noqa: E402
    DEFAULT_COST_RATES,
    AnthropicSDKAdapter,
    BudgetExceededError,
    build_anthropic_adapter,
)


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------


class _FakeMessages:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: List[Mapping[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._response


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self.messages = _FakeMessages(response)


def _stub_response(text: str = "PASS judge agrees", *, in_tokens: int = 100, out_tokens: int = 20) -> Any:
    """Mimic the SDK Pydantic-shape with ``model_dump`` + nested fields."""

    def model_dump() -> dict:
        return {
            "id": "msg_test",
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }

    return SimpleNamespace(model_dump=model_dump)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_protocol() -> None:
    adapter = AnthropicSDKAdapter(client=_FakeClient(_stub_response()))
    assert isinstance(adapter, AnthropicAdapter)


# ---------------------------------------------------------------------------
# messages_create
# ---------------------------------------------------------------------------


def test_messages_create_returns_dict_envelope() -> None:
    client = _FakeClient(_stub_response("PASS rubric ok"))
    adapter = AnthropicSDKAdapter(client=client)

    envelope = adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "hello"}],
        system=[{"type": "text", "text": "rubric", "cache_control": {"type": "ephemeral"}}],
        max_tokens=64,
    )

    assert isinstance(envelope, dict)
    assert envelope["content"] == [{"type": "text", "text": "PASS rubric ok"}]
    # The SDK call received the raw kwargs we passed in (cassette filtered out).
    forwarded = client.messages.calls[0]
    assert forwarded["model"] == "claude-haiku-4-5"
    assert forwarded["max_tokens"] == 64
    assert forwarded["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert forwarded["messages"][0]["content"] == "hello"
    assert "cassette" not in forwarded


def test_messages_create_strips_cassette_kwarg() -> None:
    """``cassette`` is a Protocol artefact for replay adapters and must not
    leak into the live SDK call."""
    client = _FakeClient(_stub_response())
    adapter = AnthropicSDKAdapter(client=client)
    adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
        cassette=object(),
    )
    assert "cassette" not in client.messages.calls[0]


def test_messages_create_accumulates_cost() -> None:
    client = _FakeClient(_stub_response(in_tokens=1_000_000, out_tokens=200_000))
    rates = {
        "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_write": 1.25},
    }
    adapter = AnthropicSDKAdapter(client=client, cost_rates=rates)
    adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=128,
    )
    # 1.0 (1M input) + 1.0 (200K output × $5/M) = 2.0
    assert adapter.total_cost_usd == pytest.approx(2.0)
    assert adapter.call_count == 1


def test_messages_create_unknown_model_skips_cost() -> None:
    """A model not in the rate table should not crash; cost stays 0."""
    client = _FakeClient(_stub_response(in_tokens=1000, out_tokens=200))
    adapter = AnthropicSDKAdapter(client=client, cost_rates={})
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
    """Pre-call worst-case estimate exceeds the per-call budget -> reject."""
    client = _FakeClient(_stub_response())
    rates = {
        "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_write": 1.25},
    }
    adapter = AnthropicSDKAdapter(
        client=client,
        cost_rates=rates,
        per_call_budget_usd=0.001,  # absurdly tight: any real call breaches
    )
    with pytest.raises(BudgetExceededError, match="per-call budget"):
        adapter.messages_create(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "a" * 4000}],  # ~1k input tokens
            max_tokens=10_000,  # output cost dominates
        )
    # SDK was never called.
    assert client.messages.calls == []


def test_budget_guard_allows_call_under_budget() -> None:
    client = _FakeClient(_stub_response(in_tokens=100, out_tokens=20))
    rates = {
        "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_write": 1.25},
    }
    adapter = AnthropicSDKAdapter(
        client=client,
        cost_rates=rates,
        per_call_budget_usd=1.0,  # generous
    )
    envelope = adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "tiny"}],
        max_tokens=64,
    )
    assert envelope["content"][0]["text"]
    assert client.messages.calls  # call went through


def test_budget_guard_disabled_when_unset() -> None:
    """``per_call_budget_usd=None`` disables the guard entirely."""
    client = _FakeClient(_stub_response())
    adapter = AnthropicSDKAdapter(client=client, per_call_budget_usd=None)
    adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x" * 100_000}],
        max_tokens=10_000,
    )
    assert adapter.call_count == 1


def test_budget_guard_skipped_for_unknown_model() -> None:
    """Without rates we cannot compute worst-case; let the call through and
    record total_cost_usd == 0.0. The alternative (refuse) would block any
    new model from being trialled."""
    client = _FakeClient(_stub_response(in_tokens=100, out_tokens=20))
    adapter = AnthropicSDKAdapter(
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
# Cache-control passthrough (the whole point of the adapter)
# ---------------------------------------------------------------------------


def test_system_cache_control_block_forwarded_byte_identical() -> None:
    """The rubric block must reach the SDK with cache_control intact, or
    prompt caching breaks and per-call cost spikes."""
    client = _FakeClient(_stub_response())
    adapter = AnthropicSDKAdapter(client=client)
    rubric_block = {
        "type": "text",
        "text": "RUBRIC: do the right thing.",
        "cache_control": {"type": "ephemeral"},
    }
    adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        system=[rubric_block],
        max_tokens=8,
    )
    forwarded = client.messages.calls[0]
    assert forwarded["system"] == [rubric_block]


# ---------------------------------------------------------------------------
# Lazy SDK import (no anthropic dep at import time)
# ---------------------------------------------------------------------------


def test_constructor_does_not_import_anthropic_when_client_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests must work without ``anthropic`` installed when a client is
    injected. Sentinel: replacing ``anthropic`` in sys.modules with a
    poisoned object must not break adapter construction."""
    poisoned = object()
    monkeypatch.setitem(sys.modules, "anthropic", poisoned)  # type: ignore[arg-type]
    adapter = AnthropicSDKAdapter(client=_FakeClient(_stub_response()))
    assert adapter.call_count == 0


# ---------------------------------------------------------------------------
# Factory: build_anthropic_adapter
# ---------------------------------------------------------------------------


def test_build_anthropic_adapter_defaults_to_cassette(tmp_path: Path) -> None:
    from migration_evals.cli import _CassetteAnthropicAdapter

    adapter = build_anthropic_adapter(
        repo_path=tmp_path, adapters_cfg={}, cassette_dir=None
    )
    assert isinstance(adapter, _CassetteAnthropicAdapter)


def test_build_anthropic_adapter_selects_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider=sdk uses an injected client when ``ANTHROPIC_API_KEY`` is
    absent; we monkeypatch the lazy import path instead of installing the
    SDK in the test env."""
    import migration_evals.adapters_anthropic as mod

    def fake_client_factory(**_kwargs: Any) -> _FakeClient:
        return _FakeClient(_stub_response())

    monkeypatch.setattr(mod, "_load_anthropic_client", fake_client_factory)

    adapter = build_anthropic_adapter(
        repo_path=tmp_path,
        adapters_cfg={"anthropic_provider": "sdk"},
        cassette_dir=None,
    )
    assert isinstance(adapter, AnthropicSDKAdapter)


def test_build_anthropic_adapter_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="anthropic_provider"):
        build_anthropic_adapter(
            repo_path=tmp_path,
            adapters_cfg={"anthropic_provider": "banana"},
            cassette_dir=None,
        )


# ---------------------------------------------------------------------------
# Default cost rates sanity
# ---------------------------------------------------------------------------


def test_default_cost_rates_have_required_keys() -> None:
    required = {"input", "output", "cache_read", "cache_write"}
    for model_id, rates in DEFAULT_COST_RATES.items():
        assert required <= set(rates.keys()), f"{model_id} missing keys"
        # Sanity: cache_read should be cheaper than fresh input.
        assert rates["cache_read"] < rates["input"], f"{model_id} cache rates inverted"


# ---------------------------------------------------------------------------
# Live integration (opt-in)
# ---------------------------------------------------------------------------


_LIVE_OK = (
    os.environ.get("ANTHROPIC_API_KEY")
    and os.environ.get("MIGRATION_EVAL_ANTHROPIC_INTEGRATION") == "1"
)


@pytest.mark.skipif(
    not _LIVE_OK,
    reason="set ANTHROPIC_API_KEY + MIGRATION_EVAL_ANTHROPIC_INTEGRATION=1 to run",
)
def test_live_messages_create_smoke() -> None:
    """One real call against the Anthropic API. Costs ~$0.0005 for Haiku.

    Verifies the live SDK shape: response.model_dump() yields a content
    list whose first block has a non-empty ``text``.
    """
    adapter = build_anthropic_adapter(
        repo_path=Path("."),
        adapters_cfg={"anthropic_provider": "sdk"},
        cassette_dir=None,
    )
    envelope = adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
        max_tokens=8,
    )
    assert envelope["content"]
    assert "PONG" in envelope["content"][0]["text"].upper()
