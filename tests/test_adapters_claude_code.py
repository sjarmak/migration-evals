"""Tests for the Claude-Code-CLI-backed AnthropicAdapter (vj9.6 follow-up).

Dispatches via ``claude -p --output-format json`` against the user's
existing OAuth credentials, so no API key is required. Unit tests
monkeypatch ``subprocess.run`` so the suite never invokes the real
CLI. The opt-in live integration test runs ``claude -p`` for real.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Mapping, Sequence

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from migration_evals.adapters import AnthropicAdapter  # noqa: E402
from migration_evals.adapters_claude_code import (  # noqa: E402
    ClaudeCodeAdapter,
    ClaudeCodeError,
)
from migration_evals.adapters_anthropic import build_anthropic_adapter  # noqa: E402

# ---------------------------------------------------------------------------
# subprocess.run recorder
# ---------------------------------------------------------------------------


class _StubProc:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Recorder:
    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.calls: List[Mapping[str, Any]] = []

    def __call__(self, args: Sequence[str], **kwargs: Any) -> Any:
        self.calls.append({"args": list(args), "kwargs": dict(kwargs)})
        if not self._responses:
            raise AssertionError(f"unexpected subprocess.run call: {args}")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _ok_envelope(text: str = "PASS rubric ok") -> str:
    """Mimic the JSON shape `claude -p --output-format json` emits."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "session_id": "abc-123",
            "duration_ms": 1500,
            "total_cost_usd": 0.0123,
            "usage": {
                "input_tokens": 42,
                "output_tokens": 8,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_protocol() -> None:
    assert isinstance(ClaudeCodeAdapter(), AnthropicAdapter)


# ---------------------------------------------------------------------------
# messages_create — happy path
# ---------------------------------------------------------------------------


def test_messages_create_returns_anthropic_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder([_StubProc(stdout=_ok_envelope("PASS judge approves"))])
    monkeypatch.setattr(subprocess, "run", recorder)

    adapter = ClaudeCodeAdapter()
    envelope = adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "Patch: add Optional return type."}],
        system=[
            {
                "type": "text",
                "text": "RUBRIC: respond PASS or FAIL.",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        max_tokens=64,
    )

    # The envelope must look like an Anthropic messages.create response.
    assert envelope["content"] == [{"type": "text", "text": "PASS judge approves"}]
    assert envelope["model"] == "claude-haiku-4-5"
    assert envelope["usage"]["input_tokens"] == 42
    assert envelope["usage"]["output_tokens"] == 8
    # Subprocess argv: claude -p --output-format json --model X --system-prompt RUBRIC USER
    args = recorder.calls[0]["args"]
    assert args[0].endswith("claude")
    assert "-p" in args
    assert "--output-format" in args and args[args.index("--output-format") + 1] == "json"
    assert "--model" in args and args[args.index("--model") + 1] == "claude-haiku-4-5"
    # System prompt was flattened from the rubric block.
    sys_idx = args.index("--system-prompt")
    assert "RUBRIC: respond PASS or FAIL." in args[sys_idx + 1]
    # User message is the trailing positional arg.
    assert "Patch: add Optional return type." in args[-1]


def test_cost_and_call_count_accumulate(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(
        [
            _StubProc(stdout=_ok_envelope()),
            _StubProc(stdout=_ok_envelope()),
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)

    adapter = ClaudeCodeAdapter()
    for _ in range(2):
        adapter.messages_create(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "ok"}],
            max_tokens=8,
        )

    assert adapter.call_count == 2
    assert adapter.total_cost_usd == pytest.approx(0.0246)


def test_cassette_kwarg_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Protocol artefact - must never end up on the claude command line."""
    recorder = _Recorder([_StubProc(stdout=_ok_envelope())])
    monkeypatch.setattr(subprocess, "run", recorder)

    adapter = ClaudeCodeAdapter()
    adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=8,
        cassette=object(),
    )
    flat = " ".join(recorder.calls[0]["args"])
    assert "cassette" not in flat


def test_system_blocks_flatten_to_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-block ``system`` payloads (cache_control etc.) collapse to a
    single ``--system-prompt`` text. Cache-control markers are dropped
    because claude -p does not accept the structured form."""
    recorder = _Recorder([_StubProc(stdout=_ok_envelope())])
    monkeypatch.setattr(subprocess, "run", recorder)

    adapter = ClaudeCodeAdapter()
    adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "ok"}],
        system=[
            {"type": "text", "text": "Block A", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "Block B"},
        ],
        max_tokens=8,
    )
    args = recorder.calls[0]["args"]
    sys_text = args[args.index("--system-prompt") + 1]
    assert "Block A" in sys_text
    assert "Block B" in sys_text
    # No leakage of cache_control structure.
    assert "ephemeral" not in sys_text
    assert "cache_control" not in sys_text


