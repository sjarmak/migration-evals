"""Shared pytest fixtures for migration-evals tests.

Currently houses only the git-fixture helpers used by tests that need
a local file:// remote (changeset-puller and run-eval driver smokes).
Promote new fixtures here when they're consumed by 2+ test modules.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *cmd], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _make_seed_repo(root: Path) -> tuple[Path, str]:
    """Init a one-commit git repo at ``root/seed`` and return (path, sha)."""
    src = root / "seed"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=src)
    _git(["config", "user.email", "test@example.com"], cwd=src)
    _git(["config", "user.name", "test"], cwd=src)
    (src / "foo.txt").write_text("hello\n")
    _git(["add", "foo.txt"], cwd=src)
    _git(["commit", "-q", "-m", "init"], cwd=src)
    sha = _git(["rev-parse", "HEAD"], cwd=src)
    return src, sha


def _make_bare_remote(seed: Path, root: Path) -> str:
    """Create a bare clone of ``seed`` and return a file:// URL."""
    bare = root / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)], check=True
    )
    return f"file://{bare}"


@pytest.fixture(scope="session")
def seeded_remote(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """Session-scoped (file:// URL, commit_sha) for a one-commit bare repo.

    Read-only after creation, so safe to share across tests. Avoids
    re-running ~5 git subprocesses per test.
    """
    root = tmp_path_factory.mktemp("seed-remote")
    seed, sha = _make_seed_repo(root)
    url = _make_bare_remote(seed, root)
    return url, sha


def _seed_remote_from_dir(
    tmp_path_factory: pytest.TempPathFactory, src_dir: Path, label: str
) -> tuple[str, str]:
    """Build a seeded git remote whose initial commit mirrors ``src_dir``.

    Returns ``(file_url, commit_sha)``. The initial commit contains a
    verbatim copy of ``src_dir``'s contents; the bare clone served at
    ``file_url`` is read-only after creation.
    """
    root = tmp_path_factory.mktemp(f"seed-{label}")
    seed = root / "seed"
    seed.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=seed)
    _git(["config", "user.email", "test@example.com"], cwd=seed)
    _git(["config", "user.name", "test"], cwd=seed)
    # Mirror the directory tree into the seed repo.
    for path in src_dir.rglob("*"):
        rel = path.relative_to(src_dir)
        dest = seed / rel
        if path.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(path.read_bytes())
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-q", "-m", f"init {label}"], cwd=seed)
    sha = _git(["rev-parse", "HEAD"], cwd=seed)
    url = _make_bare_remote(seed, root)
    return url, sha


_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def seeded_go_import_remote(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[str, str]:
    """File:// URL + SHA for the canonical Go-import-rewrite example."""
    src = (
        _REPO_ROOT
        / "tests"
        / "fixtures"
        / "changeset_examples"
        / "go_import_rewrite"
        / "ghodss_to_sigs"
        / "repo_state"
    )
    return _seed_remote_from_dir(tmp_path_factory, src, "go-import-rewrite")


@pytest.fixture(scope="session")
def seeded_dockerfile_bump_remote(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[str, str]:
    """File:// URL + SHA for the canonical Dockerfile-base-image-bump example."""
    src = (
        _REPO_ROOT
        / "tests"
        / "fixtures"
        / "changeset_examples"
        / "dockerfile_base_image_bump"
        / "alpine_to_debian"
        / "repo_state"
    )
    return _seed_remote_from_dir(tmp_path_factory, src, "dockerfile-bump")
