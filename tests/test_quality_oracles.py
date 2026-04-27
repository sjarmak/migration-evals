"""Tests for the batch-change quality oracles (dsm).

Covers:

- diff_minimality: ratio + overlap + over-edit math, threshold breaches,
  the no-ground-truth skip path.
- idempotency: clean idempotent post-state, drift in additions, drift in
  removals, target-file-missing.
- baseline_comparison: sed pattern with zero substitutions on the
  post-state (agent and baseline agree), unsupported tools (skipped),
  no-pattern path.
- touched_paths: warn-mode reports violations informationally,
  enforce-mode flips passed=False on out-of-glob touches; recursive `**`
  semantics; the literal `/dev/null` token is dropped from
  ``touched_paths`` but the deletion's source path IS recorded so the
  allowlist can gate deletions as well as edits/creates.
- run_quality_oracles: runs all four in fixed order.
- run_funnel: emits quality_verdicts when adapters supplies a
  quality_spec, leaves the field empty otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from migration_evals.harness.recipe import Recipe  # noqa: E402
from migration_evals.oracles.quality import (  # noqa: E402
    baseline_comparison,
    diff_minimality,
    idempotency,
    run_quality_oracles,
    touched_paths,
)
from migration_evals.quality_spec import (  # noqa: E402
    BaselinePattern,
    QualitySpec,
)


# ---------------------------------------------------------------------------
# diff_minimality
# ---------------------------------------------------------------------------


def _write(repo: Path, name: str, content: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _agent_diff_one_file() -> str:
    return dedent(
        """\
        --- a/main.go
        +++ b/main.go
        @@ -3,5 +3,5 @@
         import (
         \t"fmt"

        -\t"github.com/foo/oldpkg"
        +\t"github.com/foo/newpkg"
         )
        """
    )


def _ground_truth_one_file_same() -> str:
    """Ground truth that matches the agent's diff exactly."""
    return _agent_diff_one_file()


def _agent_diff_with_extra_file() -> str:
    """Agent edits main.go AND extras.go - over-edit beyond ground truth."""
    return dedent(
        """\
        --- a/main.go
        +++ b/main.go
        @@ -3,5 +3,5 @@
        -\t"github.com/foo/oldpkg"
        +\t"github.com/foo/newpkg"
        --- a/extras.go
        +++ b/extras.go
        @@ -1,1 +1,1 @@
        -package extras
        +package extra2
        """
    )


def test_diff_minimality_clean_pass(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_one_file())
    gt_path = tmp_path / "ground_truth.diff"
    gt_path.write_text(_ground_truth_one_file_same())

    spec = QualitySpec(ground_truth_diff=gt_path)
    verdict = diff_minimality.run(repo, spec)
    assert verdict.passed is True
    assert verdict.details["diff_size_ratio"] == 1.0
    assert verdict.details["touched_files_overlap"] == 1.0
    assert verdict.details["over_edit_pct"] == 0.0


def test_diff_minimality_skipped_when_no_ground_truth(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_one_file())
    verdict = diff_minimality.run(repo, QualitySpec.empty())
    assert verdict.passed is True
    assert verdict.details["skipped"] is True


