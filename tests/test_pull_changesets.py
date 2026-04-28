"""Tests for scripts/pull_changesets.py.

The script pulls agent changesets out of artifact storage (via a
ChangesetProvider) and stages them in the funnel layout under
``<out-root>/<instance_id>/{repo, repo/patch.diff, meta.json}``.

These tests avoid the network by pointing the clone step at a local
bare-clone URL ("file://...") and using the reference
FilesystemChangesetProvider for meta + patch.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from migration_evals.changesets import FilesystemChangesetProvider

# `seeded_remote` and the git helpers come from tests/conftest.py.

_REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = _REPO_ROOT / "scripts" / "pull_changesets.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("pull_changesets", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["pull_changesets"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pc():
    return _load_module()


def _git(cmd: list[str], cwd: Path) -> str:
    """Local git helper used for end-to-end assertions inside test bodies."""
    proc = subprocess.run(["git", *cmd], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _valid_patch_for_seed() -> str:
    """A unified diff that applies cleanly to the seed repo (foo.txt: hello -> world)."""
    return (
        "diff --git a/foo.txt b/foo.txt\n"
        "--- a/foo.txt\n"
        "+++ b/foo.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+world\n"
    )


def _broken_patch() -> str:
    """A unified diff that cannot apply (wrong source context)."""
    return (
        "diff --git a/foo.txt b/foo.txt\n"
        "--- a/foo.txt\n"
        "+++ b/foo.txt\n"
        "@@ -1 +1 @@\n"
        "-totally-not-what-is-there\n"
        "+world\n"
    )


def _stage_provider_dir(
    root: Path,
    instance_id: str,
    *,
    repo_url: str,
    commit_sha: str,
    patch: str,
) -> None:
    d = root / instance_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "repo_url": repo_url,
                "commit_sha": commit_sha,
                "workflow_id": f"wf-{instance_id}",
                "agent_runner": "claude_code",
                "agent_model": "claude-sonnet-4-6",
            }
        )
    )
    (d / "patch.diff").write_text(patch)


# -- stage_instance end-to-end --------------------------------------------


def test_stage_instance_valid_patch_populates_layout(pc, tmp_path: Path, seeded_remote) -> None:
    url, sha = seeded_remote
    staged = tmp_path / "staged"
    _stage_provider_dir(
        staged, "inst-ok", repo_url=url, commit_sha=sha, patch=_valid_patch_for_seed()
    )
    out_root = tmp_path / "eval"

    provider = FilesystemChangesetProvider(staged)
    result = pc.stage_instance(provider, "inst-ok", out_root)

    assert result.error is None
    assert result.ok is True
    inst_root = out_root / "inst-ok"
    assert inst_root.is_dir()
    assert (inst_root / "repo").is_dir()
    assert (inst_root / "repo" / "patch.diff").is_file()
    assert (inst_root / "meta.json").is_file()

    meta = json.loads((inst_root / "meta.json").read_text())
    assert meta["repo_url"] == url
    assert meta["commit_sha"] == sha
    assert meta["agent_runner"] == "claude_code"
    assert meta["agent_model"] == "claude-sonnet-4-6"
    assert meta["workflow_id"] == "wf-inst-ok"

    # repo/ is actually a git working tree at the expected commit.
    head = _git(["rev-parse", "HEAD"], cwd=inst_root / "repo")
    assert head == sha


def test_stage_instance_broken_patch_populates_but_flags_apply_fail(
    pc, tmp_path: Path, seeded_remote
) -> None:
    url, sha = seeded_remote
    staged = tmp_path / "staged"
    _stage_provider_dir(staged, "inst-bad", repo_url=url, commit_sha=sha, patch=_broken_patch())
    out_root = tmp_path / "eval"

    provider = FilesystemChangesetProvider(staged)
    result = pc.stage_instance(provider, "inst-bad", out_root)

    assert result.staged_dir == out_root / "inst-bad"
    assert result.ok is False
    assert result.error is not None and "git apply --check" in result.error
    # Files are still on disk for the caller to inspect.
    assert (out_root / "inst-bad" / "repo" / "patch.diff").is_file()
    assert (out_root / "inst-bad" / "meta.json").is_file()


def test_stage_instance_rejects_traversal_instance_id(pc, tmp_path: Path) -> None:
    """A malicious instance_id must not escape out_root or trigger rmtree."""
    out_root = tmp_path / "eval"
    out_root.mkdir()
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("must not be deleted")

    provider = FilesystemChangesetProvider(tmp_path)
    result = pc.stage_instance(provider, "../sentinel.txt", out_root)

    assert result.error is not None and "invalid instance_id" in result.error
    assert result.staged_dir is None
    assert sentinel.is_file(), "traversal id must not lead to filesystem mutation"


def test_stage_instance_missing_in_provider_returns_fetch_error(pc, tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    out_root = tmp_path / "eval"

    provider = FilesystemChangesetProvider(staged)
    result = pc.stage_instance(provider, "missing", out_root)

    assert result.error is not None and result.error.startswith("fetch failed")
    assert result.staged_dir is None
    assert result.ok is False


# -- main: N instance ids produce N populated dirs ------------------------


def test_main_stages_multiple_instances(pc, tmp_path: Path, seeded_remote) -> None:
    url, sha = seeded_remote
    staged = tmp_path / "staged"
    for iid in ("a", "b", "c"):
        _stage_provider_dir(
            staged, iid, repo_url=url, commit_sha=sha, patch=_valid_patch_for_seed()
        )
    out_root = tmp_path / "eval"

    rc = pc.main(
        [
            "--provider",
            "filesystem",
            "--root",
            str(staged),
            "--out-root",
            str(out_root),
            "a",
            "b",
            "c",
        ]
    )
    assert rc == 0
    for iid in ("a", "b", "c"):
        assert (out_root / iid / "repo" / "patch.diff").is_file()
        assert (out_root / iid / "meta.json").is_file()
        # git apply --check succeeds.
        subprocess.run(
            ["git", "apply", "--check", "patch.diff"],
            cwd=out_root / iid / "repo",
            check=True,
        )


def test_main_mixed_pass_fail_returns_exit_2(pc, tmp_path: Path, seeded_remote) -> None:
    url, sha = seeded_remote
    staged = tmp_path / "staged"
    _stage_provider_dir(staged, "good", repo_url=url, commit_sha=sha, patch=_valid_patch_for_seed())
    _stage_provider_dir(staged, "bad", repo_url=url, commit_sha=sha, patch=_broken_patch())
    out_root = tmp_path / "eval"

    rc = pc.main(
        [
            "--provider",
            "filesystem",
            "--root",
            str(staged),
            "--out-root",
            str(out_root),
            "good",
            "bad",
        ]
    )
    assert rc == 2
    assert (out_root / "good" / "repo" / "patch.diff").is_file()
    assert (out_root / "bad" / "repo" / "patch.diff").is_file()


def test_main_no_ids_returns_exit_1(pc, tmp_path: Path) -> None:
    rc = pc.main(
        ["--provider", "filesystem", "--root", str(tmp_path), "--out-root", str(tmp_path / "e")]
    )
    assert rc == 1


def test_main_instance_ids_file(pc, tmp_path: Path, seeded_remote) -> None:
    url, sha = seeded_remote
    staged = tmp_path / "staged"
    _stage_provider_dir(staged, "x", repo_url=url, commit_sha=sha, patch=_valid_patch_for_seed())
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("# header\nx\n\n")
    out_root = tmp_path / "eval"

    rc = pc.main(
        [
            "--provider",
            "filesystem",
            "--root",
            str(staged),
            "--out-root",
            str(out_root),
            "--instance-ids-file",
            str(ids_file),
        ]
    )
    assert rc == 0
    assert (out_root / "x" / "repo" / "patch.diff").is_file()
