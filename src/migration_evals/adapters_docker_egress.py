"""Per-sandbox egress filter for :mod:`migration_evals.adapters_docker`.

Extracted from ``adapters_docker.py`` (the file was >800 lines): the
``network='pull'`` machinery is a self-contained unit that stands up a
per-sandbox ``--internal`` bridge network plus a hardened tinyproxy
sidecar, forces the workload through it via ``HTTP_PROXY`` env vars, and
tears all of it down again. :class:`DockerSandboxAdapter` owns the
container/scratch lifecycle and delegates the egress concern here.

The sidecar is the only escape hatch from the ``--internal`` network:
docker installs no MASQUERADE rule for an internal network, so the
workload cannot reach the host or the outside world directly. tinyproxy
runs ``FilterDefaultDeny Yes`` with one anchored regex per allowlisted
host, so any unmatched CONNECT target gets a 403.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from migration_evals.sandbox_policy import SandboxPolicy

__all__ = [
    "EgressFilter",
    "EgressFilterManager",
    "PROXY_DNS_ALIAS",
    "PROXY_SIDECAR_USER",
    "PROXY_SIDECAR_TMPFS_MOUNT",
    "PROXY_SIDECAR_PID_PATH",
    "PROXY_SIDECAR_LOG_PATH",
    "PROXY_SIDECAR_PIDS_LIMIT",
    "PROXY_READINESS_ITERATIONS",
    "PROXY_READINESS_SLEEP_S",
    "PROXY_READINESS_SUBPROCESS_TIMEOUT_S",
]


# DNS name the workload uses to reach the proxy sidecar on the per-
# sandbox internal network. Docker's embedded DNS resolves container
# aliases to their IP on user-defined networks, so this name resolves
# inside the workload without us touching /etc/hosts.
PROXY_DNS_ALIAS = "proxy"
# Numeric ``nobody:nogroup`` (POSIX-standard nobody UID/GID). Used as the
# proxy sidecar's ``--user`` to drop container-root without coupling to
# any specific image's ``/etc/passwd``. Hardcoded — not derived from
# ``policy.user`` — because the sidecar is adapter infrastructure, not
# workload-recipe-configurable; routing it through policy would let a
# recipe accidentally weaken the sidecar's isolation.
PROXY_SIDECAR_USER = "65534:65534"
# Defense-in-depth (0ez): the sidecar root filesystem is mounted
# ``--read-only`` and the only writable path is a tmpfs at /tmp where
# tinyproxy's pid file and log file land (see ``_render_proxy_config``,
# which sets ``PidFile`` / ``LogFile`` explicitly to /tmp paths).
# ``size=16m`` caps memory consumption so a runaway tinyproxy or an
# attacker with arbitrary write cannot exhaust host RAM by filling the
# tmpfs; 16 MiB is ample for a pid file (a handful of bytes) plus a log
# file that rotates on container exit. ``mode=1777`` matches a normal
# /tmp (sticky, world-writable) so the non-root sidecar user
# (``PROXY_SIDECAR_USER``) can write into it.
PROXY_SIDECAR_TMPFS_MOUNT: str = "/tmp:size=16m,mode=1777"
# Tinyproxy writes a pid file and log file at runtime. We pin both to
# /tmp so they land in ``PROXY_SIDECAR_TMPFS_MOUNT`` (the only writable
# path under ``--read-only``); without these explicit directives
# tinyproxy falls back to compiled-in defaults (typically /var/run and
# /var/log) which would EROFS-fail on startup.
PROXY_SIDECAR_PID_PATH: str = "/tmp/tinyproxy.pid"
PROXY_SIDECAR_LOG_PATH: str = "/tmp/tinyproxy.log"
# Caps fork bombs from a compromised sidecar. The kernel default is
# ~4M pids; tinyproxy preforks ``StartServers`` (default 10) child
# processes and can grow up to ``MaxClients`` (default 100) under load.
# 128 leaves comfortable headroom above MaxClients so legitimate traffic
# is never throttled while still being three orders of magnitude below
# the kernel default — small enough to stop a fork-bomb pivot before it
# threatens the host.
PROXY_SIDECAR_PIDS_LIMIT: int = 128
# Proxy readiness loop: 50 iterations × 0.1s = 5s cap. Wave-1 review
# (security MEDIUM #3): tinyproxy starts in <1s on a healthy host, so
# 5s is a generous-but-bounded ceiling that closes the race where a
# fast workload (go build, pip install) hits connection-refused before
# the sidecar has bound its listen socket. Increase only if a slower
# proxy image lands; never make this unbounded.
PROXY_READINESS_ITERATIONS: int = 50
# str (not float) because it's interpolated verbatim into a sh -c script,
# never multiplied or compared as a number.
PROXY_READINESS_SLEEP_S: str = "0.1"
# Python-side timeout on subprocess.run(..., timeout=) for the readiness
# probe. The shell loop above is internally bounded at ITERATIONS × SLEEP_S
# (= 5s), but docker exec dispatch itself can stall before the script even
# starts (daemon queue, namespace setup). Add a generous Python-side cap so
# a wedged daemon can't block create_sandbox indefinitely. Belt and braces.
PROXY_READINESS_SUBPROCESS_TIMEOUT_S: float = 30.0


@dataclass(frozen=True)
class EgressFilter:
    """Per-sandbox egress-filter resources created for network='pull'.

    Tracks the docker artefacts the adapter needs to remove in
    destroy_sandbox: the per-sandbox internal network, the proxy
    sidecar container, and the host directory holding the generated
    tinyproxy config (cleaned up with the scratch dir).
    """

    network_name: str
    proxy_container: str
    config_dir: Path


class EgressFilterManager:
    """Stand up and tear down the network='pull' egress filter.

    Holds the docker CLI name and the :class:`SandboxPolicy` so
    :class:`DockerSandboxAdapter` can delegate the whole egress concern
    behind three public methods: :meth:`setup`, :meth:`teardown`, and
    :meth:`proxy_env_vars`.
    """

    def __init__(self, *, docker_bin: str, policy: SandboxPolicy) -> None:
        self._docker_bin = docker_bin
        self._policy = policy

    def proxy_env_vars(self) -> dict[str, str]:
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

    def setup(self, scratch_host: Path) -> EgressFilter:
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
        # Pin permissions explicitly. The sidecar runs as PROXY_SIDECAR_USER
        # (a non-root UID different from the host invoker), so the dir and
        # config files must be world-readable. mkdir's mode is masked by the
        # process umask, so a hardened umask (0o077) without an explicit
        # chmod would silently produce a 0o700 dir the sidecar cannot enter.
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir.chmod(0o755)

        with contextlib.ExitStack() as stack:
            # config_dir is bind-mounted into the proxy sidecar; register its
            # cleanup first so it unwinds LAST — after the container that
            # mounts it has been force-removed.
            stack.callback(self._rmtree, config_dir)

            self._create_internal_network(network_name)
            stack.callback(self._remove_network, network_name)

            self._write_proxy_config_files(network_name, config_dir)

            proxy_container = self._start_proxy_sidecar(network_name, config_dir)
            stack.callback(self._force_remove_container, proxy_container)

            self._connect_proxy_to_bridge(proxy_container)

            # Wave-1 review (security MEDIUM #3): block until tinyproxy is
            # actually listening before returning. Otherwise a fast
            # workload races the proxy and hits connection-refused. The
            # probe runs inside the sidecar (so it sees the loopback
            # interface tinyproxy binds to) and is bounded so a stuck
            # sidecar fails the sandbox rather than hangs it. Registered
            # AFTER the proxy + network teardowns so a probe failure
            # unwinds them.
            self._wait_for_proxy_ready(proxy_container)

            # Success: keep the resources alive for the sandbox lifetime.
            stack.pop_all()

        return EgressFilter(
            network_name=network_name,
            proxy_container=proxy_container,
            config_dir=config_dir,
        )

    def teardown(self, egress: EgressFilter) -> None:
        """Best-effort: kill the proxy sidecar, then remove the network.

        Order matters: docker refuses to remove a network that still has
        endpoints attached. The workload has already been removed by
        ``destroy_sandbox`` before this is called.
        """
        self._force_remove_container(egress.proxy_container)
        self._remove_network(egress.network_name)
        self._rmtree(egress.config_dir)

    def _create_internal_network(self, network_name: str) -> None:
        """Create the per-sandbox ``--internal`` bridge network.

        ``--internal`` is the load-bearing flag: docker installs no
        MASQUERADE rule, so the workload cannot reach the host or the
        outside world directly. The proxy sidecar is the only escape
        hatch.
        """
        args = [
            self._docker_bin,
            "network",
            "create",
            "--internal",
            "--driver",
            "bridge",
            network_name,
        ]
        try:
            subprocess.run(args, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"docker network create failed (exit={exc.returncode}): " f"{exc.stderr.strip()}"
            ) from exc

    def _write_proxy_config_files(self, network_name: str, config_dir: Path) -> None:
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
        conf_path = config_dir / "tinyproxy.conf"
        filter_path = config_dir / "filter"
        conf_path.write_text(
            self._render_proxy_config(allow_cidr=internal_subnet),
            encoding="utf-8",
        )
        filter_path.write_text(self._render_proxy_filter(), encoding="utf-8")
        # Same rationale as config_dir.chmod above: sidecar runs as a
        # different (non-root) user so files must be world-readable
        # regardless of host umask.
        conf_path.chmod(0o644)
        filter_path.chmod(0o644)

    def _start_proxy_sidecar(self, network_name: str, config_dir: Path) -> str:
        """Start the proxy sidecar on the internal network.

        The sidecar advertises the ``proxy`` DNS alias the workload
        uses, and mounts the per-sandbox config dir (tinyproxy.conf +
        filter) into ``/etc/tinyproxy``. Returns the container id.

        Hardening (91m): the sidecar mirrors the workload's baseline
        isolation flags — ``--cap-drop=ALL``,
        ``--security-opt=no-new-privileges:true``, and a non-root
        ``--user`` — so a tinyproxy memory-safety CVE cannot pivot
        from the sidecar (which is bridged to the default network for
        outbound egress) into the host. ``65534:65534`` is the
        conventional ``nobody:nogroup`` uid/gid; we pin it numerically
        so the sidecar is not coupled to a specific proxy image's
        ``/etc/passwd``. The default proxy port (8888) is non-
        privileged, so dropping root is safe.

        Defense-in-depth (0ez): adds ``--read-only`` (rootfs is
        immutable so an attacker with arbitrary write inside the
        sidecar cannot persist binaries or tamper with /etc),
        ``--tmpfs`` at /tmp (the only writable path; sized so it
        cannot exhaust host RAM), and ``--pids-limit`` (caps fork
        bombs). The rendered tinyproxy.conf points ``PidFile`` and
        ``LogFile`` at the tmpfs so the proxy can start under a
        read-only rootfs.
        """
        proxy_run = [
            self._docker_bin,
            "run",
            "-d",
            "--rm",
            "--network",
            network_name,
            "--network-alias",
            PROXY_DNS_ALIAS,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            PROXY_SIDECAR_USER,
            "--read-only",
            "--tmpfs",
            PROXY_SIDECAR_TMPFS_MOUNT,
            "--pids-limit",
            str(PROXY_SIDECAR_PIDS_LIMIT),
            "-v",
            f"{config_dir}:/etc/tinyproxy:ro",
            self._policy.proxy_image,
        ]
        try:
            completed = subprocess.run(proxy_run, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"proxy sidecar failed to start (exit={exc.returncode}): " f"{exc.stderr.strip()}"
            ) from exc
        proxy_container = completed.stdout.strip()
        if not proxy_container:
            raise RuntimeError("docker run produced empty proxy container id")
        return proxy_container

    def _connect_proxy_to_bridge(self, proxy_container: str) -> None:
        """Attach the proxy sidecar to the default bridge so it has egress."""
        connect_args = [
            self._docker_bin,
            "network",
            "connect",
            "bridge",
            proxy_container,
        ]
        try:
            subprocess.run(connect_args, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"could not connect proxy sidecar to default bridge "
                f"(exit={exc.returncode}): {exc.stderr.strip()}"
            ) from exc

    def _wait_for_proxy_ready(self, proxy_container: str) -> None:
        """Block until tinyproxy is listening on its port, or raise.

        Wave-1 review (security MEDIUM #3): ``docker run -d`` returns as
        soon as the container is created, not when its workload is
        ready. Without this probe, a fast workload (e.g. ``go build``
        kicking off ``go mod download`` immediately) can issue its first
        request before tinyproxy has bound its listen socket and hit
        connection-refused, masquerading as an allowlist denial.

        The probe runs inside the sidecar via ``docker exec sh -c`` so
        it observes the same loopback interface tinyproxy binds to. The
        retry loop is bounded — ``PROXY_READINESS_ITERATIONS`` × ``0.1``
        seconds — so a wedged sidecar fails the sandbox in 5s rather
        than hanging the run.

        ``nc -z`` is the probe: alpine's busybox (the base of the
        default ``kalaksi/tinyproxy`` image) ships netcat, so this is
        portable across the proxy images we support today. The argv is
        a literal list so the diff is the documentation.
        """
        port = self._policy.proxy_port
        # Bounded retry: exit 0 the first time nc -z succeeds; exit 1
        # after PROXY_READINESS_ITERATIONS misses. POSIX `sh` (busybox
        # ash) — no bashisms like /dev/tcp.
        script = (
            f"i=0; while [ $i -lt {PROXY_READINESS_ITERATIONS} ]; do "
            f"nc -z 127.0.0.1 {port} && exit 0; "
            f"i=$((i+1)); sleep {PROXY_READINESS_SLEEP_S}; "
            "done; exit 1"
        )
        probe_args = [
            self._docker_bin,
            "exec",
            proxy_container,
            "sh",
            "-c",
            script,
        ]
        try:
            subprocess.run(
                probe_args,
                check=True,
                capture_output=True,
                text=True,
                timeout=PROXY_READINESS_SUBPROCESS_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"proxy sidecar {proxy_container} readiness probe timed out "
                f"after {PROXY_READINESS_SUBPROCESS_TIMEOUT_S}s (docker daemon "
                f"may be stalled)"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"proxy sidecar {proxy_container} did not become ready on port "
                f"{port} within {PROXY_READINESS_ITERATIONS} x "
                f"{PROXY_READINESS_SLEEP_S}s (exit={exc.returncode}): "
                f"{exc.stderr.strip()}"
            ) from exc

    def _remove_network(self, network_name: str) -> None:
        """Best-effort ``docker network rm``; never raises."""
        subprocess.run(
            [self._docker_bin, "network", "rm", network_name],
            capture_output=True,
            check=False,
        )

    def _force_remove_container(self, container_id: str) -> None:
        """Best-effort ``docker rm -f``; never raises."""
        subprocess.run(
            [self._docker_bin, "rm", "-f", container_id],
            capture_output=True,
            check=False,
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

        ``PidFile`` and ``LogFile`` are explicitly pinned to /tmp so
        they land in the sidecar's tmpfs (see
        ``PROXY_SIDECAR_TMPFS_MOUNT``); the sidecar runs with
        ``--read-only`` so tinyproxy's compiled-in defaults
        (typically /var/run, /var/log) would EROFS-fail on startup.

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
        #
        # Defense-in-depth (4cz): allow_cidr originates from
        # ``docker network inspect`` JSON via _inspect_network_subnet.
        # The Docker daemon is in our trust boundary, but a parser bug
        # or daemon misbehaviour returning a string with a newline would
        # inject arbitrary tinyproxy directives into the rendered conf.
        # Validate as a CIDR before interpolating; on parse failure fall
        # back to 0.0.0.0/0 (same fallback as a missing subnet).
        allow = self._safe_cidr(allow_cidr) if allow_cidr else "0.0.0.0/0"
        lines = [
            "# Generated by migration-evals DockerSandboxAdapter (cxa).",
            f"Port {port}",
            "Listen 0.0.0.0",
            "Timeout 600",
            # Pin pid/log to the sidecar's tmpfs (0ez); the rootfs is
            # mounted read-only so tinyproxy's defaults would EROFS-fail.
            f'PidFile "{PROXY_SIDECAR_PID_PATH}"',
            f'LogFile "{PROXY_SIDECAR_LOG_PATH}"',
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
        return (
            "\n".join(self._anchored_host_regex(h) for h in self._policy.network_allowlist) + "\n"
        )

    @staticmethod
    def _safe_cidr(value: str) -> str:
        """Return ``value`` if it parses as a CIDR, else ``0.0.0.0/0``.

        Defense-in-depth wrapper for values that flow into the rendered
        tinyproxy.conf. ``_inspect_network_subnet`` reads JSON from
        ``docker network inspect`` and returns a CIDR string; a parser
        bug or daemon misbehaviour returning a value containing a
        newline (or any non-CIDR garbage) would otherwise be
        interpolated verbatim into ``Allow {value}`` and could inject
        arbitrary tinyproxy directives into the conf file. ``ip_network``
        with ``strict=False`` accepts both network ("10.0.0.0/24") and
        host-bit-set ("10.0.0.5/24") forms — Docker's IPAM emits the
        former, but accepting both keeps us tolerant of upstream
        formatting changes. On any parse failure fall back to
        ``0.0.0.0/0`` (the same audit-trail label used when the subnet
        cannot be determined at all); the workload network is already
        ``--internal`` so the sidecar's only reachable clients are
        sandbox containers regardless.
        """
        try:
            return str(ipaddress.ip_network(value, strict=False))
        except (ValueError, TypeError):
            return "0.0.0.0/0"

    @staticmethod
    def _anchored_host_regex(host: str) -> str:
        """Return ``^<re.escape(host)>(:[0-9]{1,5})?$`` so dots are literal.

        Anchored matching prevents a sneaky ``evil-registry-1.docker.io``
        from being accepted via prefix-match against
        ``registry-1.docker.io``. The optional ``:port`` suffix is for
        tinyproxy version-tolerance: both 1.11.0 and 1.11.2 (the
        currently-pinned ``DEFAULT_PROXY_IMAGE``) strip the CONNECT
        port before regex match, but other builds may retain it —
        accepting both forms keeps allowlisted hosts working across
        versions.

        The port quantifier is ``{1,5}`` — same order-of-magnitude as
        the TCP port space (max 65535, 5 digits). This rejects long
        digit-string garbage (``:99999999999``) at the regex level.
        Residual numeric-range gap: ``:0`` (reserved) and ``:99999``
        (above 65535) are still admitted by the regex because a full
        1-65535 range check would require awkward alternation; the OS
        socket layer rejects them at connect time, so there is no
        real bypass — only a semantically-too-broad allowlist pattern.
        See ``test_anchored_host_regex_documents_zero_port_gap`` and
        ``test_anchored_host_regex_documents_high_port_gap``.
        """
        return f"^{re.escape(host)}(:[0-9]{{1,5}})?$"

    def _inspect_network_subnet(self, network_name: str) -> str | None:
        """Return the IPAM subnet of a docker network, or None on failure.

        Used to pin the proxy's ``Allow`` directive to just the per-
        sandbox internal subnet so the sidecar (also on the default
        bridge for outbound egress) is not an open relay.
        """
        try:
            completed = subprocess.run(
                [self._docker_bin, "network", "inspect", network_name],
                check=True,
                capture_output=True,
                text=True,
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

    @staticmethod
    def _rmtree(path: Path) -> None:
        """Best-effort recursive removal of the per-sandbox config dir."""
        shutil.rmtree(path, ignore_errors=True)
