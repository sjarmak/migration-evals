"""Cassette-backed replay stand-ins for offline / fixture runs.

These are the concrete "provider: cassette" implementations selected by
the adapter factories (``build_sandbox_adapter`` /
``build_anthropic_adapter``) and by the CLI's legacy ``--repos`` fixture
mode. Replay is file-based: each adapter loads pre-recorded envelopes
from a per-repo JSON file under a cassette directory. There is no
per-call cassette hook - an adapter either replays from its directory or
talks to the real backend, selected at construction time.

A request with no recorded envelope still replays (success / PASS) so
fixture scaffolding stays minimal, but the fabricated envelope is
stamped ``cassette_miss=True`` and a warning is emitted; the publication
gate refuses results that carry the stamp.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = ["CassetteSandboxAdapter", "CassetteAnthropicAdapter"]


class CassetteSandboxAdapter:
    """Cassette-backed sandbox stand-in.

    Reads pre-recorded ``(exit_code, stdout, stderr)`` envelopes from
    ``<cassette_dir>/<repo_name>.json``, keyed by the exact command
    string.
    """

    def __init__(self, repo_name: str, cassette_dir: Path | None) -> None:
        self._repo_name = repo_name
        self._cassette_dir = cassette_dir
        self._records: dict[str, Mapping[str, Any]] = {}
        self._warned_miss = False
        if cassette_dir is not None:
            cassette_path = cassette_dir / f"{repo_name}.json"
            if cassette_path.is_file():
                try:
                    self._records = dict(json.loads(cassette_path.read_text()))
                except (OSError, ValueError):
                    self._records = {}
        self._sandbox_counter = 0

    def create_sandbox(self, *, image: str, env: Any = None) -> str:
        self._sandbox_counter += 1
        return f"sandbox-{self._repo_name}-{self._sandbox_counter}"

    def exec(self, sandbox_id: str, *, command: str, timeout_s: int = 600) -> Mapping[str, Any]:
        record = self._records.get(command)
        if record is None:
            if not self._warned_miss:
                self._warned_miss = True
                print(
                    f"warning: sandbox cassette miss for repo "
                    f"{self._repo_name!r} (command {command!r}); replaying "
                    f"default success - result will be stamped cassette_miss",
                    file=sys.stderr,
                )
            return {"exit_code": 0, "stdout": "", "stderr": "", "cassette_miss": True}
        return dict(record)

    def destroy_sandbox(self, sandbox_id: str) -> None:  # pragma: no cover
        return None


class CassetteAnthropicAdapter:
    """Cassette-backed Anthropic stand-in for the judge tier.

    Loads a recorded response envelope from
    ``<cassette_dir>/<repo_name>.json``.
    """

    def __init__(self, repo_name: str, cassette_dir: Path | None) -> None:
        self._repo_name = repo_name
        self._cassette_dir = cassette_dir
        self.last_request: dict[str, Any] = {}
        self.call_count = 0

    def messages_create(
        self,
        *,
        model: str,
        messages: Iterable[Mapping[str, Any]],
        system: Any = None,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        self.call_count += 1
        # Capture so a real implementation can audit the cache_control block.
        self.last_request = {
            "model": model,
            "messages": list(messages),
            "system": system,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if self._cassette_dir is not None:
            cassette_path = self._cassette_dir / f"{self._repo_name}.json"
            if cassette_path.is_file():
                try:
                    return json.loads(cassette_path.read_text())
                except (OSError, ValueError):
                    pass
        print(
            f"warning: judge cassette miss for repo {self._repo_name!r}; "
            f"replaying default PASS - result will be stamped cassette_miss",
            file=sys.stderr,
        )
        return {
            "content": [{"type": "text", "text": "PASS judge defaulted to pass"}],
            "cassette_miss": True,
        }
