"""Tests for the Docker-backed SandboxAdapter (vj9.4).

The adapter shells out to the ``docker`` CLI. Unit tests monkeypatch
``subprocess.run`` so they pass on machines without Docker installed.
The live integration test at the bottom is opt-in via the
``MIGRATION_EVAL_DOCKER_INTEGRATION`` environment variable and skipped
otherwise.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

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
        self.calls: list[Mapping[str, Any]] = []

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

    sandbox_id = adapter.create_sandbox(
        image="build-sandbox:latest", env={"JAVA_HOME": "/opt/jdk17"}
    )

    assert sandbox_id == "deadbeef0000"
    assert len(recorder.calls) == 1
    args = recorder.calls[0]["args"]
    assert args[:2] == ["docker", "run"]
    assert "-d" in args and "--rm" in args
    # Hardened default (7gu): repo mount is read-only.
    mount_arg = f"{tmp_path.resolve()}:/work:ro"
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
    timeout_exc = subprocess.TimeoutExpired(
        cmd=["docker", "exec"], timeout=1, output="partial", stderr=""
    )
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
# Crash-safe teardown: context manager + atexit (cxa wave-2 review LOW #4)
# ---------------------------------------------------------------------------
#
# destroy_sandbox is only invoked on the happy path. If the calling
# process is killed mid-trial (KeyboardInterrupt / SIGTERM) every tracked
# sandbox plus its proxy sidecar + internal docker network leak. The
# adapter therefore supports two safety nets: ``with`` for explicit
# scoping, and an atexit hook for crash paths. Both must be idempotent
# so they cannot double-remove the same container.


def test_context_manager_cleans_up_tracked_sandboxes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`with DockerSandboxAdapter(...) as adapter:` must call destroy on
    every sandbox that's still tracked when the block exits. This is the
    happy-path scope guarantee — callers that forget to call
    destroy_sandbox still get cleaned up."""
    recorder = _Recorder(
        [
            _StubProc(stdout="c1\n"),  # create #1
            _StubProc(stdout="c2\n"),  # create #2
            _StubProc(returncode=0),  # rm c1
            _StubProc(returncode=0),  # rm c2
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    with DockerSandboxAdapter(tmp_path) as adapter:
        adapter.create_sandbox(image="img")
        adapter.create_sandbox(image="img")
    # Both containers must have been removed; order doesn't matter.
    rm_calls = [c["args"] for c in recorder.calls if c["args"][:3] == ["docker", "rm", "-f"]]
    removed_ids = {call[-1] for call in rm_calls}
    assert removed_ids == {"c1", "c2"}


def test_context_manager_returns_self(tmp_path: Path) -> None:
    """`__enter__` must return the adapter so `with X() as a:` works."""
    adapter = DockerSandboxAdapter(tmp_path)
    with adapter as bound:
        assert bound is adapter


def test_context_manager_cleans_up_on_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the body of the `with` block raises, tracked sandboxes must
    still be torn down. This is the crash-safety property the test
    locks in: an exception in the trial body cannot leak containers."""
    recorder = _Recorder(
        [
            _StubProc(stdout="c1\n"),  # create
            _StubProc(returncode=0),  # rm
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    with pytest.raises(RuntimeError, match="boom"):
        with DockerSandboxAdapter(tmp_path) as adapter:
            adapter.create_sandbox(image="img")
            raise RuntimeError("boom")
    rm_calls = [c["args"] for c in recorder.calls if c["args"][:3] == ["docker", "rm", "-f"]]
    assert rm_calls == [["docker", "rm", "-f", "c1"]]


def test_context_manager_no_op_when_no_sandboxes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty `with` block must not call subprocess.run at all — there's
    nothing tracked to tear down."""
    recorder = _Recorder([])  # zero responses; any call would assert
    monkeypatch.setattr(subprocess, "run", recorder)
    with DockerSandboxAdapter(tmp_path):
        pass
    assert recorder.calls == []


def test_context_manager_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second teardown — whether triggered by a nested with, an atexit
    hook firing after the with, or an explicit __exit__ call — must not
    re-invoke `docker rm` for sandboxes already destroyed. Idempotency
    is structural: ``destroy_sandbox`` pops each entry from the tracking
    dicts, so a second pass iterates an empty snapshot."""
    recorder = _Recorder(
        [
            _StubProc(stdout="c1\n"),  # create
            _StubProc(returncode=0),  # rm (first teardown)
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path)
    with adapter:
        adapter.create_sandbox(image="img")
    # Second __exit__ must be a no-op: the tracking dict is now empty,
    # so the iteration finds nothing to destroy.
    adapter.__exit__(None, None, None)
    rm_calls = [c["args"] for c in recorder.calls if c["args"][:3] == ["docker", "rm", "-f"]]
    assert len(rm_calls) == 1


def test_atexit_handler_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter must register an atexit handler so a crash path
    (KeyboardInterrupt outside a `with`, SIGTERM, etc.) still tears down
    tracked sandboxes. Verified by spying on `atexit.register`."""
    registered: list[Any] = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a, **kw: registered.append(fn) or fn)
    DockerSandboxAdapter(tmp_path)
    assert registered, "DockerSandboxAdapter.__init__ must register an atexit handler"


def test_atexit_handler_tears_down_tracked_sandboxes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invoking the registered atexit handler directly must clean up the
    sandboxes that are still tracked — same teardown path as `__exit__`,
    just driven by the atexit hook instead of the `with` block."""
    registered: list[Any] = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a, **kw: registered.append(fn) or fn)
    recorder = _Recorder(
        [
            _StubProc(stdout="c1\n"),  # create
            _StubProc(returncode=0),  # rm (driven by atexit)
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path)
    adapter.create_sandbox(image="img")
    assert registered, "expected atexit handler"
    # Fire the handler manually — simulates interpreter shutdown.
    registered[0]()
    rm_calls = [c["args"] for c in recorder.calls if c["args"][:3] == ["docker", "rm", "-f"]]
    assert rm_calls == [["docker", "rm", "-f", "c1"]]


def test_atexit_handler_is_idempotent_after_explicit_destroy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the user (or `__exit__`) already cleaned up before the
    interpreter shuts down, the atexit handler must not double-remove.
    Otherwise we'd see spurious `no such container` log noise on every
    process exit."""
    registered: list[Any] = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a, **kw: registered.append(fn) or fn)
    recorder = _Recorder(
        [
            _StubProc(stdout="c1\n"),  # create
            _StubProc(returncode=0),  # explicit destroy_sandbox rm
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path)
    sid = adapter.create_sandbox(image="img")
    adapter.destroy_sandbox(sid)
    pre = len(recorder.calls)
    # atexit fires after explicit destroy: must be a no-op.
    registered[0]()
    assert len(recorder.calls) == pre, "atexit must not re-issue docker rm"


def test_atexit_handler_swallows_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At interpreter shutdown the docker daemon may be gone, the user
    may already be killing -9, etc. The atexit handler must NEVER raise
    — propagating an exception out of an atexit callback yields ugly
    tracebacks that mask the real cause of the crash."""
    registered: list[Any] = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a, **kw: registered.append(fn) or fn)

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise OSError("daemon gone")

    # First call (create) succeeds, then everything blows up.
    create_proc = _StubProc(stdout="c1\n")
    calls = {"n": 0}

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return create_proc
        raise OSError("daemon gone")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = DockerSandboxAdapter(tmp_path)
    adapter.create_sandbox(image="img")
    # Must not raise even though every teardown subprocess.run blows up.
    registered[0]()


# ---------------------------------------------------------------------------
# Factory: build_sandbox_adapter
# ---------------------------------------------------------------------------


def test_build_sandbox_adapter_defaults_to_cassette(tmp_path: Path) -> None:
    from migration_evals.cli import _CassetteSandboxAdapter

    adapter = build_sandbox_adapter(repo_path=tmp_path, adapters_cfg={}, cassette_dir=None)
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
# docker_bin override (gk0): proves the podman drop-in path
# ---------------------------------------------------------------------------


def test_docker_bin_override_threads_through_every_subprocess_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting ``docker_bin='podman'`` must cause every CLI invocation
    (create / exec / kill / rm) to call ``podman`` instead of ``docker``.

    This is the load-bearing assertion behind ``docs/sandbox_outer_daemon.md``
    Option B: podman rootless is reachable as a drop-in by flipping the
    config knob, with no code change. The test does not require podman to
    be installed - it only verifies the adapter delegates the binary
    selection to the override.
    """
    timeout_exc = subprocess.TimeoutExpired(cmd=["podman", "exec"], timeout=1, output="", stderr="")
    recorder = _Recorder(
        [
            _StubProc(stdout="cid\n"),  # create_sandbox
            timeout_exc,  # exec raises -> triggers kill path
            _StubProc(returncode=0, stdout="cid\n"),  # kill
            _StubProc(returncode=0, stdout="cid\n"),  # rm -f (destroy)
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path, docker_bin="podman")

    sid = adapter.create_sandbox(image="alpine:3.19")
    adapter.exec(sid, command="sleep 9999", timeout_s=1)
    adapter.destroy_sandbox(sid)

    # Four subprocess calls: create / exec / kill / rm. Every one must
    # start with the overridden binary.
    assert len(recorder.calls) == 4
    for call in recorder.calls:
        assert call["args"][0] == "podman", f"docker_bin override leaked: {call['args'][:3]}"
    # Spot-check sub-commands stay identical (we only swap the binary,
    # not the verbs).
    assert recorder.calls[0]["args"][1] == "run"
    assert recorder.calls[1]["args"][1] == "exec"
    assert recorder.calls[2]["args"][1] == "kill"
    assert recorder.calls[3]["args"][1] == "rm"


def test_build_sandbox_adapter_threads_docker_bin_from_config(
    tmp_path: Path,
) -> None:
    """The factory must propagate ``adapters.docker_bin`` to the adapter.

    Without this, the podman drop-in path documented in
    ``docs/sandbox_outer_daemon.md`` would silently fall back to
    ``docker``.
    """
    adapter = build_sandbox_adapter(
        repo_path=tmp_path,
        adapters_cfg={"sandbox_provider": "docker", "docker_bin": "podman"},
        cassette_dir=None,
    )
    assert isinstance(adapter, DockerSandboxAdapter)
    assert adapter._docker_bin == "podman"


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


# ---------------------------------------------------------------------------
# Sandbox hardening (7gu)
# ---------------------------------------------------------------------------


from migration_evals.sandbox_policy import (  # noqa: E402
    DEFAULT_PROXY_IMAGE,
    SandboxPolicy,
)


def _docker_run_args(tmp_path: Path, monkeypatch, *, policy=None) -> list[str]:
    """Run create_sandbox with a recorder and return the WORKLOAD docker-run argv.

    network='pull' policies issue extra subprocess.run calls (network
    create + proxy run + network connect) before the workload starts;
    the helper returns just the workload `docker run` invocation - the
    one whose argv contains the workload image - so callers can keep
    their assertions focused on the workload's hardening flags.
    """
    # Provide enough stub responses for any backend path: network=none
    # uses 1, network=pull uses up to 4 (network create, proxy run,
    # network connect, workload run). Extra responses are ignored.
    recorder = _Recorder([_StubProc(stdout=f"id-{i}\n") for i in range(8)])
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path, policy=policy)
    adapter.create_sandbox(image="build-sandbox:latest")
    workload_runs = [
        c["args"]
        for c in recorder.calls
        if c["args"][:2] == ["docker", "run"] and "build-sandbox:latest" in c["args"]
    ]
    assert workload_runs, "expected exactly one workload docker-run"
    return workload_runs[-1]


def test_default_policy_drops_all_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _docker_run_args(tmp_path, monkeypatch)
    # --cap-drop ALL must appear; no --cap-add lines by default.
    drops = [args[i + 1] for i, a in enumerate(args) if a == "--cap-drop"]
    adds = [args[i + 1] for i, a in enumerate(args) if a == "--cap-add"]
    assert "ALL" in drops
    assert adds == []


def test_default_policy_disables_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _docker_run_args(tmp_path, monkeypatch)
    # --network none disables the namespace - egress (curl, dns, etc.)
    # cannot leave the container.
    idx = args.index("--network")
    assert args[idx + 1] == "none"


def test_default_policy_no_new_privileges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _docker_run_args(tmp_path, monkeypatch)
    sec_opts = [args[i + 1] for i, a in enumerate(args) if a == "--security-opt"]
    assert "no-new-privileges:true" in sec_opts


def test_default_policy_runs_rootless_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _docker_run_args(tmp_path, monkeypatch)
    user_idx = args.index("--user")
    assert args[user_idx + 1] == "1000:1000"


def test_default_policy_mounts_repo_readonly_with_writable_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _docker_run_args(tmp_path, monkeypatch)
    # The repo mount is :ro; a separate scratch mount is read-write.
    mount_args = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    repo_mount = next(m for m in mount_args if m.endswith("/work:ro"))
    scratch_mount = next(m for m in mount_args if "/scratch" in m)
    assert ":ro" not in scratch_mount
    assert repo_mount.startswith(str(tmp_path.resolve()))


# Three malicious-patch class containment tests (per 7gu acceptance):


def test_contains_filesystem_writes_outside_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A patch that writes to /etc would either land on the read-only
    repo mount or be denied by the rootless user. The hardening flags
    that contain this class are: read-only repo mount + non-root user.
    Verifying both are set proves the containment is in effect."""
    args = _docker_run_args(tmp_path, monkeypatch)
    assert any(
        a.endswith("/work:ro") for a in args
    ), "repo mount must be :ro to block /etc-style writes via the source tree"
    assert "--user" in args
    assert args[args.index("--user") + 1] == "1000:1000"


def test_contains_dns_exfiltration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A patch that opens an outbound connection (DNS / curl) is contained
    by --network none. With no network namespace, the container cannot
    resolve a hostname or reach an exfil endpoint."""
    args = _docker_run_args(tmp_path, monkeypatch)
    idx = args.index("--network")
    assert args[idx + 1] == "none"


def test_contains_setuid_escalation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A patch that drops a setuid binary cannot escalate because
    --security-opt=no-new-privileges plus --cap-drop=ALL means setuid
    bits cannot grant capabilities the container does not already
    have (and it has none)."""
    args = _docker_run_args(tmp_path, monkeypatch)
    sec_opts = [args[i + 1] for i, a in enumerate(args) if a == "--security-opt"]
    drops = [args[i + 1] for i, a in enumerate(args) if a == "--cap-drop"]
    assert "no-new-privileges:true" in sec_opts
    assert "ALL" in drops


# Recipe / config opt-ins:


def test_policy_network_pull_requires_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="network_allowlist"):
        SandboxPolicy(network="pull")


def test_policy_network_pull_emits_allowlist_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = SandboxPolicy(
        network="pull",
        network_allowlist=("registry-1.docker.io", "proxy.golang.org"),
    )
    args = _docker_run_args(tmp_path, monkeypatch, policy=policy)
    labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
    # network=pull does NOT set --network none; the allowlist is recorded
    # as a label for auditability.
    assert "--network" not in args or args[args.index("--network") + 1] != "none"
    assert any("registry-1.docker.io" in label for label in labels)
    assert any("proxy.golang.org" in label for label in labels)


def test_policy_cap_add_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A recipe that needs SYS_PTRACE (e.g. a tracing-based test runner)
    can opt back in to that capability without abandoning the rest."""
    policy = SandboxPolicy(cap_add=("SYS_PTRACE",))
    args = _docker_run_args(tmp_path, monkeypatch, policy=policy)
    drops = [args[i + 1] for i, a in enumerate(args) if a == "--cap-drop"]
    adds = [args[i + 1] for i, a in enumerate(args) if a == "--cap-add"]
    assert "ALL" in drops
    assert "SYS_PTRACE" in adds


def test_build_sandbox_adapter_passes_policy_through(tmp_path: Path) -> None:
    """The factory reads `sandbox_policy` from adapters_cfg and threads
    it into the adapter."""
    adapter = build_sandbox_adapter(
        repo_path=tmp_path,
        adapters_cfg={
            "sandbox_provider": "docker",
            "sandbox_policy": {
                "network": "pull",
                "network_allowlist": ["proxy.golang.org"],
                "cap_add": ["NET_BIND_SERVICE"],
            },
        },
        cassette_dir=None,
    )
    assert isinstance(adapter, DockerSandboxAdapter)
    assert adapter._policy.network == "pull"
    assert adapter._policy.cap_add == ("NET_BIND_SERVICE",)


def test_repo_mount_readonly_can_be_disabled_for_legacy_recipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some recipes mutate the repo in-place (e.g. mvn writing target/).
    They opt out of the read-only mount; the rest of the hardening
    stays."""
    policy = SandboxPolicy(repo_mount_readonly=False)
    args = _docker_run_args(tmp_path, monkeypatch, policy=policy)
    mount_args = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    repo_mount = next(m for m in mount_args if m.endswith("/work"))
    assert ":ro" not in repo_mount
    # Hardening still applies even when the repo mount is rw.
    assert "--network" in args
    assert "no-new-privileges:true" in [
        args[i + 1] for i, a in enumerate(args) if a == "--security-opt"
    ]


# ---------------------------------------------------------------------------
# Egress filter for network='pull' (cxa)
# ---------------------------------------------------------------------------
#
# When network='pull' the adapter must do real egress filtering, not just
# slap a label on the workload. Approach: per-sandbox `--internal` docker
# network with no host route + an HTTP CONNECT proxy sidecar attached to
# both the internal network and the default bridge. Workload only sees
# the internal network and is forced through HTTP_PROXY/HTTPS_PROXY at the
# sidecar. The proxy enforces the allowlist; the `--internal` flag means
# even raw-socket attempts can't escape.


def _create_with_recorder(
    tmp_path: Path,
    monkeypatch,
    *,
    policy=None,
    response_count: int = 8,
) -> tuple[DockerSandboxAdapter, _Recorder, str]:
    """Run create_sandbox with enough stub responses for any backend path.

    Returns the adapter, the recorder (so callers can inspect calls), and
    the sandbox id. The stub returns short ids for every subprocess call
    so network-create / proxy-run / workload-run all succeed.
    """
    responses = [_StubProc(stdout=f"id-{i}\n") for i in range(response_count)]
    recorder = _Recorder(responses)
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path, policy=policy)
    sandbox_id = adapter.create_sandbox(image="build-sandbox:latest")
    return adapter, recorder, sandbox_id


def _calls_with_subcommand(recorder: _Recorder, *prefix: str) -> list[list[str]]:
    """Return docker-CLI invocations whose argv starts with ``prefix``.

    e.g. ``_calls_with_subcommand(rec, "docker", "network", "create")``.
    """
    out: list[list[str]] = []
    for call in recorder.calls:
        args = call["args"]
        if list(args[: len(prefix)]) == list(prefix):
            out.append(args)
    return out


def test_pull_creates_internal_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`docker network create --internal` runs before the workload starts.

    `--internal` is the load-bearing flag: it disables NAT/route to the
    host so even `curl --noproxy '*'` cannot reach the outside world.
    """
    policy = SandboxPolicy(network="pull", network_allowlist=("registry-1.docker.io",))
    _, recorder, _ = _create_with_recorder(tmp_path, monkeypatch, policy=policy)
    nets = _calls_with_subcommand(recorder, "docker", "network", "create")
    assert nets, "expected `docker network create` for the per-sandbox network"
    assert "--internal" in nets[0], (
        "the per-sandbox network must be --internal so the workload has no "
        "direct egress; the proxy sidecar is the only escape hatch"
    )


def test_pull_starts_proxy_sidecar_on_two_networks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proxy sidecar attaches to BOTH the internal sandbox network
    (so the workload can reach it) AND the default bridge (so it has
    egress to the real internet)."""
    policy = SandboxPolicy(
        network="pull",
        network_allowlist=("registry-1.docker.io",),
        proxy_image="my-proxy:1.0",
    )
    _, recorder, _ = _create_with_recorder(tmp_path, monkeypatch, policy=policy)
    runs = _calls_with_subcommand(recorder, "docker", "run")
    proxy_runs = [r for r in runs if "my-proxy:1.0" in r]
    assert proxy_runs, "expected a docker-run for the proxy image"
    proxy_run = proxy_runs[0]
    # Sidecar starts on the internal sandbox network (`--network <name>`)
    # and is then connected to the default bridge via `docker network
    # connect bridge <sidecar>`. Verify the connect call exists.
    connects = _calls_with_subcommand(recorder, "docker", "network", "connect")
    assert any(
        "bridge" in c for c in connects
    ), "proxy sidecar must be connected to the default bridge for egress"
    # Sidecar is detached so the adapter can return.
    assert "-d" in proxy_run


def test_pull_proxy_sidecar_is_hardened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy sidecar must apply the same baseline hardening as the
    workload (91m): ``--cap-drop=ALL``, ``--security-opt
    no-new-privileges:true``, and a non-root ``--user``. Without these
    a tinyproxy memory-safety CVE could pivot from the sidecar (which
    is bridged to the default network for outbound egress) into the
    host. The sidecar runs the proxy on a non-privileged port so
    dropping root is safe.

    Defense-in-depth (0ez): the sidecar root filesystem is also mounted
    ``--read-only`` with a tmpfs at ``/tmp`` for tinyproxy's pid/log
    files, and ``--pids-limit`` caps fork bombs from a compromised
    sidecar. The rendered tinyproxy.conf must therefore point ``PidFile``
    and ``LogFile`` at the tmpfs so the proxy can actually start under a
    read-only rootfs.
    """
    policy = SandboxPolicy(
        network="pull",
        network_allowlist=("registry-1.docker.io",),
        proxy_image="my-proxy:1.0",
    )
    adapter, recorder, _ = _create_with_recorder(tmp_path, monkeypatch, policy=policy)
    runs = _calls_with_subcommand(recorder, "docker", "run")
    proxy_run = next(r for r in runs if "my-proxy:1.0" in r)
    drops = [proxy_run[i + 1] for i, a in enumerate(proxy_run) if a == "--cap-drop"]
    sec_opts = [proxy_run[i + 1] for i, a in enumerate(proxy_run) if a == "--security-opt"]
    assert "ALL" in drops, "proxy sidecar must drop all capabilities"
    assert (
        "no-new-privileges:true" in sec_opts
    ), "proxy sidecar must set no-new-privileges so setuid bits cannot escalate"
    assert "--user" in proxy_run, "proxy sidecar must run as a non-root user"
    user_value = proxy_run[proxy_run.index("--user") + 1]
    # Both UID and GID must be present and non-zero. GID 0 is the root
    # group, which still grants read access to root-owned files via group
    # permissions, so checking only the UID would let "65534:0" through.
    # We also assert the value matches PROXY_SIDECAR_USER so the test
    # fails loudly if someone changes the constant without updating here.
    from migration_evals.adapters_docker import (  # noqa: E402
        PROXY_SIDECAR_PIDS_LIMIT,
        PROXY_SIDECAR_TMPFS_MOUNT,
        PROXY_SIDECAR_USER,
    )

    assert user_value == PROXY_SIDECAR_USER, (
        f"proxy sidecar --user must equal PROXY_SIDECAR_USER ({PROXY_SIDECAR_USER!r}); "
        f"got {user_value!r}"
    )
    parts = user_value.split(":", 1)
    assert len(parts) == 2 and all(
        p.isdigit() and int(p) != 0 for p in parts
    ), f"proxy sidecar --user must be 'UID:GID' with both non-zero numeric; got {user_value!r}"

    # --read-only is a bare flag (no value). Drops the writable rootfs
    # so an attacker with arbitrary write inside the sidecar cannot
    # persist binaries or tamper with /etc.
    assert (
        "--read-only" in proxy_run
    ), "proxy sidecar rootfs must be --read-only so a tinyproxy CVE cannot write outside the tmpfs"

    # --tmpfs /tmp:size=...,mode=1777 is the only writable path. Assert
    # the mount target and that a size cap is present (so a process
    # cannot exhaust host memory by filling the tmpfs).
    tmpfs_mounts = [proxy_run[i + 1] for i, a in enumerate(proxy_run) if a == "--tmpfs"]
    assert tmpfs_mounts, "proxy sidecar must mount a tmpfs for tinyproxy's pid/log files"
    assert (
        PROXY_SIDECAR_TMPFS_MOUNT in tmpfs_mounts
    ), f"proxy sidecar must mount {PROXY_SIDECAR_TMPFS_MOUNT!r}; got {tmpfs_mounts!r}"
    tmp_mount = next(m for m in tmpfs_mounts if m.startswith("/tmp"))
    assert "size=" in tmp_mount, (
        f"proxy sidecar /tmp tmpfs must set a size cap so a process cannot "
        f"exhaust host memory; got {tmp_mount!r}"
    )

    # --pids-limit must be a positive integer. Caps fork bombs from a
    # compromised sidecar; kernel default is millions of pids.
    assert "--pids-limit" in proxy_run, "proxy sidecar must set --pids-limit to cap fork bombs"
    pids_limit_value = proxy_run[proxy_run.index("--pids-limit") + 1]
    assert pids_limit_value == str(PROXY_SIDECAR_PIDS_LIMIT), (
        f"proxy sidecar --pids-limit must equal PROXY_SIDECAR_PIDS_LIMIT "
        f"({PROXY_SIDECAR_PIDS_LIMIT}); got {pids_limit_value!r}"
    )
    assert (
        pids_limit_value.isdigit() and int(pids_limit_value) > 0
    ), f"proxy sidecar --pids-limit must be a positive int; got {pids_limit_value!r}"

    # Rendered tinyproxy.conf must redirect PidFile and LogFile to the
    # tmpfs at /tmp; otherwise the sidecar crashes on startup under a
    # read-only rootfs (default paths land in /var/run or /var/log).
    rendered_conf = adapter._render_proxy_config(allow_cidr="10.0.0.0/24")
    assert re.search(
        r'^PidFile\s+"?/tmp/', rendered_conf, re.MULTILINE
    ), "tinyproxy.conf must point PidFile at /tmp so it lands in the tmpfs"
    assert re.search(
        r'^LogFile\s+"?/tmp/', rendered_conf, re.MULTILINE
    ), "tinyproxy.conf must point LogFile at /tmp so it lands in the tmpfs"


def test_pull_workload_uses_internal_network_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workload `docker run` has `--network <sandbox-net>` (NOT bridge,
    NOT none)."""
    policy = SandboxPolicy(network="pull", network_allowlist=("registry-1.docker.io",))
    _, recorder, _ = _create_with_recorder(tmp_path, monkeypatch, policy=policy)
    runs = _calls_with_subcommand(recorder, "docker", "run")
    workload_runs = [r for r in runs if "build-sandbox:latest" in r]
    assert len(workload_runs) == 1
    workload = workload_runs[0]
    assert "--network" in workload
    netname = workload[workload.index("--network") + 1]
    assert netname != "none"
    assert netname != "bridge"
    # The network name is the same one created via `docker network create`.
    nets = _calls_with_subcommand(recorder, "docker", "network", "create")
    assert nets and netname in nets[0]


def test_pull_injects_proxy_env_into_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workload gets HTTP_PROXY/HTTPS_PROXY (and lower-case variants)
    pointing at the sidecar by DNS name on the internal network."""
    policy = SandboxPolicy(
        network="pull",
        network_allowlist=("registry-1.docker.io",),
        proxy_port=3128,
    )
    _, recorder, _ = _create_with_recorder(tmp_path, monkeypatch, policy=policy)
    runs = _calls_with_subcommand(recorder, "docker", "run")
    workload = next(r for r in runs if "build-sandbox:latest" in r)
    # Pull every -e VAL pair off the argv.
    env_pairs: list[str] = []
    for i, a in enumerate(workload):
        if a == "-e":
            env_pairs.append(workload[i + 1])
    proxy_url = "http://proxy:3128"
    assert any(p == f"HTTP_PROXY={proxy_url}" for p in env_pairs)
    assert any(p == f"HTTPS_PROXY={proxy_url}" for p in env_pairs)
    assert any(p == f"http_proxy={proxy_url}" for p in env_pairs)
    assert any(p == f"https_proxy={proxy_url}" for p in env_pairs)
    # NO_PROXY keeps `proxy` itself reachable so the workload can talk to
    # the sidecar on the internal network.
    assert any(p.startswith("NO_PROXY=") and "proxy" in p for p in env_pairs)


def test_pull_proxy_config_writes_filter_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tinyproxy config mounted into the sidecar has FilterDefaultDeny
    Yes plus an anchored regex per allowlisted host, so unlisted hosts
    are dropped at the CONNECT layer."""
    policy = SandboxPolicy(
        network="pull",
        network_allowlist=("registry-1.docker.io", "proxy.golang.org"),
    )
    _, recorder, _ = _create_with_recorder(tmp_path, monkeypatch, policy=policy)
    # The adapter writes tinyproxy.conf + filter into a per-sandbox dir
    # and -v mounts the dir at /etc/tinyproxy. Locate the dir.
    runs = _calls_with_subcommand(recorder, "docker", "run")
    proxy_run = next(r for r in runs if DEFAULT_PROXY_IMAGE in r)  # default image
    mounts = [proxy_run[i + 1] for i, a in enumerate(proxy_run) if a == "-v"]
    conf_dir_mounts = [m for m in mounts if m.endswith(":/etc/tinyproxy:ro")]
    assert conf_dir_mounts, "expected /etc/tinyproxy bind-mount"
    host_dir = Path(conf_dir_mounts[0].split(":")[0])
    conf_text = (host_dir / "tinyproxy.conf").read_text(encoding="utf-8")
    filter_text = (host_dir / "filter").read_text(encoding="utf-8")
    assert "FilterDefaultDeny Yes" in conf_text
    assert "FilterExtended Yes" in conf_text
    # Each allowlisted host appears as an anchored regex with escaped dots
    # in the filter file. The regex is version-tolerant — matches both
    # bare 'host' (tinyproxy 1.11.0 strips the CONNECT port before
    # regex match) and 'host:port' (other builds retain it). See
    # test_anchored_host_regex_tolerates_port_suffix below.
    matched = any(
        re.compile(line).match("registry-1.docker.io")
        and re.compile(line).match("registry-1.docker.io:443")
        for line in filter_text.splitlines()
        if line
    )
    assert matched, (
        f"no filter line matches both 'registry-1.docker.io' and "
        f"'registry-1.docker.io:443': {filter_text!r}"
    )


def test_anchored_host_regex_tolerates_port_suffix() -> None:
    """The Allow regex must match BOTH 'host' and 'host:port' forms.

    tinyproxy CONNECT-target filtering varies by build: 1.11.0
    (the historical vimagick/tinyproxy image) and 1.11.2 (the
    current kalaksi/tinyproxy image) both strip the ':port'
    suffix before matching the Filter regex (a CONNECT to
    example.com:443 is matched against the bare string
    "example.com"); other builds documented in the tinyproxy
    issue tracker retain the suffix. The generated regex must
    be tolerant of both so an allowlisted host is not silently
    denied on a version skew.
    """
    pattern = re.compile(DockerSandboxAdapter._anchored_host_regex("example.com"))
    assert pattern.match("example.com"), "must match bare host"
    assert pattern.match("example.com:443"), "must match host:443"
    assert pattern.match("example.com:80"), "must match host:80"
    assert pattern.match("example.com:8080"), "must match arbitrary port"
    # Anchoring must still hold: no prefix/suffix smuggling.
    assert not pattern.match("evil.com"), "must not match unrelated host"
    assert not pattern.match("evil-example.com"), "must not match prefix"
    assert not pattern.match("example.com.evil.com"), "must not match suffix"
    assert not pattern.match("example.com:443x"), "must reject trailing junk"
    assert not pattern.match("example.com:abc"), "port must be numeric"


def test_pull_destroy_removes_proxy_and_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """destroy_sandbox must tear down workload, proxy, and the per-sandbox
    network (in that order; networks can't be removed while containers
    are attached)."""
    policy = SandboxPolicy(network="pull", network_allowlist=("registry-1.docker.io",))
    adapter, recorder, sandbox_id = _create_with_recorder(
        tmp_path, monkeypatch, policy=policy, response_count=16
    )
    pre_destroy = len(recorder.calls)
    adapter.destroy_sandbox(sandbox_id)
    after = recorder.calls[pre_destroy:]
    rm_calls = [c["args"] for c in after if c["args"][:2] == ["docker", "rm"]]
    netrm_calls = [c["args"] for c in after if c["args"][:3] == ["docker", "network", "rm"]]
    assert len(rm_calls) >= 2, "expected workload + proxy removed"
    assert netrm_calls, "expected per-sandbox network removed"
    # Network removal must come AFTER all container removals.
    last_rm_idx = max(i for i, c in enumerate(after) if c["args"][:2] == ["docker", "rm"])
    first_netrm_idx = min(
        i for i, c in enumerate(after) if c["args"][:3] == ["docker", "network", "rm"]
    )
    assert (
        first_netrm_idx > last_rm_idx
    ), "must remove containers before the network they're attached to"


def test_network_none_is_unaffected_by_egress_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The egress-filter machinery must not fire for network='none' (the
    default). The workload should still be a single docker-run with
    --network none and no proxy sidecar."""
    recorder = _Recorder([_StubProc(stdout="cid\n")])
    monkeypatch.setattr(subprocess, "run", recorder)
    adapter = DockerSandboxAdapter(tmp_path)  # default policy: network=none
    adapter.create_sandbox(image="build-sandbox:latest")
    # Exactly one subprocess.run: the workload docker-run. No network
    # create, no proxy run.
    assert len(recorder.calls) == 1
    args = recorder.calls[0]["args"]
    assert args[:2] == ["docker", "run"]
    assert "--network" in args
    assert args[args.index("--network") + 1] == "none"


def test_pull_proxy_run_failure_cleans_up_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the proxy sidecar fails to start (e.g. image not pulled), the
    half-created per-sandbox network must be torn down so we don't leak
    docker resources across runs. The on-disk ``config_dir`` created by
    ``_setup_egress_filter`` must also be cleaned up via
    ``_cleanup_scratch`` so the proxy-failure path does not leak
    ``mig-eval-proxyconf-*`` directories alongside the half-built
    sandbox.
    """
    err = subprocess.CalledProcessError(
        returncode=125,
        cmd=["docker", "run"],
        output="",
        stderr="Unable to find image 'kalaksi/tinyproxy@sha256:...' locally",
    )
    recorder = _Recorder(
        [
            _StubProc(stdout="netid\n"),  # network create succeeds
            _StubProc(stdout="[]"),  # network inspect (returns no IPAM)
            err,  # proxy docker run FAILS
            _StubProc(returncode=0),  # network rm cleanup
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)

    # Spy on _cleanup_scratch (a @staticmethod) so we can assert that the
    # ExitStack-registered cleanup actually fires for the egress
    # config_dir on the proxy-failure path. We delegate to the real
    # implementation so the on-disk teardown still happens.
    cleanup_calls: list[Path] = []
    original_cleanup = DockerSandboxAdapter._cleanup_scratch

    def _spy_cleanup(path: Path) -> None:
        cleanup_calls.append(path)
        original_cleanup(path)

    # The ``staticmethod(...)`` wrapper is load-bearing: production code
    # registers the callback as ``stack.callback(self._cleanup_scratch, ...)``,
    # which goes through the descriptor protocol on the class. Patching the
    # class attribute with a bare function would cause Python to bind ``self``
    # as the first argument at attribute lookup, producing a TypeError when
    # the callback fires. Wrapping in ``staticmethod`` matches the original
    # decorator and bypasses binding so the spy receives only ``config_dir``.
    monkeypatch.setattr(DockerSandboxAdapter, "_cleanup_scratch", staticmethod(_spy_cleanup))

    policy = SandboxPolicy(network="pull", network_allowlist=("registry-1.docker.io",))
    adapter = DockerSandboxAdapter(tmp_path, policy=policy)
    with pytest.raises(RuntimeError):
        adapter.create_sandbox(image="build-sandbox:latest")
    # Last call must be the network teardown.
    netrms = _calls_with_subcommand(recorder, "docker", "network", "rm")
    assert netrms, "must clean up the per-sandbox network when proxy run fails"

    # The egress config_dir (mig-eval-proxyconf-<suffix>) is created on
    # disk by _setup_egress_filter and registered for cleanup via
    # ExitStack. On the proxy-run failure path the stack must unwind it,
    # not just the docker network.
    proxyconf_cleanups = [p for p in cleanup_calls if p.name.startswith("mig-eval-proxyconf-")]
    assert proxyconf_cleanups, (
        "must call _cleanup_scratch on the egress config_dir when proxy "
        f"run fails; saw cleanup calls: {cleanup_calls!r}"
    )
    # And the directory must actually be gone from disk afterwards
    # (i.e. the spy did not just record the call; the real cleanup ran).
    for config_dir in proxyconf_cleanups:
        assert not config_dir.exists(), f"egress config_dir was not removed from disk: {config_dir}"


def test_pull_setup_egress_filter_waits_for_proxy_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `docker exec <proxy> sh -c "<bounded nc -z loop>"` runs after the
    sidecar is bridged but before the workload starts.

    Wave-1 review (security MEDIUM #3): without this, a fast workload
    (go build, pip install) can race the proxy and hit connection-refused
    while tinyproxy is still binding its socket. The readiness probe
    closes that window before _setup_egress_filter returns control to
    create_sandbox.
    """
    policy = SandboxPolicy(
        network="pull",
        network_allowlist=("registry-1.docker.io",),
        proxy_port=8888,
    )
    _, recorder, _ = _create_with_recorder(tmp_path, monkeypatch, policy=policy)
    # The probe is a `docker exec <proxy_container> sh -c "<loop>"` and
    # must come AFTER `docker network connect bridge <proxy>` but BEFORE
    # the workload `docker run`.
    execs = _calls_with_subcommand(recorder, "docker", "exec")
    assert execs, "expected a docker exec call to probe proxy readiness"
    probe = execs[0]
    # argv shape: ["docker", "exec", <proxy_container>, "sh", "-c", "<loop>"]
    assert probe[3:5] == ["sh", "-c"], f"readiness probe must run via `sh -c`: {probe!r}"
    script = probe[5]
    # `nc -z 127.0.0.1 <port>` is the actual TCP probe. Bounded retry
    # loop (no `while true`): caps total wait at
    # PROXY_READINESS_ITERATIONS × PROXY_READINESS_SLEEP_S.
    assert (
        "nc -z 127.0.0.1 8888" in script
    ), f"readiness probe must use `nc -z 127.0.0.1 <port>`: {script!r}"
    assert "while true" not in script, "readiness loop must be bounded, not `while true`"

    # Ordering: probe runs after `network connect bridge` and before the
    # workload `docker run`.
    call_argv = [c["args"] for c in recorder.calls]
    connect_idx = next(
        i
        for i, a in enumerate(call_argv)
        if a[:3] == ["docker", "network", "connect"] and "bridge" in a
    )
    probe_idx = next(i for i, a in enumerate(call_argv) if a[:2] == ["docker", "exec"])
    workload_idx = next(
        i
        for i, a in enumerate(call_argv)
        if a[:2] == ["docker", "run"] and "build-sandbox:latest" in a
    )
    assert connect_idx < probe_idx < workload_idx, (
        "readiness probe must run AFTER `network connect bridge` and BEFORE "
        f"the workload `docker run`: connect={connect_idx} probe={probe_idx} "
        f"workload={workload_idx}"
    )


def test_pull_setup_egress_filter_raises_on_proxy_unready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the readiness probe exits non-zero (proxy never became ready),
    create_sandbox raises RuntimeError AND the ExitStack-registered
    teardowns unwind: the proxy sidecar is `docker rm -f`'d and the
    per-sandbox network is `docker network rm`'d, so we don't leak
    docker resources when the probe fails."""
    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["docker", "exec"],
        output="",
        stderr="",
    )
    recorder = _Recorder(
        [
            _StubProc(stdout="netid\n"),  # network create
            _StubProc(stdout="[]"),  # network inspect (no IPAM)
            _StubProc(stdout="proxy-cid\n"),  # proxy docker run
            _StubProc(stdout=""),  # network connect bridge
            err,  # readiness probe FAILS
            _StubProc(returncode=0),  # docker rm -f proxy (ExitStack unwind)
            _StubProc(returncode=0),  # docker network rm (ExitStack unwind)
        ]
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    policy = SandboxPolicy(network="pull", network_allowlist=("registry-1.docker.io",))
    adapter = DockerSandboxAdapter(tmp_path, policy=policy)
    with pytest.raises(RuntimeError, match=r"proxy-cid did not become ready on port 8888"):
        adapter.create_sandbox(image="build-sandbox:latest")
    # Both proxy container removal and network removal must appear -
    # ExitStack unwinds in reverse-registration order.
    rm_calls = _calls_with_subcommand(recorder, "docker", "rm", "-f")
    netrms = _calls_with_subcommand(recorder, "docker", "network", "rm")
    assert any(
        "proxy-cid" in r for r in rm_calls
    ), "proxy sidecar must be force-removed when readiness probe fails"
    assert netrms, "per-sandbox network must be removed when readiness probe fails"


# ---------------------------------------------------------------------------
# Live Docker integration for the egress filter (opt-in).
# ---------------------------------------------------------------------------
#
# Proves end-to-end that the proxy sidecar drops a non-allowlisted host
# and forwards an allowlisted one. Same gating as the existing live test;
# additionally requires the proxy image to be importable locally so we
# don't go pulling tinyproxy on every CI box.


_PROXY_IMAGE_FOR_INTEGRATION = os.environ.get("MIGRATION_EVAL_PROXY_IMAGE", DEFAULT_PROXY_IMAGE)


def _proxy_image_present() -> bool:
    if not _DOCKER_AVAILABLE:
        return False
    res = subprocess.run(
        ["docker", "image", "inspect", _PROXY_IMAGE_FOR_INTEGRATION],
        capture_output=True,
    )
    return res.returncode == 0


@pytest.mark.skipif(
    not (_DOCKER_AVAILABLE and _DOCKER_INTEGRATION and _proxy_image_present()),
    reason=(
        "set MIGRATION_EVAL_DOCKER_INTEGRATION=1 with Docker available and "
        "the proxy image (MIGRATION_EVAL_PROXY_IMAGE or DEFAULT_PROXY_IMAGE) "
        "present locally"
    ),
)
def test_live_egress_allowlist_enforced(tmp_path: Path) -> None:
    """End-to-end: allowlisted host succeeds; disallowed host is dropped."""
    (tmp_path / "noop").write_text("hi\n")
    policy = SandboxPolicy(
        network="pull",
        network_allowlist=("example.com",),
        proxy_image=_PROXY_IMAGE_FOR_INTEGRATION,
    )
    adapter = DockerSandboxAdapter(tmp_path, policy=policy)
    # Use a workload image that has curl. alpine + apk would need network;
    # curlimages/curl is purpose-built and small, but if not present skip.
    workload_image = os.environ.get("MIGRATION_EVAL_WORKLOAD_IMAGE", "curlimages/curl:latest")
    has_workload = (
        subprocess.run(
            ["docker", "image", "inspect", workload_image], capture_output=True
        ).returncode
        == 0
    )
    if not has_workload:
        pytest.skip(f"workload image {workload_image} not present locally")
    sid = adapter.create_sandbox(image=workload_image)
    try:
        # Allowlisted host -> proxy lets it through.
        ok = adapter.exec(
            sid,
            command="curl -sSf -o /dev/null -w '%{http_code}' https://example.com/",
            timeout_s=30,
        )
        assert ok["exit_code"] == 0, f"allowlisted host should succeed: {ok}"
        # Disallowed host -> proxy should drop the CONNECT, exit nonzero.
        bad = adapter.exec(
            sid,
            command="curl -sSf -o /dev/null https://www.google.com/",
            timeout_s=30,
        )
        assert bad["exit_code"] != 0, f"disallowed host must fail: {bad}"
    finally:
        adapter.destroy_sandbox(sid)
