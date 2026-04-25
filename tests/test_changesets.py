"""Unit tests for the ChangesetProvider interface and FilesystemChangesetProvider.

The provider abstraction lets the funnel pull agent-produced diffs from
any artifact-storage backend (filesystem, S3-compatible object store,
blob storage, ...) behind a single :func:`fetch` call. Only the
filesystem implementation ships in-repo; it is the reference provider
used by tests and by pre-staged fixture runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from migration_evals.changesets import (
    Changeset,
    ChangesetProvider,
    FilesystemChangesetProvider,
    get_provider,
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
