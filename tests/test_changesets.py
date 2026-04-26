"""Unit tests for the ChangesetProvider interface, the registry, and the
shipped reference providers (``FilesystemChangesetProvider``,
``HTTPChangesetProvider``).

The provider abstraction lets the funnel pull agent-produced diffs from
any artifact-storage backend (filesystem, HTTP artifact server,
S3-compatible object store, blob storage, ...) behind a single
:func:`fetch` call. The two implementations that ship in-repo are
reference templates that production backends can copy-modify.
"""

from __future__ import annotations

import contextlib
import json
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Iterator

import pytest

from migration_evals.changesets import (
    Changeset,
    ChangesetProvider,
    FilesystemChangesetProvider,
    HTTPChangesetProvider,
    get_provider,
    register_provider,
    unregister_provider,
    validate_commit_sha,
    validate_instance_id,
)


_META_FIXTURE = {
    "repo_url": "https://github.com/example/foo",
    "commit_sha": "abcdef1234567890abcdef1234567890abcdef12",
    "workflow_id": "wf-42",
    "agent_runner": "claude_code",
    "agent_model": "claude-sonnet-4-6",
}

_PATCH_FIXTURE = """\
--- a/src/Foo.java
+++ b/src/Foo.java
@@ -1,3 +1,3 @@
 class Foo {
-    void bar() {}
+    void bar() { return; }
 }
"""


def _stage_instance(root: Path, instance_id: str, *, meta=None, patch=None) -> Path:
    inst_dir = root / instance_id
    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / "meta.json").write_text(json.dumps(meta if meta is not None else _META_FIXTURE))
    (inst_dir / "patch.diff").write_text(patch if patch is not None else _PATCH_FIXTURE)
    return inst_dir


# -- FilesystemChangesetProvider.fetch -------------------------------------


def test_filesystem_provider_fetch_reads_meta_and_patch(tmp_path: Path) -> None:
    _stage_instance(tmp_path, "inst-1")
    provider = FilesystemChangesetProvider(tmp_path)

    cs = provider.fetch("inst-1")

    assert isinstance(cs, Changeset)
    assert cs.instance_id == "inst-1"
    assert cs.repo_url == "https://github.com/example/foo"
    assert cs.commit_sha == "abcdef1234567890abcdef1234567890abcdef12"
    assert cs.workflow_id == "wf-42"
    assert cs.agent_runner == "claude_code"
    assert cs.agent_model == "claude-sonnet-4-6"
    assert cs.patch_diff == _PATCH_FIXTURE


def test_filesystem_provider_fetch_missing_instance_raises(tmp_path: Path) -> None:
    provider = FilesystemChangesetProvider(tmp_path)
    with pytest.raises(FileNotFoundError, match="no-such-id"):
        provider.fetch("no-such-id")


def test_filesystem_provider_fetch_missing_patch_raises(tmp_path: Path) -> None:
    inst_dir = tmp_path / "inst-2"
    inst_dir.mkdir()
    (inst_dir / "meta.json").write_text(json.dumps(_META_FIXTURE))

    provider = FilesystemChangesetProvider(tmp_path)
    with pytest.raises(FileNotFoundError, match="patch.diff"):
        provider.fetch("inst-2")


def test_filesystem_provider_fetch_missing_meta_key_raises(tmp_path: Path) -> None:
    bad_meta = {k: v for k, v in _META_FIXTURE.items() if k != "commit_sha"}
    _stage_instance(tmp_path, "inst-3", meta=bad_meta)

    provider = FilesystemChangesetProvider(tmp_path)
    with pytest.raises(KeyError, match="commit_sha"):
        provider.fetch("inst-3")


def test_filesystem_provider_satisfies_protocol(tmp_path: Path) -> None:
    provider = FilesystemChangesetProvider(tmp_path)
    assert isinstance(provider, ChangesetProvider)


# -- get_provider factory --------------------------------------------------


def test_get_provider_returns_filesystem_impl(tmp_path: Path) -> None:
    provider = get_provider("filesystem", {"root": str(tmp_path)})
    assert isinstance(provider, FilesystemChangesetProvider)


def test_get_provider_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        get_provider("s3-mystery-backend", {})


def test_get_provider_filesystem_requires_root() -> None:
    with pytest.raises(KeyError, match="root"):
        get_provider("filesystem", {})


# -- security: instance_id validation --------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../escape",
        "/etc/passwd",
        "a/b",
        "..",
        ".",
        "",
        "name with space",
        "name;rm-rf",
    ],
)
def test_validate_instance_id_rejects_unsafe(bad_id: str) -> None:
    with pytest.raises(ValueError, match="unsafe instance_id"):
        validate_instance_id(bad_id)


@pytest.mark.parametrize(
    "good_id",
    ["inst-1", "wf_42", "abc.def", "Run-2026-04-24", "a"],
)
def test_validate_instance_id_accepts_safe(good_id: str) -> None:
    validate_instance_id(good_id)