def test_diff_minimality_breach_over_edit(tmp_path: Path) -> None:
    """Agent touched a file ground truth didn't - over_edit_pct breaches."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_with_extra_file())
    gt_path = tmp_path / "ground_truth.diff"
    gt_path.write_text(_ground_truth_one_file_same())

    spec = QualitySpec(ground_truth_diff=gt_path)
    verdict = diff_minimality.run(repo, spec)
    # agent_files = {main.go, extras.go}; gt_files = {main.go}
    # over_edit_pct = 1/2 = 0.5 > 0.25 -> breach
    assert verdict.passed is False
    assert verdict.details["over_edit_pct"] == 0.5
    assert any(
        "over_edit_pct" in b for b in verdict.details["breaches"]
    )


def test_diff_minimality_breach_diff_size_ratio(tmp_path: Path) -> None:
    """Agent diff is much larger than ground truth -> ratio breach."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Agent adds 6 lines; gt adds/removes 2 lines total -> ratio=6.0.
    _write(
        repo,
        "patch.diff",
        dedent(
            """\
            --- a/main.go
            +++ b/main.go
            @@ -1,1 +1,7 @@
             package main
            +import "fmt"
            +import "os"
            +import "io"
            +import "log"
            +import "time"
            +import "errors"
            """
        ),
    )
    gt_path = tmp_path / "ground_truth.diff"
    gt_path.write_text(
        dedent(
            """\
            --- a/main.go
            +++ b/main.go
            @@ -1,1 +1,2 @@
             package main
            +import "fmt"
            """
        )
    )
    verdict = diff_minimality.run(repo, QualitySpec(ground_truth_diff=gt_path))
    assert verdict.passed is False
    assert verdict.details["diff_size_ratio"] is not None
    assert verdict.details["diff_size_ratio"] > 2.0
    assert any(
        "diff_size_ratio" in b for b in verdict.details["breaches"]
    )


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_idempotency_clean_post_state(tmp_path: Path) -> None:
    """Re-applying the patch to the post-state is a no-op."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Post-state already has the new line; old line is gone.
    _write(
        repo,
        "main.go",
        dedent(
            """\
            package main

            import (
            \t"fmt"

            \t"github.com/foo/newpkg"
            )
            """
        ),
    )
    _write(repo, "patch.diff", _agent_diff_one_file())
    verdict = idempotency.run(repo, QualitySpec.empty())
    assert verdict.passed is True
    assert verdict.details["idempotent"] is True
    assert verdict.details["files_with_drift"] == 0


def test_idempotency_drift_when_minus_line_still_present(
    tmp_path: Path,
) -> None:
    """The post-state still contains the line the patch claims to remove."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo,
        "main.go",
        dedent(
            """\
            package main

            import (
            \t"fmt"

            \t"github.com/foo/oldpkg"
            \t"github.com/foo/newpkg"
            )
            """
        ),
    )
    _write(repo, "patch.diff", _agent_diff_one_file())
    verdict = idempotency.run(repo, QualitySpec.empty())
    assert verdict.passed is False
    assert verdict.details["idempotent"] is False
    assert any(
        "still present" in d for d in verdict.details["drift_examples"]
    )


def test_idempotency_drift_when_plus_line_missing(tmp_path: Path) -> None:
    """The post-state is missing a line the patch claims to add."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo,
        "main.go",
        dedent(
            """\
            package main

            import (
            \t"fmt"
            )
            """
        ),
    )
    _write(repo, "patch.diff", _agent_diff_one_file())
    verdict = idempotency.run(repo, QualitySpec.empty())
    assert verdict.passed is False
    assert verdict.details["idempotent"] is False
    assert any(
        "missing expected" in d for d in verdict.details["drift_examples"]
    )


def test_idempotency_skipped_when_no_patch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    verdict = idempotency.run(repo, QualitySpec.empty())
    assert verdict.passed is True
    assert verdict.details["skipped"] is True


# ---------------------------------------------------------------------------
# baseline_comparison
# ---------------------------------------------------------------------------


def test_baseline_skipped_when_no_tool(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_one_file())
    verdict = baseline_comparison.run(repo, QualitySpec.empty())
    assert verdict.passed is True
    assert verdict.details["skipped"] is True


def test_baseline_skipped_when_unsupported_tool(tmp_path: Path) -> None:
    """``comby`` and ``gopls`` are accepted by QualitySpec but skipped here."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_one_file())
    spec = QualitySpec(baseline_tool="comby")
    verdict = baseline_comparison.run(repo, spec)
    assert verdict.passed is True
    assert verdict.details["skipped"] is True
    assert "comby" in verdict.details["reason"]


