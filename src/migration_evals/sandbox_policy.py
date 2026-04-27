"""Sandbox-hardening policy for the Docker adapter (7gu).

Codex review #3 surfaced that ``adapters_docker.py`` was running the
trial repo read-write, with a default network namespace, no dropped
capabilities, ``no-new-privileges`` off, and the container running as
root. For a system that executes arbitrary repo code (recipe
``build_cmd`` / ``test_cmd``), that is a real exfiltration and host-
interaction surface.

This module ships the policy. :class:`SandboxPolicy` holds the knobs
the docker adapter consults; the defaults are deliberately the most
restrictive useful set:

- ``network = "none"`` — no network namespace; recipes that legitimately
  need a registry pull opt in via ``network = "pull"`` plus an explicit
  ``network_allowlist``.
- ``cap_drop = ("ALL",)`` plus an empty ``cap_add`` — recipes opt back
  in to a specific capability if the build genuinely needs it.
- ``no_new_privileges = True`` — no setuid escalation inside the
  container.
- ``user = "1000:1000"`` — rootless inside the container.
- ``repo_mount_readonly = True`` — the source tree is mounted ``ro``;
  build output writes go to ``scratch_dir`` (a separate writable mount).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

ALLOWED_NETWORK_MODES = ("none", "pull")
DEFAULT_USER = "1000:1000"
DEFAULT_SCRATCH = "/scratch"
DEFAULT_PROXY_IMAGE = "vimagick/tinyproxy:latest"
DEFAULT_PROXY_PORT = 8888

# Docker's documented default-capability set — these are what the docker
# daemon grants to a container before any --cap-add. Anything outside
# this set (notably SYS_ADMIN, NET_ADMIN, SYS_PTRACE, SYS_MODULE,
# DAC_READ_SEARCH) materially expands the blast radius of a sandbox
# escape and must NOT be re-grantable from a YAML/dict-supplied recipe.
# Source: docs.docker.com/engine/containers/run/#runtime-privilege-and-
# linux-capabilities — "default capabilities" table.
SAFE_CAPS = frozenset(
    {
        "AUDIT_WRITE",
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "FSETID",
        "KILL",
        "MKNOD",
        "NET_BIND_SERVICE",
        "NET_RAW",
        "SETFCAP",
        "SETGID",
        "SETPCAP",
        "SETUID",
        "SYS_CHROOT",
    }
)


def _validate_cap_add_allowlist(caps: tuple[str, ...]) -> None:
    """Reject any cap_add entry that isn't in :data:`SAFE_CAPS`.

    Applied at YAML/dict ingest (``from_dict``) only — direct
    ``SandboxPolicy(...)`` construction is an audited internal-caller
    path and may legitimately request elevated caps (e.g. ``SYS_PTRACE``
    for a debugger-style trial). The error lists every offending cap so
    the operator can fix the recipe in one pass.
    """
    bad = tuple(c for c in caps if c not in SAFE_CAPS)
    if bad:
        raise ValueError(
            "cap_add contains capabilities outside the safe-cap allowlist: "
            f"{list(bad)}; allowed caps are {sorted(SAFE_CAPS)}"
        )


@dataclass(frozen=True)
class SandboxPolicy:
    """Per-trial sandbox-hardening configuration.

    Construct from a YAML block under ``adapters.sandbox_policy:`` (or
    inside a recipe template) via :meth:`from_dict`. Defaults are
    locked-down; opting in to a looser stance is always explicit.
    """

    network: str = "none"
    network_allowlist: Tuple[str, ...] = ()
    cap_drop: Tuple[str, ...] = ("ALL",)
    cap_add: Tuple[str, ...] = ()
    no_new_privileges: bool = True
    user: Optional[str] = DEFAULT_USER
    repo_mount_readonly: bool = True
    scratch_dir: str = DEFAULT_SCRATCH
    # Egress-filter knobs (cxa). When network='pull', the docker adapter
    # spins up an HTTP CONNECT proxy sidecar from this image and routes
    # the workload's HTTP_PROXY/HTTPS_PROXY to it on this port. The
    # workload's own network is a per-sandbox `--internal` bridge, so
    # the proxy is the only egress path and the allowlist is enforced
    # mechanically. The image is configurable so test environments and
    # air-gapped deployments can swap in a pre-mirrored proxy build.
    proxy_image: str = DEFAULT_PROXY_IMAGE
    proxy_port: int = DEFAULT_PROXY_PORT

    def __post_init__(self) -> None:
        if self.network not in ALLOWED_NETWORK_MODES:
            raise ValueError(
                f"network must be one of {ALLOWED_NETWORK_MODES}; "
                f"got {self.network!r}"
            )
        if self.network == "pull" and not self.network_allowlist:
            raise ValueError(
                "network='pull' requires a non-empty network_allowlist "
                "(e.g. ['registry-1.docker.io', 'proxy.golang.org'])"
            )
        if self.network == "none" and self.network_allowlist:
            raise ValueError(
                "network_allowlist must be empty when network='none'"
            )

    @classmethod
    def hardened_default(cls) -> "SandboxPolicy":
        """The locked-down preset every trial gets unless overridden."""
        return cls()

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any] | None
    ) -> "SandboxPolicy":
        if not data:
            return cls.hardened_default()
        kwargs: dict[str, Any] = {}
        if "network" in data:
            kwargs["network"] = str(data["network"])
        if "network_allowlist" in data:
            allowlist = data["network_allowlist"] or ()
            kwargs["network_allowlist"] = tuple(str(x) for x in allowlist)
        if "cap_drop" in data:
            kwargs["cap_drop"] = tuple(
                str(x) for x in (data["cap_drop"] or ())
            )
        if "cap_add" in data:
            cap_add = tuple(
                str(x) for x in (data["cap_add"] or ())
            )
            _validate_cap_add_allowlist(cap_add)
            kwargs["cap_add"] = cap_add
        if "no_new_privileges" in data:
            kwargs["no_new_privileges"] = bool(data["no_new_privileges"])
        if "user" in data:
            value = data["user"]
            kwargs["user"] = str(value) if value else None
        if "repo_mount_readonly" in data:
            kwargs["repo_mount_readonly"] = bool(data["repo_mount_readonly"])
        if "scratch_dir" in data:
            kwargs["scratch_dir"] = str(data["scratch_dir"])
        if "proxy_image" in data:
            kwargs["proxy_image"] = str(data["proxy_image"])
        if "proxy_port" in data:
            kwargs["proxy_port"] = int(data["proxy_port"])
        return cls(**kwargs)


__all__ = [
    "ALLOWED_NETWORK_MODES",
    "DEFAULT_PROXY_IMAGE",
    "DEFAULT_PROXY_PORT",
    "DEFAULT_SCRATCH",
    "DEFAULT_USER",
    "SAFE_CAPS",
    "SandboxPolicy",
]
