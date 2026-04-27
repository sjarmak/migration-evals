"""Tests for SandboxPolicy validation (5k2).

Wave-1 review surfaced that ``SandboxPolicy.from_dict`` accepted any
string in ``cap_add`` and forwarded it straight to ``docker --cap-add``.
A YAML-supplied recipe could therefore re-grant dangerous capabilities
(SYS_ADMIN, NET_ADMIN, SYS_PTRACE, ...) without any guard.

The fix is an allowlist check in ``from_dict``: only Docker's default
safe-cap set is accepted on the YAML/dict ingest path. The dataclass
constructor itself is unchanged so internal callers with full audit
context can still set ``cap_add`` directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from migration_evals.sandbox_policy import (  # noqa: E402
    SAFE_CAPS,
    SandboxPolicy,
)


# ---------------------------------------------------------------------------
# SAFE_CAPS contents — explicit lock against accidental drift
# ---------------------------------------------------------------------------


def test_safe_caps_matches_docker_default_set() -> None:
    """Pin the safe set to Docker's documented default capabilities.

    If a future change touches this set, that is a security-relevant
    decision and should fail this test loudly so the diff is reviewed.
    """
    assert SAFE_CAPS == frozenset(
        {
            "CHOWN",
            "DAC_OVERRIDE",
            "FOWNER",
            "FSETID",
            "KILL",
            "SETGID",
            "SETUID",
            "SETPCAP",
            "NET_BIND_SERVICE",
            "NET_RAW",
            "SYS_CHROOT",
            "MKNOD",
            "AUDIT_WRITE",
            "SETFCAP",
        }
    )


def test_safe_caps_excludes_known_dangerous_caps() -> None:
    for cap in (
        "SYS_ADMIN",
        "NET_ADMIN",
        "SYS_PTRACE",
        "SYS_MODULE",
        "DAC_READ_SEARCH",
        "ALL",
    ):
        assert cap not in SAFE_CAPS


# ---------------------------------------------------------------------------
# from_dict: cap_add allowlist enforcement
# ---------------------------------------------------------------------------


def test_from_dict_accepts_empty_cap_add() -> None:
    policy = SandboxPolicy.from_dict({"cap_add": []})
    assert policy.cap_add == ()


def test_from_dict_accepts_missing_cap_add() -> None:
    policy = SandboxPolicy.from_dict({})
    assert policy.cap_add == ()


def test_from_dict_accepts_single_safe_cap() -> None:
    policy = SandboxPolicy.from_dict({"cap_add": ["NET_BIND_SERVICE"]})
    assert policy.cap_add == ("NET_BIND_SERVICE",)


def test_from_dict_accepts_all_safe_caps() -> None:
    safe = sorted(SAFE_CAPS)
    policy = SandboxPolicy.from_dict({"cap_add": safe})
    assert set(policy.cap_add) == SAFE_CAPS


def test_from_dict_rejects_sys_admin() -> None:
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"cap_add": ["SYS_ADMIN"]})
    msg = str(excinfo.value)
    assert "SYS_ADMIN" in msg
    assert "cap_add" in msg


def test_from_dict_rejects_net_admin() -> None:
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"cap_add": ["NET_ADMIN"]})
    assert "NET_ADMIN" in str(excinfo.value)


def test_from_dict_rejects_sys_ptrace() -> None:
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"cap_add": ["SYS_PTRACE"]})
    assert "SYS_PTRACE" in str(excinfo.value)


def test_from_dict_rejects_all_pseudo_cap() -> None:
    """``ALL`` is the wildcard and must never be re-granted via cap_add."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"cap_add": ["ALL"]})
    assert "ALL" in str(excinfo.value)


def test_from_dict_mixed_caps_lists_only_bad_ones() -> None:
    """Error message must call out every offending cap, not just the first.

    The safe cap may legitimately appear in the message's allowlist hint
    (e.g. ``allowed caps are [..., 'NET_BIND_SERVICE', ...]``); what
    must not happen is the safe cap being flagged in the offending-list.
    """
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict(
            {"cap_add": ["NET_BIND_SERVICE", "SYS_ADMIN", "NET_ADMIN"]}
        )
    msg = str(excinfo.value)
    # both bad caps named in the offending-list
    assert "SYS_ADMIN" in msg
    assert "NET_ADMIN" in msg
    # split on the allowlist hint so we can assert against the bad-list
    # portion only — the safe cap is allowed to appear in the hint.
    bad_section = msg.split("allowed caps are")[0]
    assert "NET_BIND_SERVICE" not in bad_section


def test_from_dict_rejects_lowercase_cap() -> None:
    """Linux capabilities are conventionally upper-case; lowercase
    forms are not a valid Docker --cap-add spelling and must be
    rejected rather than silently lower-cased."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"cap_add": ["sys_admin"]})
    assert "sys_admin" in str(excinfo.value)


def test_from_dict_rejects_mixed_case_safe_cap() -> None:
    """Even a cap whose upper-case form is in the safe set is rejected
    if spelled with non-canonical casing — the YAML author should fix
    the spelling."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"cap_add": ["Net_Bind_Service"]})
    assert "Net_Bind_Service" in str(excinfo.value)


def test_from_dict_rejects_unknown_cap() -> None:
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"cap_add": ["TOTALLY_FAKE_CAP"]})


# ---------------------------------------------------------------------------
# Direct constructor remains unguarded (internal-callers contract)
# ---------------------------------------------------------------------------


def test_constructor_accepts_dangerous_cap_directly() -> None:
    """Direct ``SandboxPolicy(...)`` construction is the audited
    internal-caller path — the cap allowlist applies only to YAML/dict
    ingest. Keeping this asymmetry explicit prevents the validation
    from creeping into __post_init__ and breaking the SYS_PTRACE-needs-
    to-be-set-from-Python tests in test_adapters_docker.py."""
    policy = SandboxPolicy(cap_add=("SYS_PTRACE",))
    assert policy.cap_add == ("SYS_PTRACE",)
