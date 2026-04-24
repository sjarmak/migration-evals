"""Unit tests for the Tier-0 diff-validity oracle.

Covers all three check paths:
  - patch artifact (well-formed + malformed unified diff)
  - orig/ vs migrated/ subtree (synthetic-fixture shape)
  - repo-only structural fallback

The tier returns an OracleVerdict with tier='diff_valid' regardless of
which check path fires. The check path is recorded in
``verdict.details['check']``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from migration_evals.harness.recipe import Recipe
from migration_evals.oracles import tier0_diff


def _stub_recipe() -> Recipe:
    return Recipe(
        dockerfile="FROM scratch\n",
        build_cmd="echo build",
        test_cmd="echo test",
        harness_provenance={
            "model": "test-stub",
            "prompt_version": "v0",
            "timestamp": "2026-04-24T00:00:00Z",
        },
    )


# -- patch artifact path ---------------------------------------------------


VALID_PATCH = """\
--- a/src/Foo.java
+++ b/src/Foo.java
@@ -1,3 +1,4 @@
 class Foo {
-    void bar() {}
+    void bar() { return; }
+    void baz() {}
 }
"""


MALFORMED_PATCH = """\
--- a/src/Foo.java
+++ b/src/Foo.java
@@ -1,2 +1,99 @@
 class Foo {
-    void bar() {}
"""


def test_tier0_passes_on_well_formed_patch_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "patch.diff").write_text(VALID_PATCH)
    verdict = tier0_diff.run(repo, _stub_recipe(), daytona_adapter=None)
    assert verdict.tier == "diff_valid"
    assert verdict.passed is True
    assert verdict.details["check"] == "patch_artifact"
    assert verdict.details["n_files"] == 1
    assert verdict.details["n_hunks"] == 1


def test_tier0_fails_on_malformed_patch_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "patch.diff").write_text(MALFORMED_PATCH)
    verdict = tier0_diff.run(repo, _stub_recipe(), daytona_adapter=None)
    assert verdict.tier == "diff_valid"
    assert verdict.passed is False
    assert verdict.details["check"] == "patch_artifact"
    # Reason can be either the generic outer "patch_malformed" or the
    # specific underlying parser failure (e.g., "hunk_line_count_mismatch").
    assert verdict.details["reason"] in {
        "patch_malformed",
        "hunk_line_count_mismatch",
        "no_diff_content",
    }


def test_tier0_recognises_alternative_patch_filenames(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent_diff.patch").write_text(VALID_PATCH)
    verdict = tier0_diff.run(repo, _stub_recipe(), daytona_adapter=None)
    assert verdict.passed is True
    assert verdict.details["check"] == "patch_artifact"


# -- orig/ vs migrated/ path -----------------------------------------------


def _make_synthetic_pair(root: Path, *, valid: bool) -> Path:
    repo = root / "repo"
    (repo / "orig" / "src" / "main" / "java" / "com").mkdir(parents=True)
    (repo / "migrated" / "src" / "main" / "java" / "com").mkdir(parents=True)
    (repo / "orig" / "src" / "main" / "java" / "com" / "Foo.java").write_text(
        "class Foo { void bar() {} }\n"
    )
    migrated_content = (
        "class Foo { void bar() { return; } void baz() {} }\n"
        if valid
        else "class Foo { void bar( ) { unbalanced \n"  # missing brace + paren
    )
    (repo / "migrated" / "src" / "main" / "java" / "com" / "Foo.java").write_text(
        migrated_content
    )
    return repo


def test_tier0_passes_on_well_formed_synthetic_pair(tmp_path: Path) -> None:
    repo = _make_synthetic_pair(tmp_path, valid=True)
    verdict = tier0_diff.run(repo, _stub_recipe(), daytona_adapter=None)
    assert verdict.passed is True
    assert verdict.details["check"] == "orig_vs_migrated"
    assert verdict.details["n_files_checked"] == 1
    assert verdict.details["failures"] == []


def test_tier0_fails_when_migrated_file_brace_imbalanced(tmp_path: Path) -> None:
    repo = _make_synthetic_pair(tmp_path, valid=False)
    verdict = tier0_diff.run(repo, _stub_recipe(), daytona_adapter=None)
    assert verdict.passed is False
    assert verdict.details["check"] == "orig_vs_migrated"
    assert verdict.details["reason"] == "migrated_file_invalid"
    assert any("brace" in failure or "paren" in failure for failure in verdict.details["failures"])


def test_tier0_fails_when_migrated_subtree_empty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "orig").mkdir(parents=True)
    (repo / "orig" / "Foo.java").write_text("class Foo {}\n")
    (repo / "migrated").mkdir()
    verdict = tier0_diff.run(repo, _stub_recipe(), daytona_adapter=None)
    assert verdict.passed is False
    assert verdict.details["reason"] == "migrated_subtree_empty"


# -- repo-only structural fallback -----------------------------------------


def test_tier0_passes_when_no_source_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi")
    verdict = tier0_diff.run(repo, _stub_recipe(), daytona_adapter=None)
    assert verdict.passed is True
    assert verdict.details["check"] == "repo_structural"
    assert verdict.details["reason"] == "no_source_files_to_check"


def test_tier0_passes_on_balanced_source_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Foo.java").write_text("class Foo { void bar() { return; } }\n")
    (repo / "lib.py").write_text("def f(x): return x\n")
    verdict = tier0_diff.run(repo, _stub_recipe(), daytona_adapter=None)
    assert verdict.passed is True
    assert verdict.details["check"] == "repo_structural"


def test_tier0_fails_on_unbalanced_braces_in_source_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Foo.java").write_text("class Foo { void bar( { return; }\n")
    verdict = tier0_diff.run(repo, _stub_recipe(), daytona_adapter=None)
    assert verdict.passed is False
    assert verdict.details["check"] == "repo_structural"
    assert verdict.details["reason"] == "source_file_invalid"


# -- funnel integration ----------------------------------------------------


def test_funnel_runs_tier0_first(tmp_path: Path) -> None:
    """The cascade should short-circuit at Tier-0 on a malformed patch."""
    from migration_evals.funnel import run_funnel

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "patch.diff").write_text(MALFORMED_PATCH)

    class _NeverCalledDaytona:
        def create_sandbox(self, **kwargs):
            raise AssertionError("Tier-0 failure should short-circuit before Tier-1")

    result = run_funnel(
        repo,
        _stub_recipe(),
        adapters={"daytona": _NeverCalledDaytona(), "anthropic": None, "enable_daikon": False},
        is_synthetic=False,
    )
    assert result.final_verdict.tier == "diff_valid"
    assert result.final_verdict.passed is False
    assert result.failure_class == "agent_error"
    # No subsequent tiers ran.
    assert [name for name, _ in result.per_tier_verdict] == ["diff_valid"]


def test_funnel_skips_tier0_via_stage_filter(tmp_path: Path) -> None:
    """`--stage compile` must skip Tier-0 entirely."""
    from migration_evals.funnel import run_funnel

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "patch.diff").write_text(MALFORMED_PATCH)

    class _PassingDaytona:
        def create_sandbox(self, **kwargs):
            return "sb-1"

        def exec(self, sandbox_id, **kwargs):
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        def destroy_sandbox(self, sandbox_id):
            return None

    result = run_funnel(
        repo,
        _stub_recipe(),
        adapters={"daytona": _PassingDaytona(), "anthropic": None, "enable_daikon": False},
        is_synthetic=False,
        stages=("compile_only",),
    )
    # Tier-0 was filtered out so the malformed patch never blocked us.
    assert [name for name, _ in result.per_tier_verdict] == ["compile_only"]
    assert result.final_verdict.passed is True
