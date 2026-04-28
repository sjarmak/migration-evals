"""Stub sandbox factory for ``scripts/calibrate.py`` unit tests.

The calibrate driver exposes ``--sandbox-factory module:attr`` so unit
tests can assert that calibrate wires a SandboxAdapter through to the
funnel for tier-1 / tier-2 stages without requiring Docker. This module
provides:

* :func:`stub_factory` — the factory the test passes via
  ``--sandbox-factory tests._calibrate_stub_sandbox:stub_factory``. It
  records every ``(repo_path, image)`` pair and returns a deterministic
  scripted adapter.

* :class:`ScriptedSandbox` — a SandboxAdapter-shaped stand-in whose
  ``exec`` consults a per-fixture script keyed off the recipe's
  build/test commands. Default behaviour is "everything passes" so the
  default test only needs to assert wiring.

* :func:`reset` / :func:`recorded_calls` — module-level state helpers
  the tests use to inspect what calibrate did.

The recorded state lives at module scope (not on the factory closure)
because ``--sandbox-factory`` re-imports the module in the calibrate
subprocess, but the subprocess writes its observations to a side-channel
file (``CALIBRATE_STUB_LOG``) the parent test reads back.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

LOG_ENV_VAR = "CALIBRATE_STUB_LOG"
SCRIPT_ENV_VAR = "CALIBRATE_STUB_SCRIPT"


def _log_path() -> Path | None:
    raw = os.environ.get(LOG_ENV_VAR)
    return Path(raw) if raw else None


def _load_script() -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
    """Load the per-fixture exec script from ``CALIBRATE_STUB_SCRIPT``.

    Shape: ``{fixture_dir_name: {build_cmd_substring: {exit_code, ...}}}``.
    A missing entry defaults to exit_code=0 (everything passes), so a
    test that only cares about wiring can omit the script env var.
    """
    raw = os.environ.get(SCRIPT_ENV_VAR)
    if not raw:
        return {}
    return json.loads(Path(raw).read_text())


def _record(event: dict[str, Any]) -> None:
    log = _log_path()
    if log is None:
        return
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


class ScriptedSandbox:
    """SandboxAdapter stand-in driven by a JSON script.

    See module docstring for the script shape.
    """

    def __init__(self, repo_path: Path, *, image: str) -> None:
        self._repo_path = Path(repo_path)
        self._image = image
        self._fixture_id = self._repo_path.parent.name
        self._script = _load_script().get(self._fixture_id, {})
        self._next_id = 0

    def create_sandbox(
        self,
        *,
        image: str,
        env: Mapping[str, str] | None = None,
        cassette: Any | None = None,
    ) -> str:
        self._next_id += 1
        sandbox_id = f"stub-{self._fixture_id}-{self._next_id}"
        _record(
            {
                "event": "create_sandbox",
                "fixture_id": self._fixture_id,
                "image_arg": image,
                "factory_image": self._image,
                "sandbox_id": sandbox_id,
            }
        )
        return sandbox_id

    def exec(
        self,
        sandbox_id: str,
        *,
        command: str,
        timeout_s: int = 600,
        cassette: Any | None = None,
    ) -> Mapping[str, Any]:
        # Pick the first scripted entry whose key appears in the
        # command. Mechanical substring match — keeps this stub honest
        # about not making semantic decisions on behalf of the funnel.
        envelope: Mapping[str, Any] = {
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }
        for key, payload in self._script.items():
            if key in command:
                envelope = {
                    "exit_code": int(payload.get("exit_code", 0)),
                    "stdout": str(payload.get("stdout", "")),
                    "stderr": str(payload.get("stderr", "")),
                }
                break
        _record(
            {
                "event": "exec",
                "fixture_id": self._fixture_id,
                "sandbox_id": sandbox_id,
                "command": command,
                "exit_code": envelope["exit_code"],
            }
        )
        return envelope

    def destroy_sandbox(self, sandbox_id: str) -> None:
        _record(
            {
                "event": "destroy_sandbox",
                "fixture_id": self._fixture_id,
                "sandbox_id": sandbox_id,
            }
        )


def stub_factory(repo_path: Path, *, image: str) -> ScriptedSandbox:
    """Calibrate-side factory entry point used via ``--sandbox-factory``."""
    return ScriptedSandbox(repo_path, image=image)


__all__ = [
    "LOG_ENV_VAR",
    "SCRIPT_ENV_VAR",
    "ScriptedSandbox",
    "stub_factory",
]
