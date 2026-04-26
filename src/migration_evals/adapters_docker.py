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

import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

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
        policy: Optional[SandboxPolicy] = None,
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

    # ------------------------------------------------------------------
    # SandboxAdapter Protocol
    # ------------------------------------------------------------------

    def create_sandbox(
        self,
        *,
        image: str,
        env: Optional[Mapping[str, str]] = None,
        cassette: Optional[Any] = None,
    ) -> str:
        """Start a detached, hardened container and return its id."""
        scratch_host = Path(tempfile.mkdtemp(prefix="mig-eval-scratch-"))
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
        # Network isolation - 'none' means no network namespace; 'pull'
        # means default bridge with the recipe's allowlist enforced
        # outside the container (DNS / proxy config), so this layer
        # records the allowlist on the container as a label for
        # auditability.
        if self._policy.network == "none":
            args.extend(["--network", "none"])
        elif self._policy.network == "pull":
            for host in self._policy.network_allowlist:
                args.extend(
                    ["--label", f"migration-eval.network-allowlist={host}"]
                )
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
        for key, value in (env or {}).items():
            args.extend(["-e", f"{key}={value}"])
        args.extend([image, "tail", "-f", "/dev/null"])

        try:
            completed = subprocess.run(
                args, check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError as exc:
            # Don't leak the scratch dir if docker run failed before
            # the container ever started.
            self._cleanup_scratch(scratch_host)
            raise RuntimeError(
                f"docker run failed (exit={exc.returncode}): {exc.stderr.strip()}"
            ) from exc

        container_id = completed.stdout.strip()
        if not container_id:
            self._cleanup_scratch(scratch_host)
            raise RuntimeError("docker run produced empty container id")
        self._containers[container_id] = container_id
        self._scratch_dirs[container_id] = scratch_host
        return container_id

    @staticmethod
    def _cleanup_scratch(path: Path) -> None:
        """Best-effort recursive removal of a scratch directory."""
        try:
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        except Exception:  # pragma: no cover - defensive
            pass

    def exec(
        self,
        sandbox_id: str,
        *,
        command: str,
        timeout_s: int = 600,
        cassette: Optional[Any] = None,
    ) -> Mapping[str, Any]:
        """Execute ``command`` inside the sandbox via ``sh -c``."""
        container_id = self._containers[sandbox_id]
        args = [self._docker_bin, "exec", container_id, "sh", "-c", command]
        try:
            completed = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            # Stop the container so the inner process dies too - otherwise
            # the build/test command keeps consuming resources after we
            # have given up waiting for it.
            subprocess.run(
                [self._docker_bin, "kill", container_id],
                capture_output=True,
                check=False,
            )
            stdout = exc.stdout if isinstance(exc.stdout, str) else (
                exc.stdout.decode("utf-8", "replace") if exc.stdout else ""
            )
            stderr = exc.stderr if isinstance(exc.stderr, str) else (
                exc.stderr.decode("utf-8", "replace") if exc.stderr else ""
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
        """Force-remove the container and clean up scratch."""
        container_id = self._containers.pop(sandbox_id, None)
        scratch = self._scratch_dirs.pop(sandbox_id, None)
        if container_id is None:
            if scratch is not None:
                self._cleanup_scratch(scratch)
            return
        # check=False: a missing-container error from Docker should not
        # mask the caller's real outcome.
        subprocess.run(
            [self._docker_bin, "rm", "-f", container_id],
            capture_output=True,
            check=False,
        )
        if scratch is not None:
            self._cleanup_scratch(scratch)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_sandbox_adapter(
    *,
    repo_path: Path,
    adapters_cfg: Mapping[str, Any],
    cassette_dir: Optional[Path],
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
        from migration_evals.cli import _CassetteSandboxAdapter

        return _CassetteSandboxAdapter(Path(repo_path).name, cassette_dir)

    if provider == "docker":
        policy_cfg = adapters_cfg.get("sandbox_policy")
        policy = SandboxPolicy.from_dict(policy_cfg)
        return DockerSandboxAdapter(
            repo_path,
            docker_bin=adapters_cfg.get("docker_bin", DEFAULT_DOCKER_BIN),
            workdir=adapters_cfg.get("docker_workdir", DEFAULT_WORKDIR),
            policy=policy,
        )

    raise ValueError(
        f"unknown sandbox_provider {provider!r}; expected 'cassette' or 'docker'"
    )