def test_filesystem_provider_rejects_traversal_instance_id(tmp_path: Path) -> None:
    provider = FilesystemChangesetProvider(tmp_path)
    with pytest.raises(ValueError, match="unsafe instance_id"):
        provider.fetch("../outside")


# -- security: commit_sha validation ---------------------------------------


@pytest.mark.parametrize(
    "bad_sha",
    [
        "HEAD",
        "main",
        "abcdef1",  # too short
        "abcdef1234567890abcdef1234567890abcdef1Z",  # non-hex
        "ABCDEF1234567890ABCDEF1234567890ABCDEF12",  # uppercase
        "",
    ],
)
def test_validate_commit_sha_rejects_non_full_sha(bad_sha: str) -> None:
    with pytest.raises(ValueError, match="40-char lowercase hex SHA-1"):
        validate_commit_sha(bad_sha)


def test_filesystem_provider_rejects_non_sha_meta(tmp_path: Path) -> None:
    bad_meta = {**_META_FIXTURE, "commit_sha": "main"}
    _stage_instance(tmp_path, "inst-bad-sha", meta=bad_meta)
    provider = FilesystemChangesetProvider(tmp_path)
    with pytest.raises(ValueError, match="40-char lowercase hex SHA-1"):
        provider.fetch("inst-bad-sha")


# -- HTTPChangesetProvider --------------------------------------------------


