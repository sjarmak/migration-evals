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

import contextlib
import json
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from migration_evals.sandbox_policy import SandboxPolicy

__all__ = ["DockerSandboxAdapter", "build_sandbox_adapter"]


DEFAULT_DOCKER_BIN = "docker"
DEFAULT_WORKDIR = "/work"
# DNS name the workload uses to reach the proxy sidecar on the per-
# sandbox internal network. Docker's embedded DNS resolves container
# aliases to their IP on user-defined networks, so this name resolves
# inside the workload without us touching /etc/hosts.
PROXY_DNS_ALIAS = "proxy"


@dataclass(frozen=True)
class _EgressFilter:
    """Per-sandbox egress-filter resources created for network='pull'.

    Tracks the docker artefacts the adapter needs to remove in
    destroy_sandbox: the per-sandbox internal network, the proxy
    sidecar container, and the host directory holding the generated
    tinyproxy config (cleaned up with the scratch dir).
    """

    network_name: str
    proxy_container: str
    config_dir: Path


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
        # Per-sandbox egress-filter artefacts (only populated for
        # network='pull'). Keyed by sandbox id so destroy_sandbox can
        # tear them down in the right order.
        self._egress: dict[str, _EgressFilter] = {}

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
            egress: Optional[_EgressFilter] = None
            if self._policy.network == "pull":
                egress = self._setup_egress_filter(scratch_host)
                stack.callback(self._teardown_egress_filter, egress)

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
        egress: Optional[_EgressFilter],
        env: Optional[Mapping[str, str]],
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
                raise RuntimeError(
                    "egress filter was not set up for network='pull' branch"
                )
            args.extend(["--network", egress.network_name])
            # Keep the audit label so existing log analyzers can still
            # see what the trial was permitted to reach.
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
        # Workload env: caller-supplied env first, then the proxy env
        # for network='pull'. We deliberately set proxy vars LAST so a
        # caller cannot accidentally point HTTP_PROXY at a different
        # host and bypass the allowlist.
        for key, value in (env or {}).items():
            args.extend(["-e", f"{key}={value}"])
        if egress is not None:
            for key, value in self._proxy_env_vars().items():
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
            completed = subprocess.run(
                args, check=True, capture_output=True, text=True
            )
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

    # ------------------------------------------------------------------
    # Egress filter (network='pull')
    # ------------------------------------------------------------------

    def _proxy_env_vars(self) -> dict[str, str]:
        """HTTP_PROXY env vars the workload sees on an internal network.

        Both upper- and lower-case forms are set because tooling is
        inconsistent (curl, pip, apt all read different cases). NO_PROXY
        keeps `localhost` and the proxy's own DNS alias reachable
        without recursion.
        """
        url = f"http://{PROXY_DNS_ALIAS}:{self._policy.proxy_port}"
        no_proxy = f"localhost,127.0.0.1,{PROXY_DNS_ALIAS}"
        return {
            "HTTP_PROXY": url,
            "HTTPS_PROXY": url,
            "http_proxy": url,
            "https_proxy": url,
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }

    def _setup_egress_filter(self, scratch_host: Path) -> _EgressFilter:
        """Stand up the per-sandbox network + proxy sidecar.

        Order matters: create the internal network first, inspect its
        subnet so we can restrict the proxy's ``Allow`` to just that
        subnet (otherwise clients on the default bridge could use the
        proxy as an open relay to our allowlisted hosts), render the
        config, then start the proxy sidecar attached to the internal
        network, then connect the sidecar to the default bridge so it
        has egress. The workload is run by the caller and joined to
        ``network_name``.

        Resource cleanup uses a single ``ExitStack`` ladder: each
        successful step registers its teardown immediately, so any
        downstream failure unwinds in reverse order with no copy-pasted
        try/except blocks. ``stack.pop_all()`` on the happy path keeps
        the resources alive past the ``with``.
        """
        suffix = uuid.uuid4().hex[:12]
        network_name = f"mig-eval-egress-{suffix}"
        # Per-sandbox config dir lives next to the scratch dir so the
        # generated tinyproxy.conf is cleaned up when the scratch dir is.
        config_dir = scratch_host.parent / f"mig-eval-proxyconf-{suffix}"
        config_dir.mkdir(parents=True, exist_ok=True)

        with contextlib.ExitStack() as stack:
            # config_dir was just created on disk; register its cleanup
            # first so it unwinds last (after the docker resources that
            # depend on it).
            stack.callback(self._cleanup_scratch, config_dir)

            self._create_internal_network(network_name)
            stack.callback(self._remove_network, network_name)

            self._write_proxy_config_files(network_name, config_dir)

            proxy_container = self._start_proxy_sidecar(
                network_name, config_dir
            )
            stack.callback(self._force_remove_container, proxy_container)

            self._connect_proxy_to_bridge(proxy_container)

            # Success: keep the resources alive for the sandbox lifetime.
            stack.pop_all()

        return _EgressFilter(
            network_name=network_name,
            proxy_container=proxy_container,
            config_dir=config_dir,
        )

    def _create_internal_network(self, network_name: str) -> None:
        """Create the per-sandbox ``--internal`` bridge network.

        ``--internal`` is the load-bearing flag: docker installs no
        MASQUERADE rule, so the workload cannot reach the host or the
        outside world directly. The proxy sidecar is the only escape
        hatch.
        """
        args = [
            self._docker_bin, "network", "create",
            "--internal",
            "--driver", "bridge",
            network_name,
        ]
        try:
            subprocess.run(args, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"docker network create failed (exit={exc.returncode}): "
                f"{exc.stderr.strip()}"
            ) from exc

    def _write_proxy_config_files(
        self, network_name: str, config_dir: Path
    ) -> None:
        """Render tinyproxy.conf + filter into ``config_dir``.

        Inspects the network for its IPAM subnet so the proxy's
        ``Allow`` directive can restrict clients to this sandbox's
        subnet only. Otherwise the sidecar (which is also on the
        default bridge for outbound egress) would accept connections
        from any container on the bridge and act as an open relay to
        our allowlist. Best-effort: if inspection fails we fall back
        to ``0.0.0.0/0`` so we don't break the sandbox, but the happy
        path on a healthy daemon always pins the subnet.
        """
        internal_subnet = self._inspect_network_subnet(network_name)
        (config_dir / "tinyproxy.conf").write_text(
            self._render_proxy_config(allow_cidr=internal_subnet),
            encoding="utf-8",
        )
        (config_dir / "filter").write_text(
            self._render_proxy_filter(), encoding="utf-8"
        )

    def _start_proxy_sidecar(
        self, network_name: str, config_dir: Path
    ) -> str:
        """Start the proxy sidecar on the internal network.

        The sidecar advertises the ``proxy`` DNS alias the workload
        uses, and mounts the per-sandbox config dir (tinyproxy.conf +
        filter) into ``/etc/tinyproxy``. Returns the container id.
        """
        proxy_run = [
            self._docker_bin, "run",
            "-d", "--rm",
            "--network", network_name,
            "--network-alias", PROXY_DNS_ALIAS,
            "-v", f"{config_dir}:/etc/tinyproxy:ro",
            self._policy.proxy_image,
        ]
        try:
            completed = subprocess.run(
                proxy_run, check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"proxy sidecar failed to start (exit={exc.returncode}): "
                f"{exc.stderr.strip()}"
            ) from exc
        proxy_container = completed.stdout.strip()
        if not proxy_container:
            raise RuntimeError("docker run produced empty proxy container id")
        return proxy_container

    def _connect_proxy_to_bridge(self, proxy_container: str) -> None:
        """Attach the proxy sidecar to the default bridge so it has egress."""
        connect_args = [
            self._docker_bin, "network", "connect", "bridge", proxy_container,
        ]
        try:
            subprocess.run(
                connect_args, check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"could not connect proxy sidecar to default bridge "
                f"(exit={exc.returncode}): {exc.stderr.strip()}"
            ) from exc

    def _remove_network(self, network_name: str) -> None:
        """Best-effort ``docker network rm``; never raises."""
        subprocess.run(
            [self._docker_bin, "network", "rm", network_name],
            capture_output=True, check=False,
        )

    def _force_remove_container(self, container_id: str) -> None:
        """Best-effort ``docker rm -f``; never raises."""
        subprocess.run(
            [self._docker_bin, "rm", "-f", container_id],
            capture_output=True, check=False,
        )

    def _render_proxy_config(self, *, allow_cidr: str | None = None) -> str:
        """Generate a tinyproxy config that points at the filter file.

        ``FilterDefaultDeny Yes`` plus an anchored regex per allowlisted
        host (in the sibling ``filter`` file) means the proxy returns
        403 for any unmatched CONNECT host. ``FilterExtended Yes``
        enables ERE so anchors and escaped dots are honoured. Hosts are
        passed through ``re.escape`` so dots in ``registry-1.docker.io``
        are literal, not "any character".

        ``allow_cidr`` restricts which clients can use the proxy at all.
        The sidecar attaches to the default bridge for outbound egress,
        so without this restriction any container on the bridge could
        relay through us to our allowlisted hosts. Pinning ``Allow`` to
        the per-sandbox internal subnet means only this sandbox's
        workload can connect.

        Also embed the allowlist as comment lines so tests (and
        operators reading the conf for audit) can see the active
        allowlist in one place; tinyproxy ignores lines starting with
        ``#``.
        """
        port = self._policy.proxy_port
        # Allow only the per-sandbox internal subnet. Fall back to
        # 0.0.0.0/0 only if the caller could not determine the subnet
        # (network inspect failed) - an audit-trail label rather than a
        # deliberate open relay.
        allow = allow_cidr if allow_cidr else "0.0.0.0/0"
        lines = [
            "# Generated by migration-evals DockerSandboxAdapter (cxa).",
            f"Port {port}",
            "Listen 0.0.0.0",
            "Timeout 600",
            # Accept CONNECT for HTTPS and HTTP. Filter does the actual
            # allow/deny based on hostname.
            "ConnectPort 443",
            "ConnectPort 80",
            # Restrict clients to the per-sandbox internal subnet so
            # the sidecar (also on the default bridge for egress) does
            # not act as an open relay to our allowlist.
            f"Allow {allow}",
            'Filter "/etc/tinyproxy/filter"',
            "FilterDefaultDeny Yes",
            "FilterExtended Yes",
            "FilterURLs Off",
        ]
        for host in self._policy.network_allowlist:
            lines.append(f"# allowlist: {self._anchored_host_regex(host)}")
        return "\n".join(lines) + "\n"

    def _render_proxy_filter(self) -> str:
        """Return the body of the tinyproxy ``filter`` file.

        One anchored regex per line; tinyproxy treats this as an
        OR-list. Empty allowlist still yields a non-empty file (a
        single never-matching line) so ``FilterDefaultDeny Yes`` is
        what actually denies — but in practice ``network='pull'``
        without an allowlist is rejected by ``SandboxPolicy``.
        """
        return "\n".join(
            self._anchored_host_regex(h)
            for h in self._policy.network_allowlist
        ) + "\n"

    @staticmethod
    def _anchored_host_regex(host: str) -> str:
        """Return ``^<re.escape(host)>(:[0-9]+)?$`` so dots are literal.

        Anchored matching prevents a sneaky ``evil-registry-1.docker.io``
        from being accepted via prefix-match against
        ``registry-1.docker.io``. The optional ``:port`` suffix is for
        tinyproxy version-tolerance: 1.11.0 strips the CONNECT port
        before regex match, but other builds retain it — accepting both
        forms keeps allowlisted hosts working across versions.
        """
        return f"^{re.escape(host)}(:[0-9]+)?$"

    def _inspect_network_subnet(self, network_name: str) -> str | None:
        """Return the IPAM subnet of a docker network, or None on failure.

        Used to pin the proxy's ``Allow`` directive to just the per-
        sandbox internal subnet so the sidecar (also on the default
        bridge for outbound egress) is not an open relay.
        """
        try:
            completed = subprocess.run(
                [self._docker_bin, "network", "inspect", network_name],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError:
            return None
        try:
            data = json.loads(completed.stdout)
            ipam_configs = data[0].get("IPAM", {}).get("Config", [])
            for cfg in ipam_configs:
                subnet = cfg.get("Subnet")
                if subnet:
                    return subnet
        except (json.JSONDecodeError, IndexError, KeyError, AttributeError):
            return None
        return None

    def _teardown_egress_filter(self, egress: _EgressFilter) -> None:
        """Best-effort: kill the proxy sidecar, then remove the network.

        Order matters: docker refuses to remove a network that still has
        endpoints attached. The workload has already been removed by
        ``destroy_sandbox`` before this is called.
        """
        self._force_remove_container(egress.proxy_container)
        self._remove_network(egress.network_name)
        self._cleanup_scratch(egress.config_dir)

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
                self._teardown_egress_filter(egress)
            return
        # check=False: a missing-container error from Docker should not
        # mask the caller's real outcome.
        subprocess.run(
            [self._docker_bin, "rm", "-f", container_id],
            capture_output=True,
            check=False,
        )
        if egress is not None:
            self._teardown_egress_filter(egress)
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
