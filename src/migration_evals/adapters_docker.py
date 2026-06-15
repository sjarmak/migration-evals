"""Docker-backed :class:`~migration_evals.adapters.SandboxAdapter`.

POC implementation for vj9.4. Runs the recipe's build and test commands
inside an ephemeral Docker container with the repo mounted at a
configurable workdir. See ``docs/tier0_integration_notes.md`` for why
this is the right next step before a production executor-backed
implementation.

Design
------
Containers are persistent across exec calls within one ``create_sandbox``
lifetime. That matches how :mod:`migration_evals.oracles.tier1_compile`
and :mod:`migration_evals.oracles.tier2_tests` use the Protocol
(create -> exec -> destroy) and lets a single adapter instance be reused
across tiers without re-mounting the repo each time. The container is
kept alive with ``tail -f /dev/null``; ``docker rm -f`` in
:meth:`destroy_sandbox` stops and removes it.

On exec timeout the container is force-killed with ``docker kill`` so
the inner process dies alongside the ``docker exec`` CLI - otherwise the
process would continue running inside the container after the Python-
side subprocess raises ``TimeoutExpired``.

The factory :func:`build_sandbox_adapter` centralises the choice between
this Docker backend and the cassette-replay stand-in, so that the CLI
and :mod:`migration_evals.runner` share one decision point.
"""

from __future__ import annotations

import atexit
import contextlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

from migration_evals.adapters_docker_egress import EgressFilter, EgressFilterManager
from migration_evals.sandbox_policy import SandboxPolicy

__all__ = ["DockerSandboxAdapter", "build_sandbox_adapter"]


DEFAULT_DOCKER_BIN = "docker"
DEFAULT_WORKDIR = "/work"


