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
    DEFAULT_PROXY_PORT,
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


@pytest.mark.parametrize(
    "bad_cap",
    [
        "SYS_ADMIN",
        "NET_ADMIN",
        "SYS_PTRACE",
        # ``ALL`` is the wildcard and must never be re-granted via cap_add.
        "ALL",
        # Linux capabilities are conventionally upper-case; lowercase forms
        # are not a valid Docker --cap-add spelling and must be rejected
        # rather than silently upper-cased.
        "sys_admin",
        # Even a cap whose upper-case form is in the safe set is rejected
        # if spelled with non-canonical casing — the YAML author should
        # fix the spelling.
        "Net_Bind_Service",
        # An unknown / made-up cap name has no place in the allowlist.
        "TOTALLY_FAKE_CAP",
    ],
    ids=[
        "sys-admin",
        "net-admin",
        "sys-ptrace",
        "all-wildcard",
        "lowercase",
        "mixed-case-safe",
        "unknown",
    ],
)
def test_from_dict_rejects_cap_add(bad_cap: str) -> None:
    """Each disallowed cap_add value must raise ValueError, and the
    error message must call out both the offending value and the
    ``cap_add`` field so a YAML author can locate the bad entry."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"cap_add": [bad_cap]})
    msg = str(excinfo.value)
    assert bad_cap in msg
    assert "cap_add" in msg


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


@pytest.mark.parametrize(
    "cap_drop",
    [
        # Empty list strips the drop-ALL baseline entirely — otherwise
        # ``cap_add: ["NET_RAW"]`` plus ``cap_drop: []`` silently restores
        # Docker's full default-cap set on top of the explicitly opted-in
        # cap.
        [],
        # A non-empty list missing ``ALL`` is also rejected — the operator
        # must explicitly preserve the drop-all baseline.
        ["NET_RAW"],
    ],
    ids=["empty", "without-all"],
)
def test_from_dict_rejects_cap_drop_missing_all(cap_drop: list[str]) -> None:
    """``cap_drop`` must always include ``ALL`` on the YAML/dict ingest
    path so the drop-all baseline is preserved; the error message must
    name ``ALL`` so the operator knows what to add back."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"cap_drop": cap_drop})
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


def test_from_dict_normalizes_empty_string_user_to_none() -> None:
    """An empty string for ``user`` is normalized to ``None`` (means
    "no --user flag") — confirm that path so the empty-string edge
    case can never sneak through as a literal docker arg."""
    policy = SandboxPolicy.from_dict({"user": ""})
    assert policy.user is None


