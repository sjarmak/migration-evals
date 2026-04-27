"""Tests for the recipe-template sandbox_policy merge in runner.py (ct4).

The runner reads ``adapters.sandbox_policy`` from the smoke YAML and
must also pick up a top-level ``sandbox_policy`` block from the recipe
template referenced by ``stamps.recipe_spec``. The smoke YAML wins
per-key (shallow merge) — recipe template provides defaults the smoke
config can override or extend.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from migration_evals.runner import (  # noqa: E402
    _load_recipe_template_sandbox_policy,
    _merge_sandbox_policy,
    run_from_config,
)


def _write_template(
    tmp_path: Path,
    *,
    sandbox_policy: dict | None = None,
    name: str = "fake_mig.yaml",
) -> Path:
    template = {
        "migration_id": "fake_mig",
        "recipe": {
            "dockerfile": "FROM alpine\n",
            "build_cmd": "true",
            "test_cmd": "true",
        },
        "stamps": {
            "oracle_spec": "configs/oracle_spec.yaml",
            "recipe_spec": str(tmp_path / name),
            "hypotheses": "docs/hypotheses_and_thresholds.md",
        },
    }
    if sandbox_policy is not None:
        template["sandbox_policy"] = sandbox_policy
    path = tmp_path / name
    path.write_text(yaml.safe_dump(template), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _load_recipe_template_sandbox_policy
# ---------------------------------------------------------------------------


def test_load_returns_empty_dict_when_path_is_none() -> None:
    assert _load_recipe_template_sandbox_policy(None) == {}


def test_load_returns_empty_dict_when_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    assert _load_recipe_template_sandbox_policy(missing) == {}


def test_load_returns_empty_dict_when_yaml_invalid(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(":::not yaml:::\n", encoding="utf-8")
    assert _load_recipe_template_sandbox_policy(bad) == {}


def test_load_returns_empty_dict_when_template_lacks_block(tmp_path: Path) -> None:
    path = _write_template(tmp_path)
    assert _load_recipe_template_sandbox_policy(path) == {}


def test_load_returns_block_when_present(tmp_path: Path) -> None:
    policy = {
        "network": "pull",
        "network_allowlist": ["registry-1.docker.io"],
        "cap_add": ["SYS_PTRACE"],
        "no_new_privileges": True,
    }
    path = _write_template(tmp_path, sandbox_policy=policy)
    loaded = _load_recipe_template_sandbox_policy(path)
    assert loaded == policy


def test_load_ignores_non_mapping_block(tmp_path: Path) -> None:
    # If the recipe template's sandbox_policy is, e.g., a list, treat
    # it as absent rather than crashing.
    raw = {
        "migration_id": "x",
        "recipe": {"dockerfile": "FROM x", "build_cmd": "y", "test_cmd": "z"},
        "stamps": {
            "oracle_spec": "a",
            "recipe_spec": "b",
            "hypotheses": "c",
        },
        "sandbox_policy": ["network=none"],
    }
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert _load_recipe_template_sandbox_policy(path) == {}


# ---------------------------------------------------------------------------
# _merge_sandbox_policy
# ---------------------------------------------------------------------------


def test_merge_returns_none_when_both_empty() -> None:
    assert _merge_sandbox_policy({}, None) is None
    assert _merge_sandbox_policy({}, {}) is None


def test_merge_uses_recipe_when_smoke_absent() -> None:
    recipe = {"network": "pull", "network_allowlist": ["registry-1.docker.io"]}
    merged = _merge_sandbox_policy(recipe, None)
    assert merged == recipe


def test_merge_uses_smoke_when_recipe_absent() -> None:
    smoke = {"network": "none"}
    merged = _merge_sandbox_policy({}, smoke)
    assert merged == smoke


def test_merge_smoke_overrides_recipe_per_key() -> None:
    recipe = {
        "network": "pull",
        "network_allowlist": ["registry-1.docker.io"],
        "cap_add": ["SYS_PTRACE"],
    }
    smoke = {
        "network": "none",
        "network_allowlist": [],
    }
    merged = _merge_sandbox_policy(recipe, smoke)
    # smoke wins on network + network_allowlist; recipe's cap_add survives
    assert merged == {
        "network": "none",
        "network_allowlist": [],
        "cap_add": ["SYS_PTRACE"],
    }


def test_merge_recipe_only_keys_pass_through() -> None:
    recipe = {"user": "1001:1001", "scratch_dir": "/work-out"}
    smoke = {"network": "none"}
    merged = _merge_sandbox_policy(recipe, smoke)
    assert merged == {
        "user": "1001:1001",
        "scratch_dir": "/work-out",
        "network": "none",
    }


def test_merge_does_not_mutate_inputs() -> None:
    recipe = {"network": "pull", "network_allowlist": ["a"]}
    smoke = {"network": "none"}
    recipe_before = dict(recipe)
    smoke_before = dict(smoke)
    _merge_sandbox_policy(recipe, smoke)
    assert recipe == recipe_before
    assert smoke == smoke_before


# ---------------------------------------------------------------------------
# run_from_config: end-to-end sandbox_policy threading
# ---------------------------------------------------------------------------


def _minimal_smoke_config(
    tmp_path: Path,
    *,
    repo_path: Path,
    output_root: Path,
    recipe_path: Path,
    smoke_policy: dict | None = None,
) -> Path:
    cfg = {
        "migration_id": "fake_mig",
        "agent_model": "claude-sonnet-4-6",
        "agent_runner": "test",
        "variant": "smoke",
        "output_root": str(output_root),
        "stages": ["diff"],
        "repos": [{"path": str(repo_path), "seed": 1}],
        "adapters": {
            # keep cassette default so we don't touch docker
            "sandbox_provider": "cassette",
        },
        "stamps": {
            "oracle_spec": str(_REPO_ROOT / "configs" / "oracle_spec.yaml"),
            "recipe_spec": str(recipe_path),
            "hypotheses": str(
                _REPO_ROOT / "docs" / "hypotheses_and_thresholds.md"
            ),
        },
    }
    if smoke_policy is not None:
        cfg["adapters"]["sandbox_policy"] = smoke_policy
    cfg_path = tmp_path / "smoke.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return cfg_path


def test_run_from_config_threads_recipe_template_policy_to_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: when recipe template declares sandbox_policy and the
    smoke YAML doesn't, the runner must hand the merged policy to
    ``build_sandbox_adapter``.

    We capture adapters_cfg at the factory boundary rather than running
    the whole funnel — this isolates the merge contract from the
    cassette/docker provider behavior.
    """
    captured: dict = {}

    def fake_build_sandbox_adapter(*, repo_path, adapters_cfg, cassette_dir):
        captured["adapters_cfg"] = dict(adapters_cfg)
        # return a stub adapter that the funnel won't actually call —
        # we'll short-circuit by raising in fake_run_funnel below.
        return object()

    def fake_run_funnel(*args, **kwargs):
        raise _StopAfterAdapterBuild()

    class _StopAfterAdapterBuild(Exception):
        pass

    monkeypatch.setattr(
        "migration_evals.runner.build_sandbox_adapter",
        fake_build_sandbox_adapter,
    )
    monkeypatch.setattr(
        "migration_evals.runner.run_funnel", fake_run_funnel
    )

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "meta.json").write_text(
        json.dumps(
            {
                "task_id": "t1",
                "migration_id": "fake_mig",
                "agent_model": "claude-sonnet-4-6",
                "dockerfile": "FROM alpine\n",
                "build_cmd": "true",
                "test_cmd": "true",
                "is_synthetic": True,
            }
        ),
        encoding="utf-8",
    )

    recipe_path = _write_template(
        tmp_path,
        sandbox_policy={
            "network": "pull",
            "network_allowlist": ["registry-1.docker.io"],
        },
    )

    cfg_path = _minimal_smoke_config(
        tmp_path,
        repo_path=repo_path,
        output_root=tmp_path / "out",
        recipe_path=recipe_path,
    )

    with pytest.raises(_StopAfterAdapterBuild):
        run_from_config(cfg_path)

    assert "sandbox_policy" in captured["adapters_cfg"]
    assert captured["adapters_cfg"]["sandbox_policy"] == {
        "network": "pull",
        "network_allowlist": ["registry-1.docker.io"],
    }