def test_baseline_sed_agrees_with_agent(tmp_path: Path) -> None:
    """A sed pattern that produces zero substitutions on the post-state
    means the post-state already reflects the migration - agent and sed
    baseline both arrived at the same place."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Post-state already has new path; sed run on it would change nothing.
    _write(
        repo,
        "main.go",
        'package main\nimport "github.com/foo/newpkg"\n',
    )
    _write(repo, "patch.diff", _agent_diff_one_file())
    spec = QualitySpec(
        baseline_tool="sed",
        baseline_pattern=BaselinePattern(
            match=r"github\.com/foo/oldpkg",
            replace="github.com/foo/newpkg",
            files="*.go",
        ),
    )
    verdict = baseline_comparison.run(repo, spec)
    assert verdict.passed is True
    assert verdict.details["baseline_passed"] is True
    assert verdict.details["agent_lift"] == 0.0
    # The patch claims to touch main.go, which matches *.go.
    assert verdict.details["n_files"] == 1


def test_baseline_sed_disagrees_when_pattern_still_matches(
    tmp_path: Path,
) -> None:
    """sed finds the old path on the post-state file - the agent's diff
    didn't fully complete the migration that the recipe author's regex
    describes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo,
        "main.go",
        # Post-state still has the OLD path -> sed substitutes -> baseline
        # disagrees with the post-state and the agent did not, in fact,
        # match the baseline.
        'package main\nimport "github.com/foo/oldpkg"\n',
    )
    _write(repo, "patch.diff", _agent_diff_one_file())
    spec = QualitySpec(
        baseline_tool="sed",
        baseline_pattern=BaselinePattern(
            match=r"github\.com/foo/oldpkg",
            replace="github.com/foo/newpkg",
            files="*.go",
        ),
    )
    verdict = baseline_comparison.run(repo, spec)
    assert verdict.passed is True  # informational tier
    assert verdict.details["baseline_passed"] is False
    assert verdict.details["agent_lift"] == 1.0


# ---------------------------------------------------------------------------
# touched_paths
# ---------------------------------------------------------------------------


def _agent_diff_with_md_file() -> str:
    """Agent edits main.go AND a docs/README.md outside a Go-only allowlist."""
    return dedent(
        """\
        --- a/main.go
        +++ b/main.go
        @@ -3,5 +3,5 @@
        -\t"github.com/foo/oldpkg"
        +\t"github.com/foo/newpkg"
        --- a/docs/README.md
        +++ b/docs/README.md
        @@ -1,1 +1,1 @@
        -old docs
        +new docs
        """
    )


def _agent_diff_nested_go() -> str:
    """Agent touches a deeply-nested Go file (`internal/pkg/foo.go`)."""
    return dedent(
        """\
        --- a/internal/pkg/foo.go
        +++ b/internal/pkg/foo.go
        @@ -1,1 +1,1 @@
        -package foo
        +package foo2
        """
    )


def _agent_diff_with_deletion() -> str:
    """Agent deletes ``legacy.go`` (in glob) and ``legacy.txt`` (out of glob).

    Both deletions render as ``+++ /dev/null``; the source paths come
    from the ``--- a/`` lines. The allowlist oracle should record both
    source paths but never the literal ``/dev/null`` token.
    """
    return dedent(
        """\
        --- a/legacy.go
        +++ /dev/null
        @@ -1,1 +0,0 @@
        -package legacy
        --- a/legacy.txt
        +++ /dev/null
        @@ -1,1 +0,0 @@
        -stale notes
        --- a/main.go
        +++ b/main.go
        @@ -1,1 +1,1 @@
        -old
        +new
        """
    )


def test_touched_paths_skipped_when_no_allowlist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_one_file())
    verdict = touched_paths.run(repo, QualitySpec.empty())
    assert verdict.passed is True
    assert verdict.details["skipped"] is True


def test_touched_paths_skipped_when_no_patch_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    spec = QualitySpec(touched_paths_allowlist=("**/*.go",))
    verdict = touched_paths.run(repo, spec)
    assert verdict.passed is True
    assert verdict.details["skipped"] is True