@pytest.mark.parametrize(
    "bad_user",
    [
        # ``"root"`` is the named form of UID 0.
        "root",
        # ``"0"`` is the numeric form of root.
        "0",
        # ``"0:0"`` is root:root in numeric form.
        "0:0",
        # UID 0 is root regardless of GID.
        "0:1000",
        # GID 0 is the root group — even with a non-root UID, group-root
        # membership is enough to read root-owned files.
        "1000:0",
        # ``"1000"`` (no GID) makes docker default the GID to the image's
        # default group, which is typically root. Require explicit
        # ``UID:GID`` so the operator commits to a non-root GID.
        "1000",
        # Non-numeric forms cannot be statically verified to be non-root.
        "abc:def",
        # A named account like ``appuser`` may map to UID 0 inside the
        # container; without a numeric UID:GID we cannot verify non-root.
        "appuser",
        # Surrounding whitespace would be passed through as-is to docker
        # and is not a valid UID:GID spelling.
        " 1000:1000 ",
        # Negative numbers are syntactically invalid for UID:GID.
        "-1:1000",
        # ``user: 0`` in YAML is parsed as integer 0, which is falsy in
        # Python. The previous ``if value:`` guard silently mapped this
        # to ``user=None``, dropping the rootless default. Validation
        # must treat it as a bad value, not a missing one.
        0,
        # ``re.match`` with ``$`` allows a trailing ``\n`` — switching to
        # ``re.fullmatch`` closes that hole. A newline-bearing string is
        # not a valid argv element for ``docker --user``.
        "1000:1000\n",
    ],
    ids=[
        "root-string",
        "zero",
        "zero-zero",
        "zero-uid-nonzero-gid",
        "nonzero-uid-zero-gid",
        "uid-only",
        "non-numeric",
        "named-account",
        "with-whitespace",
        "negative-uid",
        "integer-zero",
        "trailing-newline",
    ],
)
def test_from_dict_rejects_user(bad_user: str | int) -> None:
    """Each disallowed ``user`` value must raise ValueError on the
    YAML/dict ingest path. The error message must name the ``user``
    field AND include a repr of the offending value (matching the
    validator's ``{user!r}`` formatter, which escapes whitespace like
    ``\\n``) so a YAML author can locate the bad entry without
    reading the validator. Covers the non-root-UID, non-root-GID,
    must-be-numeric-UID:GID, and must-be-fullmatch contracts of the
    ingest validator."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"user": bad_user})
    msg = str(excinfo.value)
    assert "user" in msg
    assert repr(str(bad_user)) in msg


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
    assert DEFAULT_PROXY_IMAGE.startswith("kalaksi/tinyproxy@sha256:")


def test_default_proxy_image_digest_has_expected_length() -> None:
    """A sha256 digest is exactly 64 lowercase hex chars after the prefix."""
    prefix = "kalaksi/tinyproxy@sha256:"
    assert len(DEFAULT_PROXY_IMAGE) == len(prefix) + 64
    digest_hex = DEFAULT_PROXY_IMAGE[len(prefix) :]
    assert all(c in "0123456789abcdef" for c in digest_hex)


# ---------------------------------------------------------------------------
# proxy_port type + range validation (security wave-4 z7i)
# ---------------------------------------------------------------------------
#
# Wave-1 review (LOW #1) flagged that ``from_dict`` did not range-check
# ``proxy_port`` after ``int()`` conversion. Wave-4 review expanded the
# scope: the dataclass constructor itself was unguarded, so internal
# direct-construction callers could pass a non-int (e.g. a shell-injection
# payload string like ``"8888; rm -rf /"``) which then flowed verbatim
# into ``_wait_for_proxy_ready``'s f-string URL and into
# ``_render_proxy_config``. The fix is symmetric: range check in
# ``from_dict`` (matches the existing cap_add / user / network_allowlist
# ingest validators) AND a self-defending type+range check in
# ``__post_init__`` so the dataclass cannot hold an invalid port
# regardless of how it was constructed.


# from_dict: range validation after int() coercion ------------------------


def test_from_dict_accepts_default_proxy_port() -> None:
    policy = SandboxPolicy.from_dict({"proxy_port": DEFAULT_PROXY_PORT})
    assert policy.proxy_port == DEFAULT_PROXY_PORT


@pytest.mark.parametrize("port", [1, 1024, 8888, 65535])
def test_from_dict_accepts_proxy_port_in_range(port: int) -> None:
    policy = SandboxPolicy.from_dict({"proxy_port": port})
    assert policy.proxy_port == port


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_from_dict_rejects_proxy_port_out_of_range(port: int) -> None:
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"proxy_port": port})
    msg = str(excinfo.value)
    assert "proxy_port" in msg


def test_from_dict_rejects_proxy_port_non_integer_string() -> None:
    """A non-numeric string fails ``int()`` conversion before our range
    check fires; either way the dict-ingest path must reject it."""
    with pytest.raises(ValueError):
        SandboxPolicy.from_dict({"proxy_port": "not-a-number"})


def test_from_dict_accepts_proxy_port_numeric_string() -> None:
    """A numeric string is coerced via ``int()`` and then range-checked
    — the existing dict-ingest contract is to accept stringified ints."""
    policy = SandboxPolicy.from_dict({"proxy_port": "8888"})
    assert policy.proxy_port == 8888


@pytest.mark.parametrize("bool_value", [True, False])
def test_from_dict_rejects_proxy_port_bool(bool_value: bool) -> None:
    """``int(True) == 1`` and ``int(False) == 0`` — without a pre-coercion
    bool guard, ``proxy_port: true`` in YAML would silently become port 1.
    Reject the bool type at the ingest boundary."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"proxy_port": bool_value})
    assert "proxy_port" in str(excinfo.value)


def test_from_dict_rejects_proxy_port_float() -> None:
    """``int(8888.5) == 8888`` silently truncates. Reject the float type
    at the ingest boundary so a fractional YAML value fails fast."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy.from_dict({"proxy_port": 8888.5})
    assert "proxy_port" in str(excinfo.value)


# __post_init__: type + range validation on direct construction -----------


def test_constructor_accepts_default_proxy_port() -> None:
    """Sanity check: the hardened default must still construct."""
    policy = SandboxPolicy()
    assert policy.proxy_port == DEFAULT_PROXY_PORT


@pytest.mark.parametrize("port", [1, 1024, 8888, 65535])
def test_constructor_accepts_proxy_port_in_range(port: int) -> None:
    policy = SandboxPolicy(proxy_port=port)
    assert policy.proxy_port == port


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_constructor_rejects_proxy_port_out_of_range(port: int) -> None:
    """Direct construction is no longer an audited-escape hatch for
    proxy_port — an out-of-range int flows into ``_wait_for_proxy_ready``
    and ``_render_proxy_config`` and must be blocked at the dataclass
    boundary."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy(proxy_port=port)
    assert "proxy_port" in str(excinfo.value)


def test_constructor_rejects_proxy_port_string() -> None:
    """The shell-injection surface motivator: a string proxy_port (e.g.
    ``"8888; rm -rf /"``) must be rejected before it can flow into the
    ``_wait_for_proxy_ready`` f-string URL or the ``_render_proxy_config``
    template. Unlike ``from_dict``, the constructor does no ``int()``
    coercion, so the type check must live in ``__post_init__``."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy(proxy_port="8888")  # type: ignore[arg-type]
    assert "proxy_port" in str(excinfo.value)


def test_constructor_rejects_proxy_port_injection_payload() -> None:
    """Concrete injection-style string — the exact attack the
    __post_init__ guard exists to defeat."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy(proxy_port="8888; rm -rf /")  # type: ignore[arg-type]
    assert "proxy_port" in str(excinfo.value)


def test_constructor_rejects_proxy_port_bool() -> None:
    """``bool`` is a subclass of ``int`` in Python (``isinstance(True,
    int)`` is True), so a bare ``True`` would slip past a naive ``int``
    check and become port 1. Reject explicitly — a boolean is never a
    valid port spelling."""
    with pytest.raises(ValueError) as excinfo:
        SandboxPolicy(proxy_port=True)  # type: ignore[arg-type]
    assert "proxy_port" in str(excinfo.value)
