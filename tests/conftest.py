"""Shared pytest fixtures for migration-evals tests.

Currently houses only the git-fixture helpers used by tests that need
a local file:// remote (changeset-puller and run-eval driver smokes).
Promote new fixtures here when they're consumed by 2+ test modules.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGESET_EXAMPLES = _REPO_ROOT / "tests" / "fixtures" / "changeset_examples"


def _git(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *cmd], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _populate_default(seed: Path) -> None:
    (seed / "foo.txt").write_text("hello\n")


def _make_seed_repo(
    root: Path,
    *,
    populate: Callable[[Path], None] = _populate_default,
    label: str = "init",
) -> tuple[Path, str]:
    """Init a one-commit git repo at ``root/seed`` and return (path, sha).

    ``populate(seed)`` writes the initial files; the default writes
    ``foo.txt: hello\\n`` so the legacy fixture stays untouched. Pass a
    custom populator (e.g. ``lambda d: shutil.copytree(src, d, ...)``)
    to seed from a directory tree.
    """
    src = root / "seed"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=src)
    _git(["config", "user.email", "test@example.com"], cwd=src)
    _git(["config", "user.name", "test"], cwd=src)
    populate(src)
    _git(["add", "-A"], cwd=src)
    _git(["commit", "-q", "-m", label], cwd=src)
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


def _seed_from_example(
    tmp_path_factory: pytest.TempPathFactory,
    example_subpath: str,
    label: str,
) -> tuple[str, str]:
    """Build a seeded bare remote from a committed canonical example."""
    src_dir = _CHANGESET_EXAMPLES / example_subpath / "repo_state"
    root = tmp_path_factory.mktemp(f"seed-{label}")
    seed, sha = _make_seed_repo(
        root,
        populate=lambda dst: shutil.copytree(src_dir, dst, dirs_exist_ok=True),
        label=f"init {label}",
    )
    url = _make_bare_remote(seed, root)
    return url, sha


@pytest.fixture(scope="session")
def seeded_go_import_remote(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[str, str]:
    """File:// URL + SHA for the canonical Go-import-rewrite example."""
    return _seed_from_example(
        tmp_path_factory,
        "go_import_rewrite/ghodss_to_sigs",
        "go-import-rewrite",
    )


@pytest.fixture(scope="session")
def seeded_dockerfile_bump_remote(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[str, str]:
    """File:// URL + SHA for the canonical Dockerfile-base-image-bump example."""
    return _seed_from_example(
        tmp_path_factory,
        "dockerfile_base_image_bump/alpine_to_debian",
        "dockerfile-bump",
    )
