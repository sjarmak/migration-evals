"""End-to-end smoke for scripts/run_eval.py.

The driver wires three pieces together:

    1. ChangesetProvider -> /tmp/eval/<id>/{repo, repo/patch.diff, meta.json}
    2. <eval-root>/<id>/repo/meta.json synthesized from the recipe template
       + the changeset provenance
    3. runner.run_from_config() over a transient YAML config

This test exercises only the Tier-0 (``diff_valid``) stage so the smoke
runs offline without Docker or a sandbox cassette. It seeds a local
file:// remote, stages two changesets (one valid patch, one broken),
runs the driver, and asserts the result.json files land with the
correct success / failure_class.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# `seeded_remote` comes from tests/conftest.py.

_REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = _REPO_ROOT / "scripts" / "run_eval.py"
RECIPE_PATH = _REPO_ROOT / "configs" / "recipes" / "java8_17.yaml"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_eval", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_eval"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def re_mod():
    return _load_module()


def _valid_patch() -> str:
    return (
        "diff --git a/foo.txt b/foo.txt\n"
        "--- a/foo.txt\n"
        "+++ b/foo.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+world\n"
    )


def _broken_patch() -> str:
    return (
        "diff --git a/foo.txt b/foo.txt\n"
        "--- a/foo.txt\n"
        "+++ b/foo.txt\n"
        "@@ -1 +1 @@\n"
        "-not-the-actual-content\n"
        "+world\n"
    )


def _stage(staged: Path, instance_id: str, *, repo_url: str, sha: str, patch: str) -> None:
    d = staged / instance_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "repo_url": repo_url,
                "commit_sha": sha,
                "workflow_id": f"wf-{instance_id}",
                "agent_runner": "claude_code",
                "agent_model": "claude-sonnet-4-6",
            }
        )
    )
    (d / "patch.diff").write_text(patch)


# -- load_recipe_template --------------------------------------------------


def test_load_recipe_template_reads_yaml(re_mod) -> None:
    tmpl = re_mod.load_recipe_template(RECIPE_PATH)
    assert tmpl["migration_id"] == "java8_17"
    assert "dockerfile" in tmpl["recipe"]
    assert tmpl["recipe"]["build_cmd"].startswith("mvn")
    assert "oracle_spec" in tmpl["stamps"]


# -- synthesize_repo_meta --------------------------------------------------


def test_synthesize_repo_meta_merges_template_and_provenance(re_mod, tmp_path: Path) -> None:
    inst_root = tmp_path / "inst-1"
    repo_dir = inst_root / "repo"
    repo_dir.mkdir(parents=True)
    (inst_root / "meta.json").write_text(
        json.dumps(
            {
                "repo_url": "https://github.com/example/foo",
                "commit_sha": "a" * 40,
                "workflow_id": "wf-1",
                "agent_runner": "claude_code",
                "agent_model": "claude-sonnet-4-6",
            }
        )
    )
    tmpl = re_mod.load_recipe_template(RECIPE_PATH)

    re_mod.synthesize_repo_meta(inst_root, tmpl)

    repo_meta = json.loads((repo_dir / "meta.json").read_text())
    # Recipe fields come from the template.
    assert repo_meta["build_cmd"].startswith("mvn")
    assert repo_meta["test_cmd"].startswith("mvn")
    assert repo_meta["dockerfile"].startswith("FROM maven")
    # Provenance + identity come from the changeset.
    assert repo_meta["migration_id"] == "java8_17"
    assert repo_meta["agent_model"] == "claude-sonnet-4-6"
    assert repo_meta["task_id"] == "inst-1"


# -- driver E2E (Tier-0 only, no sandbox) ---------------------------------


def test_main_runs_funnel_tier0_and_writes_result_jsons(
    re_mod, tmp_path: Path, seeded_remote
) -> None:
    url, sha = seeded_remote
    staged = tmp_path / "staged"
    _stage(staged, "good", repo_url=url, sha=sha, patch=_valid_patch())
    _stage(staged, "bad", repo_url=url, sha=sha, patch=_broken_patch())
    eval_root = tmp_path / "eval"
    out_root = tmp_path / "out"

    rc = re_mod.main(
        [
            "--migration", "java8_17",
            "--provider", "filesystem",
            "--root", str(staged),
            "--eval-root", str(eval_root),
            "--output-root", str(out_root),
            "--variant", "smoke",
            "--stages", "diff",
            "good", "bad",
        ]
    )
    assert rc == 0

    # One result.json per instance under output_root/<repo_name>_<seed>/
    written = sorted(out_root.glob("*/result.json"))
    assert len(written) == 2

    payloads = {p.parent.name: json.loads(p.read_text()) for p in written}
    # Tier-0 passes for the valid patch, fails (agent_error) for the broken one.
    good_key = next(k for k in payloads if k.startswith("good_"))
    bad_key = next(k for k in payloads if k.startswith("bad_"))
    assert payloads[good_key]["success"] is True
    assert payloads[bad_key]["success"] is False
    assert payloads[bad_key]["failure_class"] == "agent_error"
    # Both carry the migration_id and agent_model from the synthesized meta.
    for p in payloads.values():
        assert p["migration_id"] == "java8_17"
        assert p["agent_model"] == "claude-sonnet-4-6"


def test_main_skips_pull_failures_and_returns_partial_exit(
    re_mod, tmp_path: Path, seeded_remote
) -> None:
    """A pull-failure instance does not stop the run; exit code reflects
    that at least one instance failed before the funnel even saw it."""
    url, sha = seeded_remote
    staged = tmp_path / "staged"
    _stage(staged, "good", repo_url=url, sha=sha, patch=_valid_patch())
    # "missing" is not in the staged dir -> pull fails.
    eval_root = tmp_path / "eval"
    out_root = tmp_path / "out"

    rc = re_mod.main(
        [
            "--migration", "java8_17",
            "--provider", "filesystem",
            "--root", str(staged),
            "--eval-root", str(eval_root),
            "--output-root", str(out_root),
            "--variant", "smoke",
            "--stages", "diff",
            "good", "missing",
        ]
    )
    # Pull fail counts as a non-zero exit; result.json still emitted for "good".
    assert rc == 2
    assert (list(out_root.glob("good_*/result.json"))), "good instance must still produce result.json"


def test_main_unknown_migration_returns_exit_1(re_mod, tmp_path: Path) -> None:
    rc = re_mod.main(
        [
            "--migration", "bogus_migration",
            "--provider", "filesystem",
            "--root", str(tmp_path),
            "--eval-root", str(tmp_path / "eval"),
            "--output-root", str(tmp_path / "out"),
            "--variant", "smoke",
            "good",
        ]
    )
    assert rc == 1