def test_run_from_config_smoke_overrides_recipe_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both sources declare sandbox_policy, smoke YAML wins per-key."""
    captured: dict = {}

    def fake_build_sandbox_adapter(*, repo_path, adapters_cfg, cassette_dir):
        captured["adapters_cfg"] = dict(adapters_cfg)
        return object()

    class _Stop(Exception):
        pass

    monkeypatch.setattr(
        "migration_evals.runner.build_sandbox_adapter",
        fake_build_sandbox_adapter,
    )
    monkeypatch.setattr(
        "migration_evals.runner.run_funnel",
        lambda *a, **kw: (_ for _ in ()).throw(_Stop()),
    )

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "meta.json").write_text(
        json.dumps(
            {
                "task_id": "t1",
                "migration_id": "fake_mig",
                "agent_model": "claude-sonnet-4-6",
                "dockerfile": "FROM alpine\n",
                "build_cmd": "true",
                "test_cmd": "true",
                "is_synthetic": True,
            }
        ),
        encoding="utf-8",
    )

    recipe_path = _write_template(
        tmp_path,
        sandbox_policy={
            "network": "pull",
            "network_allowlist": ["registry-1.docker.io"],
            "user": "1001:1001",
        },
    )
    cfg_path = _minimal_smoke_config(
        tmp_path,
        repo_path=repo_path,
        output_root=tmp_path / "out",
        recipe_path=recipe_path,
        smoke_policy={"network": "none", "network_allowlist": []},
    )

    with pytest.raises(_Stop):
        run_from_config(cfg_path)

    merged = captured["adapters_cfg"]["sandbox_policy"]
    # smoke overrides network + network_allowlist
    assert merged["network"] == "none"
    assert merged["network_allowlist"] == []
    # recipe-only key (user) survives the merge
    assert merged["user"] == "1001:1001"


def test_run_from_config_no_policy_anywhere_leaves_adapters_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_build_sandbox_adapter(*, repo_path, adapters_cfg, cassette_dir):
        captured["adapters_cfg"] = dict(adapters_cfg)
        return object()

    class _Stop(Exception):
        pass

    monkeypatch.setattr(
        "migration_evals.runner.build_sandbox_adapter",
        fake_build_sandbox_adapter,
    )
    monkeypatch.setattr(
        "migration_evals.runner.run_funnel",
        lambda *a, **kw: (_ for _ in ()).throw(_Stop()),
    )

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "meta.json").write_text(
        json.dumps(
            {
                "task_id": "t1",
                "migration_id": "fake_mig",
                "agent_model": "claude-sonnet-4-6",
                "dockerfile": "FROM alpine\n",
                "build_cmd": "true",
                "test_cmd": "true",
                "is_synthetic": True,
            }
        ),
        encoding="utf-8",
    )
    recipe_path = _write_template(tmp_path)  # no sandbox_policy
    cfg_path = _minimal_smoke_config(
        tmp_path,
        repo_path=repo_path,
        output_root=tmp_path / "out",
        recipe_path=recipe_path,
    )

    with pytest.raises(_Stop):
        run_from_config(cfg_path)

    assert "sandbox_policy" not in captured["adapters_cfg"]
