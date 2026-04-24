"""Tests for the Docker-backed SandboxAdapter (vj9.4).

The adapter shells out to the ``docker`` CLI. Unit tests monkeypatch
``subprocess.run`` so they pass on machines without Docker installed.
The live integration test at the bottom is opt-in via the
``MIGRATION_EVAL_DOCKER_INTEGRATION`` environment variable and skipped
otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Mapping, Sequence

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from migration_evals.adapters import SandboxAdapter  # noqa: E402
from migration_evals.adapters_docker import (  # noqa: E402
    DockerSandboxAdapter,
    build_sandbox_adapter,
)


# ---------------------------------------------------------------------------
# subprocess.run recorder
# ---------------------------------------------------------------------------


class _StubProc:
    """Minimal stand-in for :class:`subprocess.CompletedProcess`."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Recorder:
    """Capture subprocess.run invocations and reply from a queue."""

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


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_protocol(tmp_path: Path) -> None:
    adapter = DockerSandboxAdapter(tmp_path)
    assert isinstance(adapter, SandboxAdapter)


# ---------------------------------------------------------------------------
# create_sandbox
# ---------------------------------------------------------------------------


def test_create_sandbox_issues_docker_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder([_StubProc(stdout="deadbeef0000\n")])
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path, workdir="/work")

    sandbox_id = adapter.create_sandbox(image="build-sandbox:latest", env={"JAVA_HOME": "/opt/jdk17"})

    assert sandbox_id == "deadbeef0000"
    assert len(recorder.calls) == 1
    args = recorder.calls[0]["args"]
    assert args[:2] == ["docker", "run"]
    assert "-d" in args and "--rm" in args
    # Mount is the repo path; workdir is /work
    mount_arg = f"{tmp_path.resolve()}:/work"
    assert mount_arg in args
    assert args[args.index("-w") + 1] == "/work"
    # Env var is passed through as -e KEY=VALUE
    assert "JAVA_HOME=/opt/jdk17" in args
    # Image comes before the keep-alive tail command
    assert "build-sandbox:latest" in args
    idx = args.index("build-sandbox:latest")
    assert args[idx + 1 :] == ["tail", "-f", "/dev/null"]


def test_create_sandbox_raises_when_docker_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = subprocess.CalledProcessError(
        returncode=1, cmd=["docker", "run"], output="", stderr="unknown image"
    )
    monkeypatch.setattr(subprocess, "run", _Recorder([err]))
    adapter = DockerSandboxAdapter(tmp_path)

    with pytest.raises(RuntimeError, match="docker run failed"):
        adapter.create_sandbox(image="nonexistent:latest")


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------