def test_touched_paths_warn_mode_passes_with_violations_listed(
    tmp_path: Path,
) -> None:
    """Warn mode (default): violations land in details but passed stays True."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_with_md_file())
    spec = QualitySpec(
        touched_paths_allowlist=("**/*.go", "*.go"),
        touched_paths_allowlist_mode="warn",
    )
    verdict = touched_paths.run(repo, spec)
    assert verdict.passed is True
    assert verdict.details["mode"] == "warn"
    assert "docs/README.md" in verdict.details["violations"]
    assert "main.go" not in verdict.details["violations"]


def test_touched_paths_enforce_mode_fails_on_out_of_glob(
    tmp_path: Path,
) -> None:
    """Enforce mode flips passed=False when any touched path is out of glob."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_with_md_file())
    spec = QualitySpec(
        touched_paths_allowlist=("**/*.go", "*.go"),
        touched_paths_allowlist_mode="enforce",
    )
    verdict = touched_paths.run(repo, spec)
    assert verdict.passed is False
    assert verdict.details["mode"] == "enforce"
    assert "docs/README.md" in verdict.details["violations"]


def test_touched_paths_enforce_passes_when_all_in_glob(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_one_file())
    spec = QualitySpec(
        touched_paths_allowlist=("**/*.go", "*.go"),
        touched_paths_allowlist_mode="enforce",
    )
    verdict = touched_paths.run(repo, spec)
    assert verdict.passed is True
    assert verdict.details["violations"] == []


def test_touched_paths_recursive_glob_matches_nested(tmp_path: Path) -> None:
    """`**/*.go` matches `internal/pkg/foo.go`.

    fnmatch's translation of `**/*.go` requires at least one path
    separator before the `.go` suffix, so a top-level `main.go` would
    NOT match `**/*.go` alone — recipe authors who want both should
    union with `*.go`. The test below uses ONLY `**/*.go` to prove the
    recursive case is wired correctly without piggybacking on `*.go`.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_nested_go())
    spec = QualitySpec(
        touched_paths_allowlist=("**/*.go",),
        touched_paths_allowlist_mode="enforce",
    )
    verdict = touched_paths.run(repo, spec)
    assert verdict.passed is True
    assert verdict.details["violations"] == []
    assert "internal/pkg/foo.go" in verdict.details["touched_paths"]


def test_touched_paths_top_level_glob_matches_root(tmp_path: Path) -> None:
    """`*.go` matches a root-level `main.go` even though `**/*.go` requires
    at least one path separator. Documents the fnmatch semantics so recipe
    authors can pick the right glob."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_one_file())  # touches `main.go`
    spec = QualitySpec(
        touched_paths_allowlist=("*.go",),
        touched_paths_allowlist_mode="enforce",
    )
    verdict = touched_paths.run(repo, spec)
    assert verdict.passed is True
    assert verdict.details["violations"] == []
    assert "main.go" in verdict.details["touched_paths"]


def test_touched_paths_multiple_globs_union(tmp_path: Path) -> None:
    """Both globs admit the diff: `main.go` via `*.go`, `docs/README.md`
    via `docs/**`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_with_md_file())
    spec = QualitySpec(
        touched_paths_allowlist=("*.go", "docs/**"),
        touched_paths_allowlist_mode="enforce",
    )
    verdict = touched_paths.run(repo, spec)
    assert verdict.passed is True
    assert verdict.details["violations"] == []


def test_touched_paths_drops_literal_dev_null_token(tmp_path: Path) -> None:
    """The literal ``/dev/null`` token must never appear in
    ``touched_paths`` — it is the unified-diff sentinel for "this side
    has no file", not a real path. The deletion's source path (from the
    ``--- a/...`` line) IS recorded so the allowlist can gate deletions."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_with_deletion())
    spec = QualitySpec(
        touched_paths_allowlist=("**/*.go", "*.go"),
        touched_paths_allowlist_mode="warn",
    )
    verdict = touched_paths.run(repo, spec)
    assert "/dev/null" not in verdict.details["touched_paths"]
    # Both deletion sources are recorded.
    assert "legacy.go" in verdict.details["touched_paths"]
    assert "legacy.txt" in verdict.details["touched_paths"]
    assert "main.go" in verdict.details["touched_paths"]