class DockerSandboxAdapter:
    """SandboxAdapter that runs commands inside a real Docker container.

    Hardened by default (7gu): ``--network none``, ``--cap-drop=ALL``,
    ``--security-opt=no-new-privileges``, ``--user 1000:1000``, repo
    mount ``ro``, and a writable scratch volume that build commands use
    for output. Override per-trial via ``SandboxPolicy``.

    Parameters
    ----------
    repo_path
        Host path of the checked-out repo. Mounted read-only at
        ``workdir`` by default (writes redirected to the per-sandbox
        scratch volume). Set ``policy.repo_mount_readonly = False`` to
        restore the legacy read-write mount.
    docker_bin
        Name of the docker CLI on ``$PATH``.
    workdir
        Absolute path inside the container used as the mount point and
        the working directory for every ``exec`` call.
    policy
        :class:`SandboxPolicy` controlling the security flags. Defaults
        to :meth:`SandboxPolicy.hardened_default`.
    """

    def __init__(
        self,
        repo_path: Path,
        *,
        docker_bin: str = DEFAULT_DOCKER_BIN,
        workdir: str = DEFAULT_WORKDIR,
        policy: SandboxPolicy | None = None,
    ) -> None:
        self._repo_path = Path(repo_path).resolve()
        self._docker_bin = docker_bin
        self._workdir = workdir
        self._policy = policy or SandboxPolicy.hardened_default()
        # Per-sandbox scratch directories (host path) so writes from a
        # read-only repo mount have somewhere to land. Cleaned up in
        # destroy_sandbox.
        self._containers: dict[str, str] = {}
        self._scratch_dirs: dict[str, Path] = {}
        # Per-sandbox egress-filter artefacts (only populated for
        # network='pull'). Keyed by sandbox id so destroy_sandbox can
        # tear them down in the right order.
        self._egress: dict[str, EgressFilter] = {}
        # Delegate the network='pull' egress concern (per-sandbox internal
        # network + hardened proxy sidecar) to a dedicated manager so this
        # adapter stays focused on container/scratch lifecycle.
        self._egress_mgr = EgressFilterManager(docker_bin=docker_bin, policy=self._policy)
        # Crash-safety net: atexit fires for normal interpreter shutdown
        # paths and SIGTERM-via-handler. Errors are swallowed inside the
        # handler — at shutdown the docker daemon may be gone or the
        # user is escalating to SIGKILL anyway, and a traceback from
        # atexit only obscures the real cause of exit.
        atexit.register(self._atexit_teardown)

    # ------------------------------------------------------------------
    # Crash-safe teardown (context manager + atexit)
    # ------------------------------------------------------------------

    def __enter__(self) -> DockerSandboxAdapter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Never suppress — return None so any in-flight exception
        # propagates after cleanup.
        self._teardown_all()

    def _teardown_all(self) -> None:
        """Destroy every still-tracked sandbox.

        Idempotent by construction: ``destroy_sandbox`` pops each entry
        from the tracking dicts, so a second call iterates an empty
        snapshot and is a no-op. This means ``__exit__`` followed by
        atexit (or two ``__exit__`` calls) issue exactly one teardown
        per sandbox.

        After teardown, the per-instance atexit callback is unregistered
        so the adapter can be garbage-collected and per-loop callers
        (``runner.py`` builds a new adapter per repo) don't accumulate
        phantom callbacks pinning every previous instance until exit.
        """
        for sandbox_id in list(self._containers.keys()):
            self.destroy_sandbox(sandbox_id)
        atexit.unregister(self._atexit_teardown)

    def _atexit_teardown(self) -> None:
        """Atexit-safe variant of :meth:`_teardown_all` that never raises.

        Interpreter shutdown is the wrong time to discover the docker
        daemon has gone away; let the OS reap whatever is left rather
        than spew tracebacks on top of whatever already killed us.
        Errors are logged to stderr (best-effort) so a teardown bug is
        still visible in CI logs without bringing down the interpreter.
        """
        try:
            self._teardown_all()
        except BaseException as exc:  # noqa: BLE001 — atexit must not raise
            with contextlib.suppress(Exception):
                print(
                    f"DockerSandboxAdapter: atexit teardown failed: {exc!r}",
                    file=sys.stderr,
                )

    # ------------------------------------------------------------------
    # SandboxAdapter Protocol
    # ------------------------------------------------------------------

    def create_sandbox(
        self,
        *,
        image: str,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Start a detached, hardened container and return its id.

        All preparation steps (scratch dir, egress filter, workload run)
        register their teardown on a single ``ExitStack`` so any failure
        unwinds atomically — the caller never sees a leaked scratch dir
        or half-built egress filter. ``stack.pop_all()`` on success
        promotes ownership of the resources to the per-sandbox state
        dicts that ``destroy_sandbox`` consumes.
        """
        with contextlib.ExitStack() as stack:
            scratch_host = Path(tempfile.mkdtemp(prefix="mig-eval-scratch-"))
            stack.callback(self._cleanup_scratch, scratch_host)

            # If the policy opts in to network='pull', stand up the per-
            # sandbox internal network and HTTP CONNECT proxy sidecar
            # BEFORE the workload runs, so the workload can be attached
            # to the internal network with HTTP_PROXY env vars in one
            # shot.
            egress: EgressFilter | None = None
            if self._policy.network == "pull":
                egress = self._egress_mgr.setup(scratch_host)
                stack.callback(self._egress_mgr.teardown, egress)

            args = self._build_workload_run_argv(
                image=image,
                scratch_host=scratch_host,
                egress=egress,
                env=env,
            )
            container_id = self._run_workload(args)

            # Success: keep scratch + egress alive past the `with` and
            # transfer ownership to the per-sandbox state dicts.
            stack.pop_all()

        self._containers[container_id] = container_id
        self._scratch_dirs[container_id] = scratch_host
        if egress is not None:
            self._egress[container_id] = egress
        return container_id

    def _build_workload_run_argv(
        self,
        *,
        image: str,
        scratch_host: Path,
        egress: EgressFilter | None,
        env: Mapping[str, str] | None,
    ) -> list[str]:
        """Compose the full ``docker run`` argv for the workload container.

        Pure argv construction — no IO. Splitting this out keeps
        ``create_sandbox`` focused on lifecycle/cleanup and lets the
        argv-shape tests in :mod:`tests.test_adapters_docker` exercise
        the policy translation in isolation if needed.
        """
        repo_mount_flag = (
            f"{self._repo_path}:{self._workdir}:ro"
            if self._policy.repo_mount_readonly
            else f"{self._repo_path}:{self._workdir}"
        )
        args = [
            self._docker_bin,
            "run",
            "-d",
            "--rm",
            "-v",
            repo_mount_flag,
            "-v",
            f"{scratch_host}:{self._policy.scratch_dir}",
            "-w",
            self._workdir,
        ]
        # Network isolation. 'none' has no namespace at all. 'pull' goes
        # on a per-sandbox `--internal` bridge (no host route) and is
        # forced through the proxy sidecar via HTTP_PROXY env vars - so
        # the workload only reaches allowlisted hosts even if it tries
        # to use raw sockets.
        if self._policy.network == "none":
            args.extend(["--network", "none"])
        elif self._policy.network == "pull":
            if egress is None:
                raise RuntimeError("egress filter was not set up for network='pull' branch")
            args.extend(["--network", egress.network_name])
            # Keep the audit label so existing log analyzers can still
            # see what the trial was permitted to reach.
            for host in self._policy.network_allowlist:
                args.extend(["--label", f"migration-eval.network-allowlist={host}"])
        # Privilege isolation.
        if self._policy.no_new_privileges:
            args.extend(["--security-opt", "no-new-privileges:true"])
        # Capability set: drop everything by default, optionally add
        # back the specific capabilities the recipe needs.
        for cap in self._policy.cap_drop:
            args.extend(["--cap-drop", cap])
        for cap in self._policy.cap_add:
            args.extend(["--cap-add", cap])
        if self._policy.user:
            args.extend(["--user", self._policy.user])
        # Workload env: caller-supplied env first, then the proxy env
        # for network='pull'. We deliberately set proxy vars LAST so a
        # caller cannot accidentally point HTTP_PROXY at a different
        # host and bypass the allowlist.
        for key, value in (env or {}).items():
            args.extend(["-e", f"{key}={value}"])
        if egress is not None:
            for key, value in self._egress_mgr.proxy_env_vars().items():
                args.extend(["-e", f"{key}={value}"])
        args.extend([image, "tail", "-f", "/dev/null"])
        return args

    def _run_workload(self, args: list[str]) -> str:
        """Execute the workload ``docker run`` and return its container id.

        Raises ``RuntimeError`` on a non-zero exit or an empty container
        id; callers rely on the surrounding ``ExitStack`` to unwind any
        partially-built sandbox state.
        """
        try:
            completed = subprocess.run(args, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"docker run failed (exit={exc.returncode}): {exc.stderr.strip()}"
            ) from exc
        container_id = completed.stdout.strip()
        if not container_id:
            raise RuntimeError("docker run produced empty container id")
        return container_id

    @staticmethod
    def _cleanup_scratch(path: Path) -> None:
        """Best-effort recursive removal of a scratch directory."""
        shutil.rmtree(path, ignore_errors=True)

    def exec(
        self,
        sandbox_id: str,
        *,
        command: str,
        timeout_s: int = 600,
    ) -> Mapping[str, Any]:
        """Execute ``command`` inside the sandbox via ``sh -c``."""
        container_id = self._containers[sandbox_id]
        args = [self._docker_bin, "exec", container_id, "sh", "-c", command]
        try:
            completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            # Stop the container so the inner process dies too - otherwise
            # the build/test command keeps consuming resources after we
            # have given up waiting for it.
            subprocess.run(
                [self._docker_bin, "kill", container_id],
                capture_output=True,
                check=False,
            )
            stdout = (
                exc.stdout
                if isinstance(exc.stdout, str)
                else (exc.stdout.decode("utf-8", "replace") if exc.stdout else "")
            )
            stderr = (
                exc.stderr
                if isinstance(exc.stderr, str)
                else (exc.stderr.decode("utf-8", "replace") if exc.stderr else "")
            )
            return {
                "exit_code": 124,  # conventional 'timed out' exit code
                "stdout": stdout,
                "stderr": f"{stderr}\ntimeout after {timeout_s}s".lstrip(),
            }
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    def destroy_sandbox(self, sandbox_id: str) -> None:
        """Force-remove the container and clean up scratch.

        For network='pull' sandboxes, also tear down the proxy sidecar
        and the per-sandbox internal network. Order: workload first
        (frees the network endpoint), then proxy + network.
        """
        container_id = self._containers.pop(sandbox_id, None)
        scratch = self._scratch_dirs.pop(sandbox_id, None)
        egress = self._egress.pop(sandbox_id, None)
        if container_id is None:
            if scratch is not None:
                self._cleanup_scratch(scratch)
            if egress is not None:
                self._egress_mgr.teardown(egress)
            return
        # check=False: a missing-container error from Docker should not
        # mask the caller's real outcome.
        subprocess.run(
            [self._docker_bin, "rm", "-f", container_id],
            capture_output=True,
            check=False,
        )
        if egress is not None:
            self._egress_mgr.teardown(egress)
        if scratch is not None:
            self._cleanup_scratch(scratch)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_sandbox_adapter(
    *,
    repo_path: Path,
    adapters_cfg: Mapping[str, Any],
    cassette_dir: Path | None,
) -> Any:
    """Pick the sandbox adapter implied by ``adapters_cfg``.

    Config key ``sandbox_provider`` selects the backend:

    * ``"cassette"`` (default) - the replay-cassette stand-in from
      :mod:`migration_evals.cli`. Preserves existing smoke-config
      behaviour when no provider is set.
    * ``"docker"`` - :class:`DockerSandboxAdapter` with the repo mounted
      at ``adapters.docker_workdir`` (default ``/work``).

    ``repo_path`` is required because both stand-ins are per-repo
    instances.
    """
    provider = (adapters_cfg.get("sandbox_provider") or "cassette").lower()

    if provider == "cassette":
        # Imported here to avoid a top-level cycle: cli.py already
        # imports from migration_evals and would otherwise pull this
        # module in at import time.
        from migration_evals.adapters_cassette import CassetteSandboxAdapter

        return CassetteSandboxAdapter(Path(repo_path).name, cassette_dir)

    if provider == "docker":
        policy_cfg = adapters_cfg.get("sandbox_policy")
        policy = SandboxPolicy.from_dict(policy_cfg)
        return DockerSandboxAdapter(
            repo_path,
            docker_bin=adapters_cfg.get("docker_bin", DEFAULT_DOCKER_BIN),
            workdir=adapters_cfg.get("docker_workdir", DEFAULT_WORKDIR),
            policy=policy,
        )

    raise ValueError(f"unknown sandbox_provider {provider!r}; expected 'cassette' or 'docker'")