def test_exec_returns_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(
        [
            _StubProc(stdout="container-id\n"),  # create_sandbox
            _StubProc(returncode=0, stdout="BUILD SUCCESS\n", stderr=""),  # exec
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path)
    sid = adapter.create_sandbox(image="build-sandbox:latest")

    envelope = adapter.exec(sid, command="mvn -q -DskipTests package", timeout_s=60)

    assert envelope == {"exit_code": 0, "stdout": "BUILD SUCCESS\n", "stderr": ""}
    exec_call = recorder.calls[1]["args"]
    assert exec_call[:3] == ["docker", "exec", "container-id"]
    assert exec_call[-3:] == ["sh", "-c", "mvn -q -DskipTests package"]


def test_exec_propagates_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(
        [
            _StubProc(stdout="c1\n"),
            _StubProc(returncode=2, stdout="", stderr="compile error\n"),
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path)
    sid = adapter.create_sandbox(image="x")

    envelope = adapter.exec(sid, command="false")
    assert envelope == {"exit_code": 2, "stdout": "", "stderr": "compile error\n"}


def test_exec_timeout_kills_container_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeout_exc = subprocess.TimeoutExpired(cmd=["docker", "exec"], timeout=1, output="partial", stderr="")
    recorder = _Recorder(
        [
            _StubProc(stdout="c1\n"),  # create
            timeout_exc,  # exec raises
            _StubProc(returncode=0, stdout="c1\n"),  # docker kill
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path)
    sid = adapter.create_sandbox(image="x")

    envelope = adapter.exec(sid, command="sleep 9999", timeout_s=1)

    assert envelope["exit_code"] != 0
    assert "timeout" in envelope["stderr"].lower()
    assert envelope["stdout"] == "partial"
    # The last recorded call is docker kill <container_id>.
    kill_call = recorder.calls[-1]["args"]
    assert kill_call == ["docker", "kill", "c1"]


def test_exec_unknown_sandbox_raises(tmp_path: Path) -> None:
    adapter = DockerSandboxAdapter(tmp_path)
    with pytest.raises(KeyError):
        adapter.exec("never-created", command="true")


# ---------------------------------------------------------------------------
# destroy_sandbox
# ---------------------------------------------------------------------------


def test_destroy_sandbox_removes_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(
        [
            _StubProc(stdout="cabc\n"),  # create
            _StubProc(returncode=0, stdout="cabc\n"),  # docker rm -f
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path)
    sid = adapter.create_sandbox(image="x")
    adapter.destroy_sandbox(sid)

    rm_call = recorder.calls[-1]["args"]
    assert rm_call == ["docker", "rm", "-f", "cabc"]


def test_destroy_sandbox_unknown_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _Recorder([]))  # expect zero calls
    adapter = DockerSandboxAdapter(tmp_path)
    adapter.destroy_sandbox("never-created")  # must not raise


def test_destroy_sandbox_tolerates_rm_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder(
        [
            _StubProc(stdout="cbad\n"),
            _StubProc(returncode=1, stdout="", stderr="no such container"),
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path)
    sid = adapter.create_sandbox(image="x")

    adapter.destroy_sandbox(sid)  # must not raise even when docker rm exits nonzero


# ---------------------------------------------------------------------------
# Factory: build_sandbox_adapter
# ---------------------------------------------------------------------------


def test_build_sandbox_adapter_defaults_to_cassette(tmp_path: Path) -> None:
    from migration_evals.cli import _CassetteSandboxAdapter

    adapter = build_sandbox_adapter(
        repo_path=tmp_path, adapters_cfg={}, cassette_dir=None
    )
    assert isinstance(adapter, _CassetteSandboxAdapter)


def test_build_sandbox_adapter_selects_docker(tmp_path: Path) -> None:
    adapter = build_sandbox_adapter(
        repo_path=tmp_path,
        adapters_cfg={"sandbox_provider": "docker"},
        cassette_dir=None,
    )
    assert isinstance(adapter, DockerSandboxAdapter)


def test_build_sandbox_adapter_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sandbox_provider"):
        build_sandbox_adapter(
            repo_path=tmp_path,
            adapters_cfg={"sandbox_provider": "banana"},
            cassette_dir=None,
        )


# ---------------------------------------------------------------------------
# Live Docker integration (opt-in)
# ---------------------------------------------------------------------------


_DOCKER_AVAILABLE = shutil.which("docker") is not None
_DOCKER_INTEGRATION = os.environ.get("MIGRATION_EVAL_DOCKER_INTEGRATION") == "1"


@pytest.mark.skipif(
    not (_DOCKER_AVAILABLE and _DOCKER_INTEGRATION),
    reason="set MIGRATION_EVAL_DOCKER_INTEGRATION=1 with Docker available",
)
def test_live_docker_roundtrip(tmp_path: Path) -> None:
    """End-to-end: create -> exec -> destroy against real Docker."""
    (tmp_path / "hello.txt").write_text("hi from repo\n")
    adapter = DockerSandboxAdapter(tmp_path)
    sid = adapter.create_sandbox(image="alpine:3.19")
    try:
        envelope = adapter.exec(sid, command="cat hello.txt")
        assert envelope["exit_code"] == 0
        assert "hi from repo" in envelope["stdout"]
    finally:
        adapter.destroy_sandbox(sid)
