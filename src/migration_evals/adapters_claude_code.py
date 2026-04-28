"""Claude-Code-CLI-backed :class:`~migration_evals.adapters.AnthropicAdapter`.

Dispatches via ``claude -p --output-format json`` against the user's
existing Claude Code OAuth credentials. No Anthropic API key required;
usage is billed against the Claude Code subscription rather than per-call
USD spend.

Why this exists
---------------
:mod:`migration_evals.adapters_anthropic` wraps the paid Anthropic SDK
directly. That is the right choice for CI runners and headless servers
that have an ``ANTHROPIC_API_KEY`` provisioned. For developers running
the funnel from a workstation that already has Claude Code installed
and logged in, this adapter avoids paid-API spend by reusing the same
auth path Claude Code itself uses.

Mapping the Protocol to the CLI
-------------------------------
The :class:`AnthropicAdapter` Protocol expects ``system`` to be either
a string or a list of content blocks (with optional ``cache_control``
markers). The Claude Code CLI accepts ``--system-prompt`` as a single
string. This adapter flattens the list-of-blocks form into a string,
discarding cache-control metadata - prompt caching for OAuth callers is
managed internally by Claude Code, not via the cache_control marker
that the paid API reads.

The ``messages`` list is concatenated into one user prompt and passed
as the trailing positional argument to ``claude -p``. Multi-turn
conversations should be modelled at the orchestrator layer, not here -
each ``messages_create`` call is a one-shot dispatch.

Cost accounting
---------------
``claude -p`` reports ``total_cost_usd`` in its JSON envelope - the
API-equivalent cost. For users on a Claude Code subscription, that is
not what they are billed; it is the value the same call would cost
through the paid API. We accumulate it on ``self.total_cost_usd``
anyway because it is the most useful comparable signal, but treat it
as advisory rather than billing-truth.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["ClaudeCodeAdapter", "ClaudeCodeError"]


DEFAULT_CLAUDE_BIN = "claude"
DEFAULT_TIMEOUT_S = 120


class ClaudeCodeError(RuntimeError):
    """Raised when ``claude -p`` exits non-zero, returns non-JSON, or
    reports an error envelope."""


class ClaudeCodeAdapter:
    """AnthropicAdapter that shells out to ``claude -p``.

    Parameters
    ----------
    claude_bin
        Name or absolute path of the Claude Code CLI on ``$PATH``.
    timeout_s
        Subprocess timeout per call. Conservative default; raise it for
        slow rubrics or long manifests.
    """

    def __init__(
        self,
        *,
        claude_bin: str = DEFAULT_CLAUDE_BIN,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._claude_bin = claude_bin
        self._timeout_s = timeout_s
        self.total_cost_usd: float = 0.0
        self.call_count: int = 0
        self.last_request: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # AnthropicAdapter Protocol
    # ------------------------------------------------------------------

    def messages_create(
        self,
        *,
        model: str,
        messages: Iterable[Mapping[str, Any]],
        system: Any | None = None,
        max_tokens: int = 1024,
        cassette: Any | None = None,  # ignored; Protocol artefact
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        materialised = list(messages)
        system_text = _flatten_system(system)
        user_prompt = _compose_user_prompt(materialised)

        # Capture before subprocess call so a thrown exception still
        # leaves a record of what we tried to send.
        self.last_request = {
            "model": model,
            "messages": materialised,
            "system": system,
            "max_tokens": max_tokens,
        }

        args = [
            self._claude_bin,
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            # Tier-3 judge is a one-shot rubric call that should never reach
            # for tools. Disallow common write/exec tools defensively so
            # even a model that tries cannot touch the user's filesystem,
            # git state, or shell.
            "--disallowedTools",
            "Bash,Edit,Write,NotebookEdit",
        ]
        if system_text:
            args.extend(["--system-prompt", system_text])
        args.append(user_prompt)

        try:
            completed = subprocess.run(
                args, capture_output=True, text=True, timeout=self._timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCodeError(
                f"timeout after {self._timeout_s}s waiting for claude -p"
            ) from exc

        if completed.returncode != 0:
            raise ClaudeCodeError(
                f"claude -p failed (exit={completed.returncode}): " f"{completed.stderr.strip()}"
            )

        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            preview = completed.stdout[:200]
            raise ClaudeCodeError(f"claude -p returned non-JSON stdout: {preview!r}") from exc

        if envelope.get("is_error"):
            raise ClaudeCodeError(
                f"claude -p reported error: {envelope.get('result')!r} "
                f"(api_error_status={envelope.get('api_error_status')})"
            )

        text = str(envelope.get("result") or "")
        usage = envelope.get("usage") or {}
        cost = float(envelope.get("total_cost_usd") or 0.0)

        self.total_cost_usd += cost
        self.call_count += 1

        return {
            "id": envelope.get("session_id"),
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": envelope.get("stop_reason"),
            "usage": {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
                "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
            },
            "_claude_code": {
                "session_id": envelope.get("session_id"),
                "duration_ms": envelope.get("duration_ms"),
                "total_cost_usd_advisory": cost,
            },
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flatten_system(system: Any) -> str:
    """Collapse the AnthropicAdapter ``system`` payload into a single string.

    Accepts None, a plain string, or a list of content blocks. Block-level
    ``cache_control`` markers are intentionally dropped - the paid-API form
    of prompt caching is not consumed by ``claude -p``.
    """
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for blk in system:
            if isinstance(blk, Mapping):
                text = blk.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n\n".join(parts)
    return str(system)


def _compose_user_prompt(messages: Iterable[Mapping[str, Any]]) -> str:
    """Concatenate message contents into one prompt for ``claude -p``.

    Multi-turn conversations are not first-class through this adapter -
    each ``messages_create`` is a one-shot. Callers that need turn
    structure should encode it inside the user prompt themselves.
    """
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, Mapping):
                    text = blk.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
    return "\n\n".join(parts)


def claude_cli_available(claude_bin: str = DEFAULT_CLAUDE_BIN) -> bool:
    """Return True if ``claude`` is on ``$PATH`` (or ``claude_bin`` resolves)."""
    return shutil.which(claude_bin) is not None
