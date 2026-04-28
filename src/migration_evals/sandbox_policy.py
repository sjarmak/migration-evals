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

import re
from dataclasses import dataclass
from typing import Any, Mapping

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

# Hostname charset for ``network_allowlist`` entries. Each entry is later
# fed through ``re.escape`` and joined into the tinyproxy filter file with
# ``\n`` separators; ``re.escape`` of a real newline produces a backslash
# followed by an actual newline, which physically splits one filter line
# into two — and a YAML entry like ``"trusted.io\nevil.io"`` would smuggle
# ``evil.io`` into the allowlist. Allow only the strict hostname charset
# (RFC 952/1123 + the underscore commonly seen in registry hosts).
_HOSTNAME_CHARSET_RE = re.compile(r"^[A-Za-z0-9._\-]+$")

# Strict ``UID:GID`` form with both components a non-zero positive integer.
# Both parts must be present (UID-only would let docker default the GID to
# the image's default group — typically root) and both must be non-zero
# (UID 0 is root; GID 0 is the root group, which still grants read access
# to root-owned files via group permissions).
_NONROOT_USER_RE = re.compile(r"^[1-9]\d*:[1-9]\d*$")


def _validate_cap_add_allowlist(caps: tuple[str, ...]) -> None:
    """Reject any cap_add entry that isn't in :data:`SAFE_CAPS`.

    Applied at YAML/dict ingest (``from_dict``) only — direct
    ``SandboxPolicy(...)`` construction is an audited internal-caller
    path and may legitimately request elevated caps (e.g. ``SYS_PTRACE``
    for a debugger-style trial). The error lists every offending cap so
    the operator can fix the recipe in one pass.
    """
    bad = [c for c in caps if c not in SAFE_CAPS]
    if bad:
        raise ValueError(
            "cap_add contains capabilities outside the safe-cap allowlist: "
            f"{bad}; allowed caps are {sorted(SAFE_CAPS)}"
        )


def _validate_network_allowlist(hosts: tuple[str, ...]) -> None:
    """Reject ``network_allowlist`` entries that contain anything outside
    the hostname charset.

    Why: each entry flows verbatim into the tinyproxy filter file via
    ``re.escape``. Newlines, carriage returns, and other control chars
    survive ``re.escape`` as literal whitespace and split one filter line
    into two, smuggling unauthorized hosts into the allowlist.
    """
    bad = [h for h in hosts if not _HOSTNAME_CHARSET_RE.match(h)]
    if bad:
        raise ValueError(
            "network_allowlist entries contain disallowed characters "
            f"(only [A-Za-z0-9._-] permitted): {bad}"
        )


def _validate_user(user: str) -> None:
    """Reject any ``user`` value that isn't a strict non-root ``UID:GID``.

    Wave-1 review surfaced that ``from_dict`` accepted any string and
    forwarded it verbatim to ``docker --user``; ``user: "0"``, ``"root"``,
    or ``"1000"`` (UID-only — GID defaults to the image's primary group,
    typically root) silently dropped the rootless-inside-container
    hardening. As with the cap_add/cap_drop validators, the check is at
    the YAML/dict ingest path only — the dataclass constructor itself is
    unguarded so internal callers retain full flexibility.
    """
    if not _NONROOT_USER_RE.fullmatch(user):
        raise ValueError(
            f"user must be a non-root 'UID:GID' (both numeric and non-zero); got {user!r}"
        )


def _validate_cap_drop(cap_drop: tuple[str, ...]) -> None:
    """Require ``"ALL"`` in any operator-supplied ``cap_drop``.

    The hardened default is ``cap_drop=("ALL",)`` paired with an empty
    ``cap_add``; the security model is "drop everything, opt back in to
    the minimum needed." A YAML recipe that supplies ``cap_drop: []`` or
    a list omitting ``ALL`` silently restores Docker's full default
    capability set, which defeats the model. Reject at ingest.
    """
    if "ALL" not in cap_drop:
        raise ValueError(
            "cap_drop must include 'ALL' to preserve the drop-all baseline; "
            f"got {list(cap_drop)}"
        )


@dataclass(frozen=True)
class SandboxPolicy:
    """Per-trial sandbox-hardening configuration.

    Construct from a YAML block under ``adapters.sandbox_policy:`` (or
    inside a recipe template) via :meth:`from_dict`. Defaults are
    locked-down; opting in to a looser stance is always explicit.
    """

    network: str = "none"
    network_allowlist: tuple[str, ...] = ()
    cap_drop: tuple[str, ...] = ("ALL",)
    cap_add: tuple[str, ...] = ()
    no_new_privileges: bool = True
    user: str | None = DEFAULT_USER
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
                f"network must be one of {ALLOWED_NETWORK_MODES}; " f"got {self.network!r}"
            )
        if self.network == "pull" and not self.network_allowlist:
            raise ValueError(
                "network='pull' requires a non-empty network_allowlist "
                "(e.g. ['registry-1.docker.io', 'proxy.golang.org'])"
            )
        if self.network == "none" and self.network_allowlist:
            raise ValueError("network_allowlist must be empty when network='none'")

    @classmethod
    def hardened_default(cls) -> "SandboxPolicy":
        """The locked-down preset every trial gets unless overridden."""
        return cls()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SandboxPolicy":
        if not data:
            return cls.hardened_default()
        kwargs: dict[str, Any] = {}
        if "network" in data:
            kwargs["network"] = str(data["network"])
        if "network_allowlist" in data:
            allowlist = tuple(str(x) for x in (data["network_allowlist"] or ()))
            _validate_network_allowlist(allowlist)
            kwargs["network_allowlist"] = allowlist
        if "cap_drop" in data:
            cap_drop = tuple(str(x) for x in (data["cap_drop"] or ()))
            _validate_cap_drop(cap_drop)
            kwargs["cap_drop"] = cap_drop
        if "cap_add" in data:
            cap_add = tuple(str(x) for x in (data["cap_add"] or ()))
            _validate_cap_add_allowlist(cap_add)
            kwargs["cap_add"] = cap_add
        if "no_new_privileges" in data:
            kwargs["no_new_privileges"] = bool(data["no_new_privileges"])
        if "user" in data:
            value = data["user"]
            # Treat only None and empty-string as "caller didn't specify"; any
            # other falsy value (integer 0, False, empty list/dict) must flow
            # through validation. Otherwise YAML's bare `user: 0` is parsed as
            # the integer 0, which is falsy in Python and would silently
            # bypass _validate_user, dropping the rootless default.
            if value is None or (isinstance(value, str) and not value):
                kwargs["user"] = None
            else:
                user = str(value)
                _validate_user(user)
                kwargs["user"] = user
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