def test_touched_paths_enforce_gates_deletions(tmp_path: Path) -> None:
    """Deleting a file outside the allowlist is itself a violation.

    The agent removed ``legacy.txt`` (out of glob) AND ``legacy.go`` (in
    glob); enforce mode must flag legacy.txt as a violation while
    leaving legacy.go alone. This pins the design that deletions DO
    contribute their source path to the allowlist check, distinguishing
    a correct implementation from one that simply skips any line
    referencing ``/dev/null``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_with_deletion())
    spec = QualitySpec(
        touched_paths_allowlist=("**/*.go", "*.go"),
        touched_paths_allowlist_mode="enforce",
    )
    verdict = touched_paths.run(repo, spec)
    assert verdict.passed is False
    assert "legacy.txt" in verdict.details["violations"]
    assert "legacy.go" not in verdict.details["violations"]
    assert "main.go" not in verdict.details["violations"]


def test_quality_spec_rejects_invalid_mode() -> None:
    """The `touched_paths_allowlist_mode` field validates against
    ('warn', 'enforce')."""
    with pytest.raises(ValueError, match="touched_paths_allowlist_mode"):
        QualitySpec(touched_paths_allowlist_mode="strict")


def test_touched_paths_records_both_sides_of_rename(tmp_path: Path) -> None:
    """A rename has different `--- a/` and `+++ b/` paths. Both sides
    contribute to ``touched_paths`` so an allowlist that admits one
    side but not the other surfaces the violation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    rename_diff = dedent(
        """\
        --- a/old_pkg/foo.go
        +++ b/new_pkg/foo.go
        @@ -1,1 +1,1 @@
        -package old_pkg
        +package new_pkg
        """
    )
    _write(repo, "patch.diff", rename_diff)
    spec = QualitySpec(
        touched_paths_allowlist=("new_pkg/**",),
        touched_paths_allowlist_mode="enforce",
    )
    verdict = touched_paths.run(repo, spec)
    assert verdict.passed is False
    assert "old_pkg/foo.go" in verdict.details["violations"]
    assert "new_pkg/foo.go" not in verdict.details["violations"]