def test_string_system_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder([_StubProc(stdout=_ok_envelope())])
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = ClaudeCodeAdapter()
    adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "ok"}],
        system="Plain string rubric",
        max_tokens=8,
    )
    args = recorder.calls[0]["args"]
    assert args[args.index("--system-prompt") + 1] == "Plain string rubric"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder([_StubProc(returncode=1, stdout="", stderr="not authenticated")])
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = ClaudeCodeAdapter()
    with pytest.raises(ClaudeCodeError, match="not authenticated"):
        adapter.messages_create(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=8,
        )


def test_non_json_stdout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder([_StubProc(stdout="not json at all")])
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = ClaudeCodeAdapter()
    with pytest.raises(ClaudeCodeError, match="non-JSON"):
        adapter.messages_create(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=8,
        )


def test_envelope_is_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    err_envelope = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "result": "rate limited",
            "api_error_status": 429,
        }
    )
    recorder = _Recorder([_StubProc(stdout=err_envelope)])
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = ClaudeCodeAdapter()
    with pytest.raises(ClaudeCodeError, match="rate limited"):
        adapter.messages_create(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=8,
        )


def test_timeout_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _Recorder([subprocess.TimeoutExpired(cmd=["claude"], timeout=1)]),
    )
    adapter = ClaudeCodeAdapter(timeout_s=1)
    with pytest.raises(ClaudeCodeError, match="timeout"):
        adapter.messages_create(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=8,
        )


# ---------------------------------------------------------------------------
# Factory wiring
# ---------------------------------------------------------------------------


def test_build_anthropic_adapter_selects_claude_code(tmp_path: Path) -> None:
    adapter = build_anthropic_adapter(
        repo_path=tmp_path,
        adapters_cfg={"anthropic_provider": "claude_code"},
        cassette_dir=None,
    )
    assert isinstance(adapter, ClaudeCodeAdapter)


def test_build_anthropic_adapter_passes_config(tmp_path: Path) -> None:
    adapter = build_anthropic_adapter(
        repo_path=tmp_path,
        adapters_cfg={
            "anthropic_provider": "claude_code",
            "claude_bin": "/custom/path/claude",
            "claude_timeout_s": 90,
        },
        cassette_dir=None,
    )
    assert isinstance(adapter, ClaudeCodeAdapter)
    assert adapter._claude_bin == "/custom/path/claude"
    assert adapter._timeout_s == 90


# ---------------------------------------------------------------------------
# Live integration (opt-in)
# ---------------------------------------------------------------------------


_CLAUDE_AVAILABLE = shutil.which("claude") is not None
_LIVE_OK = _CLAUDE_AVAILABLE and os.environ.get("MIGRATION_EVAL_CLAUDE_CODE_INTEGRATION") == "1"


@pytest.mark.skipif(
    not _LIVE_OK,
    reason="set MIGRATION_EVAL_CLAUDE_CODE_INTEGRATION=1 with the claude CLI logged in",
)
def test_live_messages_create_smoke() -> None:
    """End-to-end roundtrip against real claude -p. Uses OAuth, not API key."""
    adapter = ClaudeCodeAdapter()
    envelope = adapter.messages_create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
        system="You are an echo bot. Reply with exactly what the user requests.",
        max_tokens=16,
    )
    assert envelope["content"]
    text = envelope["content"][0]["text"].upper()
    assert "PONG" in text
