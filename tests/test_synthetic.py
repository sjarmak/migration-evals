"""Tests for the synthetic Java-8 repo generator.

Covers acceptance criteria 1-3 of the synthetic-repos-and-ast-oracle work
unit: deterministic generation, ≥10-primitive coverage, 500 distinct repos at
seed=42.
"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import pytest

from migration_evals.synthetic import java8_generator
from migration_evals.synthetic.primitives import ALL_MODULES


def _hash_dir(path: Path) -> str:
    """Stable content hash for a directory tree."""
    hasher = hashlib.sha256()
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(path).as_posix()
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(file_path.read_bytes())
        hasher.update(b"\x01")
    return hasher.hexdigest()


def test_generator_primitives_constant_has_all_ten() -> None:
    assert len(java8_generator.GENERATOR_PRIMITIVES) == 10
    module_names = {m.NAME for m in ALL_MODULES}
    assert java8_generator.GENERATOR_PRIMITIVES == module_names


def test_primitive_modules_self_report(tmp_path: Path) -> None:
    """Each primitive module is importable and its contract returns a dict."""
    import random

    for module in ALL_MODULES:
        assert isinstance(module.NAME, str)
        out = tmp_path / module.NAME
        out.mkdir(parents=True, exist_ok=True)
        result = module.generate(random.Random(1234), out)
        assert isinstance(result, dict)
        assert result["primitive"] == module.NAME


def test_generator_smoke_count_10(tmp_path: Path) -> None:
    out = tmp_path / "gen"
    rc = java8_generator.main(["--out", str(out), "--count", "10", "--seed", "42"])
    assert rc == 0

    repos = sorted(out.glob("repo_*"))
    assert len(repos) == 10
    for repo in repos:
        assert (repo / "pom.xml").exists()
        assert (repo / "emission.json").exists()
        java_files = list((repo / "src" / "main" / "java").rglob("*.java"))
        assert java_files, f"no java files in {repo}"
        pom = (repo / "pom.xml").read_text()
        assert "<maven.compiler.source>1.8" in pom
        assert "<maven.compiler.target>1.8" in pom


def test_generator_determinism(tmp_path: Path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    java8_generator.generate(out_a, count=5, seed=99)
    java8_generator.generate(out_b, count=5, seed=99)
    hash_a = _hash_dir(out_a)
    hash_b = _hash_dir(out_b)
    assert hash_a == hash_b


def test_generator_500_distinct_seed42(tmp_path: Path) -> None:
    """Acceptance criterion 3: --count 500 --seed 42 produces 500 distinct repos."""
    out = tmp_path / "gen500"
    start = time.perf_counter()
    repos = java8_generator.generate(out, count=500, seed=42)
    elapsed = time.perf_counter() - start
    assert len(repos) == 500

    hashes = {_hash_dir(repo) for repo in repos}
    # Log wall time for evidence capture.
    print(f"[test_generator_500_distinct_seed42] wall_time={elapsed:.2f}s distinct={len(hashes)}")
    assert len(hashes) == 500, f"expected 500 distinct repos, got {len(hashes)} unique hashes"


def test_generator_emission_manifest_matches_primitives(tmp_path: Path) -> None:
    """emission.json enumerates primitives actually emitted, all in generator set."""
    out = tmp_path / "gen"
    java8_generator.generate(out, count=5, seed=7)
    import json

    for repo in sorted(out.glob("repo_*")):
        manifest = json.loads((repo / "emission.json").read_text())
        declared = set(manifest["primitives"])
        assert declared.issubset(java8_generator.GENERATOR_PRIMITIVES)
        emitted = {e["primitive"] for e in manifest["emissions"]}
        assert emitted == declared
