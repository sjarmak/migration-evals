"""Fixture ``meta.json`` loading + Recipe construction.

Shared by both the CLI (:mod:`migration_evals.cli`) and the config-driven
runner (:mod:`migration_evals.runner`). It lives in the harness package so the
execution core never has to reach up into the CLI layer for these helpers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from migration_evals.harness.recipe import Recipe


def _load_repo_meta(repo_dir: Path) -> dict[str, Any]:
    meta_path = repo_dir / "meta.json"
    if not meta_path.is_file():
        return {}
    try:
        return json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return {}


def _build_recipe_from_meta(meta: Mapping[str, Any]) -> Recipe:
    """Construct a :class:`Recipe` from a fixture repo's ``meta.json``."""
    dockerfile = (
        meta.get("dockerfile") or "FROM maven:3.9-eclipse-temurin-17\nWORKDIR /src\nCOPY . .\n"
    )
    build_cmd = meta.get("build_cmd") or "mvn -B -e compile"
    test_cmd = meta.get("test_cmd") or "mvn -B -e test"
    provenance = meta.get("harness_provenance") or {
        "model": "claude-haiku-4-5",
        "prompt_version": "v1",
        "timestamp": "2026-04-24T00:00:00Z",
    }
    return Recipe(
        dockerfile=dockerfile,
        build_cmd=build_cmd,
        test_cmd=test_cmd,
        harness_provenance=provenance,
    )


__all__ = ["_load_repo_meta", "_build_recipe_from_meta"]