def test_touched_paths_handles_path_with_space(tmp_path: Path) -> None:
    """`\\S+` would truncate `path with space.go` at the first space.
    Using `[^\\t\\r\\n]+` preserves the full path so the allowlist can
    decide on the real value."""
    repo = tmp_path / "repo"
    repo.mkdir()
    diff = (
        "--- a/path with space.go\n"
        "+++ b/path with space.go\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    _write(repo, "patch.diff", diff)
    spec = QualitySpec(
        touched_paths_allowlist=("**/*.go", "*.go"),
        touched_paths_allowlist_mode="enforce",
    )
    verdict = touched_paths.run(repo, spec)
    assert "path with space.go" in verdict.details["touched_paths"]
    assert verdict.passed is True
    assert verdict.details["violations"] == []


def test_touched_paths_strips_git_metadata_after_tab(tmp_path: Path) -> None:
    """Git diffs may carry a `\\t<timestamp>` suffix on file headers; it
    must not become part of the recorded path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    diff = (
        "--- a/main.go\t2026-04-26 12:00:00\n"
        "+++ b/main.go\t2026-04-26 12:00:01\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    _write(repo, "patch.diff", diff)
    spec = QualitySpec(touched_paths_allowlist=("*.go",))
    verdict = touched_paths.run(repo, spec)
    assert verdict.details["touched_paths"] == ["main.go"]


def test_touched_paths_skipped_when_diff_exceeds_size_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile multi-megabyte diff must not OOM the host."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_one_file())
    monkeypatch.setattr(touched_paths, "MAX_DIFF_BYTES", 1)
    spec = QualitySpec(touched_paths_allowlist=("*.go",))
    verdict = touched_paths.run(repo, spec)
    assert verdict.passed is True
    assert verdict.details["skipped"] is True
    assert "MAX_DIFF_BYTES" in verdict.details["reason"]


# ---------------------------------------------------------------------------
# run_quality_oracles + funnel integration
# ---------------------------------------------------------------------------


def test_run_quality_oracles_returns_all_four_in_order(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_one_file())
    results = run_quality_oracles(repo, QualitySpec.empty())
    names = [name for name, _ in results]
    assert names == [
        "diff_minimality",
        "idempotency",
        "baseline_comparison",
        "touched_paths",
    ]


def test_run_funnel_attaches_quality_verdicts(tmp_path: Path) -> None:
    """When adapters carries a quality_spec, FunnelResult.quality_verdicts
    is populated and serialises to result.json."""
    from migration_evals.funnel import run_funnel

    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_one_file())
    recipe = Recipe(
        dockerfile="FROM scratch", build_cmd="true", test_cmd="true",
        harness_provenance={
            "model": "test", "prompt_version": "v1",
            "timestamp": "2026-04-26",
        },
    )
    fr = run_funnel(
        repo, recipe,
        adapters={"quality_spec": QualitySpec.empty()},
        is_synthetic=False,
        stages=("diff_valid",),
    )
    names = [n for n, _ in fr.quality_verdicts]
    assert names == [
        "diff_minimality",
        "idempotency",
        "baseline_comparison",
        "touched_paths",
    ]
    assert "quality_verdicts" in fr.to_dict()
    assert len(fr.to_dict()["quality_verdicts"]) == 4


def test_calibration_corpus_exercises_quality_oracles() -> None:
    """Acceptance check for dsm: the m1w calibration corpus exercises every
    quality oracle.

    Walking each fixture's repo/ through ``run_quality_oracles`` with the
    canonical go_import_rewrite quality_spec produces three verdicts per
    fixture, and at least one fixture must surface non-skip details for
    each oracle (so they are observably wired to real inputs, not just
    silent no-ops).
    """
    fixtures = (
        _REPO_ROOT
        / "tests" / "fixtures" / "calibration" / "go_import_rewrite"
    )
    spec = QualitySpec(
        ground_truth_diff=(
            _REPO_ROOT
            / "configs" / "recipes"
            / "go_import_rewrite.ground_truth.diff"
        ),
        touched_paths_allowlist=("**/*.go",),
        baseline_tool="sed",
        baseline_pattern=BaselinePattern(
            match=r"github\.com/foo/oldpkg",
            replace="github.com/foo/newpkg",
            files="*.go",
        ),
    )
    n_seen = {
        "diff_minimality": 0,
        "idempotency": 0,
        "baseline_comparison": 0,
        "touched_paths": 0,
    }
    for sub in ("known_good", "known_bad"):
        for fixture_dir in (fixtures / sub).iterdir():
            repo = fixture_dir / "repo"
            verdicts = run_quality_oracles(repo, spec)
            assert {n for n, _ in verdicts} == {
                "diff_minimality",
                "idempotency",
                "baseline_comparison",
                "touched_paths",
            }
            for name, v in verdicts:
                if not v.details.get("skipped"):
                    n_seen[name] += 1
    # Each oracle must have a non-skip observation on at least one fixture.
    assert all(n_seen[name] > 0 for name in n_seen), n_seen


def test_run_funnel_omits_quality_when_no_spec(tmp_path: Path) -> None:
    from migration_evals.funnel import run_funnel

    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "patch.diff", _agent_diff_one_file())
    recipe = Recipe(
        dockerfile="FROM scratch", build_cmd="true", test_cmd="true",
        harness_provenance={
            "model": "test", "prompt_version": "v1",
            "timestamp": "2026-04-26",
        },
    )
    fr = run_funnel(
        repo, recipe, adapters={},
        is_synthetic=False, stages=("diff_valid",),
    )
    assert fr.quality_verdicts == ()
    assert fr.to_dict()["quality_verdicts"] == []