@contextlib.contextmanager
def _serve_directory(root: Path) -> Iterator[str]:
    """Yield a base URL for a stdlib http.server rooted at ``root``.

    Binds to port 0 so concurrent test invocations do not collide. The
    server thread is shut down on context exit.
    """

    class _RootedHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            # Silence stdlib's per-request stderr noise during tests.
            return

    server = HTTPServer(("127.0.0.1", 0), _RootedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_provider_fetch_round_trip(tmp_path: Path) -> None:
    """Serve a staged corpus over stdlib http.server and fetch it back.

    Mirrors the FilesystemChangesetProvider round-trip test: the same
    corpus on disk should be retrievable via either provider with no
    behavioural difference beyond transport.
    """
    _stage_instance(tmp_path, "inst-http-1")
    with _serve_directory(tmp_path) as base_url:
        provider = HTTPChangesetProvider(base_url, timeout_s=5.0)
        cs = provider.fetch("inst-http-1")
    assert isinstance(cs, Changeset)
    assert cs.instance_id == "inst-http-1"
    assert cs.repo_url == _META_FIXTURE["repo_url"]
    assert cs.commit_sha == _META_FIXTURE["commit_sha"]
    assert cs.workflow_id == _META_FIXTURE["workflow_id"]
    assert cs.agent_runner == _META_FIXTURE["agent_runner"]
    assert cs.agent_model == _META_FIXTURE["agent_model"]
    assert cs.patch_diff == _PATCH_FIXTURE


def test_http_provider_missing_instance_raises_file_not_found(tmp_path: Path) -> None:
    """HTTP 404 must surface as FileNotFoundError to mirror the FS provider's contract."""
    with _serve_directory(tmp_path) as base_url:
        provider = HTTPChangesetProvider(base_url, timeout_s=5.0)
        with pytest.raises(FileNotFoundError, match="meta.json not found"):
            provider.fetch("missing-id")


def test_http_provider_invalid_meta_raises(tmp_path: Path) -> None:
    bad_meta = {k: v for k, v in _META_FIXTURE.items() if k != "agent_model"}
    _stage_instance(tmp_path, "inst-bad-meta", meta=bad_meta)
    with _serve_directory(tmp_path) as base_url:
        provider = HTTPChangesetProvider(base_url, timeout_s=5.0)
        with pytest.raises(KeyError, match="agent_model"):
            provider.fetch("inst-bad-meta")


def test_http_provider_rejects_traversal_instance_id() -> None:
    provider = HTTPChangesetProvider("http://127.0.0.1:1", timeout_s=1.0)
    with pytest.raises(ValueError, match="unsafe instance_id"):
        provider.fetch("../escape")


def test_http_provider_satisfies_protocol() -> None:
    provider = HTTPChangesetProvider("http://127.0.0.1:1")
    assert isinstance(provider, ChangesetProvider)


def test_http_provider_unreachable_host_raises_connection_error() -> None:
    """Network failure must propagate as ConnectionError, not silently succeed.

    Bound a tight timeout against an unroutable port so the test does
    not hang the suite on misconfigured DNS.
    """
    provider = HTTPChangesetProvider(
        "http://127.0.0.1:1", timeout_s=0.5
    )
    with pytest.raises((ConnectionError, OSError)):
        provider.fetch("anything")


def test_get_provider_http_requires_base_url() -> None:
    with pytest.raises(KeyError, match="base_url"):
        get_provider("http", {})


def test_get_provider_returns_http_impl() -> None:
    provider = get_provider("http", {"base_url": "http://example.invalid"})
    assert isinstance(provider, HTTPChangesetProvider)


# -- HTTP hardening: size cap + cross-origin redirects ----------------------


def test_http_provider_response_size_cap_rejects_oversized(tmp_path: Path) -> None:
    """A response larger than max_bytes raises ValueError, not silent truncation."""
    inst_dir = tmp_path / "inst-big"
    inst_dir.mkdir()
    (inst_dir / "meta.json").write_text(json.dumps(_META_FIXTURE))
    # patch.diff bigger than the small cap we configure below.
    (inst_dir / "patch.diff").write_text("X" * 8192)

    with _serve_directory(tmp_path) as base_url:
        provider = HTTPChangesetProvider(
            base_url, timeout_s=5.0, max_bytes=4096
        )
        with pytest.raises(ValueError, match="exceeded max_bytes=4096"):
            provider.fetch("inst-big")


def test_http_provider_response_size_cap_accepts_at_boundary(tmp_path: Path) -> None:
    """A response exactly at the cap succeeds (off-by-one guard)."""
    inst_dir = tmp_path / "inst-tight"
    inst_dir.mkdir()
    (inst_dir / "meta.json").write_text(json.dumps(_META_FIXTURE))
    body = "X" * 4096
    (inst_dir / "patch.diff").write_text(body)

    with _serve_directory(tmp_path) as base_url:
        provider = HTTPChangesetProvider(
            base_url, timeout_s=5.0, max_bytes=4096
        )
        cs = provider.fetch("inst-tight")
    assert cs.patch_diff == body


def _serve_redirect_to(target_url: str) -> "tuple[str, threading.Event]":
    """Start an http.server that 302-redirects every GET to target_url.

    Returns ``(base_url, shutdown_event)``. Caller sets shutdown_event
    after the test to stop the server.
    """
    shutdown = threading.Event()

    class _Redirector(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _Redirector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]

    def _stop() -> None:
        shutdown.wait()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    threading.Thread(target=_stop, daemon=True).start()
    return f"http://{host}:{port}", shutdown


def test_http_provider_refuses_cross_origin_redirect(tmp_path: Path) -> None:
    """A redirect that changes host must be refused, not silently followed.

    Default urllib follows http(s) redirects across hosts; the
    reference provider blocks that to prevent leaking operator-supplied
    headers (auth tokens, etc.) to a different origin.
    """
    # Stand up a real upstream the redirector points at, but on a
    # different port so it counts as a different origin.
    upstream_root = tmp_path / "upstream"
    upstream_root.mkdir()
    _stage_instance(upstream_root, "inst-target")
    with _serve_directory(upstream_root) as upstream_base:
        redirector_base, shutdown = _serve_redirect_to(
            f"{upstream_base}/inst-target/meta.json"
        )
        try:
            provider = HTTPChangesetProvider(redirector_base, timeout_s=5.0)
            # The redirect handler raises HTTPError, which urllib
            # bubbles up; we don't translate it specifically because
            # cross-origin redirects are an integration mistake, not a
            # user-recoverable runtime condition.
            with pytest.raises(Exception):
                provider.fetch("inst-target")
        finally:
            shutdown.set()


def test_http_factory_threads_max_bytes_through_config() -> None:
    """get_provider('http', {'max_bytes': N}) must reach the provider."""
    provider = get_provider(
        "http", {"base_url": "http://example.invalid", "max_bytes": 1024}
    )
    assert isinstance(provider, HTTPChangesetProvider)
    assert provider._max_bytes == 1024  # noqa: SLF001 — test-only inspection


# -- register_provider registry --------------------------------------------


def test_register_provider_round_trip() -> None:
    """A custom factory registered under a fresh name must be reachable
    via get_provider with the config it expects."""
    sentinel_root: list[str] = []

    class _Stub:
        def __init__(self, root: str) -> None:
            sentinel_root.append(root)

        def fetch(self, instance_id: str) -> Changeset:  # pragma: no cover
            raise NotImplementedError

    def _factory(config):
        return _Stub(config["root"])

    register_provider("test_stub_provider", _factory)
    try:
        provider = get_provider(
            "test_stub_provider", {"root": "/tmp/whatever"}
        )
        assert isinstance(provider, _Stub)
        assert sentinel_root == ["/tmp/whatever"]
    finally:
        unregister_provider("test_stub_provider")


def test_register_provider_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        register_provider("", lambda cfg: FilesystemChangesetProvider("/"))


def test_get_provider_unknown_name_lists_known_providers() -> None:
    """Error message names every currently-registered provider so the
    operator can self-diagnose a typo without reading the source."""
    with pytest.raises(ValueError, match="known providers:"):
        get_provider("definitely-not-real", {})
