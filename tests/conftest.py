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
