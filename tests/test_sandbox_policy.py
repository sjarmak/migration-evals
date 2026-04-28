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
    DEFAULT_PROXY_IMAGE,
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
        SandboxPolicy.from_dict({"cap_add": ["NET_BIND_SERVICE", "SYS_ADMIN", "NET_ADMIN"]})
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


# ---------------------------------------------------------------------------
# network_allowlist hostname-charset validation (security wave-2 F1)
# ---------------------------------------------------------------------------


def test_from_dict_rejects_network_allowlist_with_newline() -> None:
    """Wave-2 security review F1: a YAML entry containing a newline
    (e.g. ``"trusted.io\\nevil.io"``) survives ``re.escape`` as a
    literal newline + backslash, splitting one tinyproxy filter line
    into two and smuggling ``evil.io`` into the allowlist."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"network": "pull", "network_allowlist": ["trusted.io\nevil.io"]})
    assert "network_allowlist" in str(excinfo.value)


def test_from_dict_rejects_network_allowlist_with_carriage_return() -> None:
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"network": "pull", "network_allowlist": ["trusted.io\rother.io"]})


def test_from_dict_rejects_network_allowlist_with_space() -> None:
    """Spaces are also outside the hostname charset and would split
    the filter line in some tinyproxy versions."""
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"network": "pull", "network_allowlist": ["trusted.io evil.io"]})


def test_from_dict_rejects_network_allowlist_with_regex_metachar() -> None:
    """Regex metacharacters survive ``re.escape`` but should still be
    rejected at ingest — they have no place in a hostname."""
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"network": "pull", "network_allowlist": ["evil.*"]})


def test_from_dict_accepts_normal_network_allowlist() -> None:
    """Sanity check: the validator does not over-reject."""
    policy = SandboxPolicy.from_dict(
        {
            "network": "pull",
            "network_allowlist": [
                "registry-1.docker.io",
                "pypi.org",
                "files.pythonhosted.org",
                "host_with_underscore.example",
            ],
        }
    )
    assert policy.network_allowlist == (
        "registry-1.docker.io",
        "pypi.org",
        "files.pythonhosted.org",
        "host_with_underscore.example",
    )


def test_from_dict_lists_all_bad_network_allowlist_entries() -> None:
    """Operator should see every offending entry, not just the first."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict(
            {
                "network": "pull",
                "network_allowlist": ["good.io", "bad\nio", "also bad"],
            }
        )
    msg = str(excinfo.value)
    # The list is rendered via repr(), so the newline shows as ``\\n``.
    assert "bad\\nio" in msg
    assert "also bad" in msg


# ---------------------------------------------------------------------------
# cap_drop minimum-requirement validation (security wave-2 F2)
# ---------------------------------------------------------------------------


def test_from_dict_rejects_empty_cap_drop() -> None:
    """A YAML recipe that strips the drop-ALL baseline must be
    rejected — otherwise ``cap_add: ["NET_RAW"]`` plus ``cap_drop: []``
    silently restores Docker's full default-cap set on top of the
    explicitly opted-in cap."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"cap_drop": []})
    assert "ALL" in str(excinfo.value)


def test_from_dict_rejects_cap_drop_without_all() -> None:
    """``cap_drop: ["NET_RAW"]`` (without ALL) is also rejected — the
    operator must explicitly preserve the drop-all baseline."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"cap_drop": ["NET_RAW"]})
    assert "ALL" in str(excinfo.value)


def test_from_dict_accepts_cap_drop_with_all() -> None:
    """``cap_drop: ["ALL"]`` (the canonical baseline) must still work,
    as must ``cap_drop: ["ALL", "NET_RAW"]`` (belt-and-suspenders)."""
    policy = SandboxPolicy.from_dict({"cap_drop": ["ALL", "NET_RAW"]})
    assert "ALL" in policy.cap_drop


def test_constructor_accepts_cap_drop_without_all() -> None:
    """Same asymmetry as ``cap_add``: the validation applies to the
    YAML ingest path only. Direct construction is the audited internal
    caller and may legitimately customize the drop set."""
    policy = SandboxPolicy(cap_drop=())
    assert policy.cap_drop == ()


# ---------------------------------------------------------------------------
# user non-root requirement (security wave-2 znh)
# ---------------------------------------------------------------------------
#
# Wave-1 review surfaced that ``SandboxPolicy.from_dict`` accepted any
# string in the ``user`` field and forwarded it verbatim to ``docker
# --user``. A YAML recipe of ``user: "0"`` or ``user: "root"`` therefore
# silently reverted the rootless-inside-container hardening default
# (``DEFAULT_USER = "1000:1000"``). The fix is a strict ``UID:GID``
# regex with both components non-zero, applied at YAML/dict ingest only
# — the dataclass constructor itself is unguarded so internal callers
# with full audit context retain the existing flexibility (matches the
# ``cap_add`` / ``cap_drop`` asymmetry above).


def test_from_dict_accepts_default_user() -> None:
    policy = SandboxPolicy.from_dict({"user": "1000:1000"})
    assert policy.user == "1000:1000"


def test_from_dict_accepts_alternate_nonroot_uid_gid() -> None:
    policy = SandboxPolicy.from_dict({"user": "2000:3000"})
    assert policy.user == "2000:3000"


