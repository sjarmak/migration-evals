"""Tests for the Python 2→3 falsification probe (PRD M9).

Acceptance criteria covered:

- (a) generator produces ≥3 distinct case-type repos
- (b) probe runs to completion against fixture repos
- (c) schema_revision_required True branch (real probe run)
- (d) schema_revision_required False branch (synthetic injection of <2
      modules with mismatches)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import pytest

from migration_evals import python23_probe
from migration_evals.synthetic import python2_generator

_FIXTURE_REPOS = (
    Path(__file__).resolve().parent / "fixtures" / "python2_repos"
)


def test_generator_emits_three_distinct_case_types(tmp_path: Path) -> None:
    """Acceptance (a): generator covers ≥3 distinct Python case types."""
    out = tmp_path / "py2gen"
    repos = python2_generator.generate(out, count=12, seed=42)
    assert len(repos) == 12

    observed: set[str] = set()
    for repo_dir in repos:
        meta = json.loads((repo_dir / "python2_meta.json").read_text())
        observed.add(meta["case_type"])

    assert {"str_bytes", "setup_py_div", "two_to_three"}.issubset(observed), (
        f"expected all three case types, got {observed}"
    )
    # Each emitted repo carries a setup.py (no pyproject.toml — that absence
    # is part of the falsification surface for the harness module).
    for repo_dir in repos:
        assert (repo_dir / "setup.py").is_file()
        assert not (repo_dir / "pyproject.toml").exists()


def test_probe_runs_to_completion_against_fixtures(tmp_path: Path) -> None:
    """Acceptance (b): probe completes against the bundled fixture repos."""
    out_dir = tmp_path / "probe_out"
    findings = python23_probe.run(
        out_dir=out_dir,
        fixture_repo_root=_FIXTURE_REPOS,
    )

    findings_path = out_dir / "findings.json"
    assert findings_path.is_file(), "probe must write findings.json"

    on_disk = json.loads(findings_path.read_text())
    assert on_disk == findings, "in-memory findings must equal on-disk JSON"

    expected_keys = {
        "schema_revision_required",
        "n_repos",
        "primitive_coverage",
        "mismatches_by_module",
        "modules_with_mismatches",
        "intent",
    }
    assert expected_keys.issubset(findings.keys())

    # Fixture repo count is fixed (5 repos under tests/.../python2_repos).
    fixture_count = sum(1 for p in _FIXTURE_REPOS.iterdir() if p.is_dir())
    assert findings["n_repos"] == fixture_count


def test_schema_revision_required_true_branch_real_probe_run(
    tmp_path: Path,
) -> None:
    """Acceptance (c): real probe run against fixtures flips the gate to True.

    This is the EXPECTED v1 finding — see docs/migration_eval/python23_probe.md.
    All three of harness/synthetic/ledger naturally trip on the Java-shaped
    interfaces, so ``schema_revision_required`` is True.
    """
    out_dir = tmp_path / "probe_true_branch"
    findings = python23_probe.run(
        out_dir=out_dir,
        fixture_repo_root=_FIXTURE_REPOS,
    )
    assert findings["schema_revision_required"] is True

    modules_with_mismatches = set(findings["modules_with_mismatches"])
    assert len(modules_with_mismatches) >= 2, (
        f"expected ≥2 modules with mismatches; got {modules_with_mismatches}"
    )

    # Specifically: synthetic and ledger must trip naturally on a Python repo
    # set against the Java-shaped schemas. (Harness also trips because Recipe
    # has no ecosystem discriminator.)
    assert "synthetic" in modules_with_mismatches
    assert "ledger" in modules_with_mismatches


def test_schema_revision_required_false_branch_synthetic_injection() -> None:
    """Acceptance (d): threshold helper returns False when only 1 module trips.

    This validates the threshold logic, not real probe behavior — we inject
    a synthetic findings dict with mismatches in exactly one module.
    """
    one_module_mismatch = {
        "harness": [
            {
                "module": "harness",
                "issue": "missing_ecosystem_discriminator",
                "field": "Recipe.ecosystem",
                "reason": "synthetic test injection",
            }
        ],
        "synthetic": [],
        "ledger": [],
    }
    assert (
        python23_probe.compute_schema_revision_required(one_module_mismatch)
        is False
    )

    zero_modules_mismatch = {"harness": [], "synthetic": [], "ledger": []}
    assert (
        python23_probe.compute_schema_revision_required(zero_modules_mismatch)
        is False
    )

    # Sanity: two distinct modules → True.
    two_modules_mismatch = {
        "harness": [
            {
                "module": "harness",
                "issue": "x",
                "field": "y",
                "reason": "z",
            }
        ],
        "synthetic": [
            {
                "module": "synthetic",
                "issue": "x",
                "field": "y",
                "reason": "z",
            }
        ],
        "ledger": [],
    }
    assert (
        python23_probe.compute_schema_revision_required(two_modules_mismatch)
        is True
    )


def test_probe_findings_capture_python_runtime_tier_in_ledger_mismatch(
    tmp_path: Path,
) -> None:
    """Cross-check: ledger mismatch entry references the python tier explicitly."""
    findings = python23_probe.run(
        out_dir=tmp_path / "probe_ledger",
        fixture_repo_root=_FIXTURE_REPOS,
    )
    ledger_entries = findings["mismatches_by_module"]["ledger"]
    assert ledger_entries, "ledger mismatch list should be non-empty"
    assert any(
        python23_probe.PYTHON_2TO3_RUNTIME_TIER in entry.get("reason", "")
        for entry in ledger_entries
    )


def test_cli_probe_python23_writes_findings_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: invoking the CLI subcommand writes findings.json + exit 0."""
    from migration_evals import cli

    out_dir = tmp_path / "cli_probe"
    rc = cli.main(
        [
            "probe",
            "--ecosystem",
            "python23",
            "--fixture-repo-root",
            str(_FIXTURE_REPOS),
            "--out",
            str(out_dir),
        ]
    )
    assert rc == 0
    assert (out_dir / "findings.json").is_file()