def test_from_dict_accepts_missing_user() -> None:
    """Missing ``user`` key falls through to ``DEFAULT_USER``."""
    policy = SandboxPolicy.from_dict({})
    assert policy.user == "1000:1000"


def test_from_dict_accepts_null_user() -> None:
    """An explicit ``user: null`` (or empty string) is normalized to
    ``None`` and means "let the caller / docker pick" — the hardening
    default is applied at a higher layer in that case."""
    policy = SandboxPolicy.from_dict({"user": None})
    assert policy.user is None


def test_from_dict_rejects_user_root_string() -> None:
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"user": "root"})
    msg = str(excinfo.value)
    assert "user" in msg
    assert "root" in msg


def test_from_dict_rejects_user_zero() -> None:
    """``user: "0"`` is the numeric form of root and must be rejected."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"user": "0"})
    assert "user" in str(excinfo.value)


def test_from_dict_rejects_user_zero_zero() -> None:
    """``0:0`` is root:root in numeric form."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"user": "0:0"})
    assert "user" in str(excinfo.value)


def test_from_dict_rejects_user_zero_uid_nonzero_gid() -> None:
    """UID 0 is root regardless of GID — reject."""
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"user": "0:1000"})


def test_from_dict_rejects_user_nonzero_uid_zero_gid() -> None:
    """GID 0 is the root group — even with a non-root UID, group-root
    membership is enough to read root-owned files. Reject."""
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"user": "1000:0"})


def test_from_dict_rejects_user_uid_only() -> None:
    """``user: "1000"`` (no GID) makes docker default the GID to the
    image's default group, which is typically root. Require explicit
    ``UID:GID`` so the operator commits to a non-root GID."""
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"user": "1000"})


def test_from_dict_rejects_user_non_numeric() -> None:
    """Non-numeric forms (``"abc:def"``, ``"appuser"``) cannot be
    statically verified to be non-root and are rejected at ingest."""
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"user": "abc:def"})


def test_from_dict_rejects_user_named_account() -> None:
    """A named account like ``appuser`` may map to UID 0 inside the
    container; without a numeric UID:GID we cannot verify non-root."""
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"user": "appuser"})


def test_from_dict_normalizes_empty_string_user_to_none() -> None:
    """An empty string for ``user`` is normalized to ``None`` (means
    "no --user flag") — confirm that path so the empty-string edge
    case can never sneak through as a literal docker arg."""
    policy = SandboxPolicy.from_dict({"user": ""})
    assert policy.user is None


def test_from_dict_rejects_user_with_whitespace() -> None:
    """Surrounding whitespace would be passed through as-is to docker
    and is not a valid UID:GID spelling — reject."""
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"user": " 1000:1000 "})


def test_from_dict_rejects_user_negative_uid() -> None:
    """Negative numbers are syntactically invalid for UID:GID."""
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"user": "-1:1000"})


def test_from_dict_rejects_user_integer_zero() -> None:
    """``user: 0`` in YAML is parsed as integer 0, which is falsy in
    Python. The previous ``if value:`` guard silently mapped this to
    ``user=None``, dropping the rootless default. Validation must
    treat it as a bad value, not a missing one."""
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"user": 0})


def test_from_dict_rejects_user_with_trailing_newline() -> None:
    """``re.match`` with ``$`` allows a trailing ``\\n`` — switching to
    ``re.fullmatch`` closes that hole. A newline-bearing string is not
    a valid argv element for ``docker --user``."""
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"user": "1000:1000\n"})


def test_constructor_accepts_root_user_directly() -> None:
    """Same asymmetry as ``cap_add`` / ``cap_drop``: the validation
    applies to the YAML/dict ingest path only. Direct construction is
    the audited internal-caller path; if a unit test or internal helper
    legitimately needs to assert that the adapter forwards ``--user 0``
    it must remain possible to construct that policy in Python.
    """
    policy = SandboxPolicy(user="0")
    assert policy.user == "0"


# ---------------------------------------------------------------------------
# DEFAULT_PROXY_IMAGE digest pin (security wave-2 eg8)
# ---------------------------------------------------------------------------
#
# Wave-1 review surfaced that ``DEFAULT_PROXY_IMAGE`` was pinned to the
# floating ``:latest`` tag. A supply-chain compromise of the upstream
# tag would give an attacker code execution inside the egress sidecar
# (which has bridge-network access). The fix is to pin to an immutable
# ``@sha256:<digest>`` reference so docker resolves the same image
# bytes every pull. These tests fail loudly if a future change reverts
# to a floating tag.


def test_default_proxy_image_pinned_to_sha256_digest() -> None:
    """The default proxy image must be pinned by digest, not tag."""
    assert DEFAULT_PROXY_IMAGE.startswith("vimagick/tinyproxy@sha256:")


def test_default_proxy_image_digest_has_expected_length() -> None:
    """A sha256 digest is exactly 64 lowercase hex chars after the prefix."""
    prefix = "vimagick/tinyproxy@sha256:"
    assert len(DEFAULT_PROXY_IMAGE) == len(prefix) + 64
    digest_hex = DEFAULT_PROXY_IMAGE[len(prefix) :]
    assert all(c in "0123456789abcdef" for c in digest_hex)
