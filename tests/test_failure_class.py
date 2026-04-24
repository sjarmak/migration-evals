"""Tests for migration_evals.failure_class (PRD M6).

Iterates every labeled case under
`tests/fixtures/failure_class_cases/`, invokes `classify()`,
and compares the result to the label in `expected_class.txt`. Asserts overall
precision >= 0.90 per AC #5.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from migration_evals.failure_class import classify
from migration_evals.types import FailureClass

CASES_DIR = (
    _REPO_ROOT / "tests" / "fixtures" / "failure_class_cases"
)

PRECISION_THRESHOLD = 0.90
MIN_CASES_TOTAL = 20
MIN_CASES_PER_CLASS = 5


def _iter_cases() -> list[tuple[Path, FailureClass]]:
    cases: list[tuple[Path, FailureClass]] = []
    for case_dir in sorted(p for p in CASES_DIR.iterdir() if p.is_dir()):
        label_path = case_dir / "expected_class.txt"
        assert label_path.is_file(), f"missing expected_class.txt for {case_dir}"
        label = label_path.read_text().strip()
        expected = FailureClass(label)
        cases.append((case_dir, expected))
    return cases


def test_fixture_coverage() -> None:
    cases = _iter_cases()
    assert len(cases) >= MIN_CASES_TOTAL, (
        f"need at least {MIN_CASES_TOTAL} labeled cases, got {len(cases)}"
    )
    per_class: dict[FailureClass, int] = {c: 0 for c in FailureClass}
    for _, expected in cases:
        per_class[expected] += 1
    for cls, count in per_class.items():
        assert count >= MIN_CASES_PER_CLASS, (
            f"need at least {MIN_CASES_PER_CLASS} cases for {cls.value}, got {count}"
        )


def test_classifier_precision_meets_threshold() -> None:
    cases = _iter_cases()
    correct = 0
    mismatches: list[str] = []
    for case_dir, expected in cases:
        actual = classify(case_dir)
        if actual == expected:
            correct += 1
        else:
            mismatches.append(
                f"  {case_dir.name}: expected={expected.value}, got={actual}"
            )
    precision = correct / len(cases)
    msg = f"precision={precision:.2%} ({correct}/{len(cases)}); mismatches:\n" + "\n".join(mismatches)
    assert precision >= PRECISION_THRESHOLD, msg


@pytest.mark.parametrize("case_dir,expected", _iter_cases(), ids=lambda v: str(v))
def test_each_case(case_dir: Path, expected: FailureClass) -> None:
    """Per-case smoke test — surface exactly which fixtures break."""
    actual = classify(case_dir)
    assert isinstance(actual, FailureClass), (
        f"classify() returned {actual!r} for {case_dir}"
    )
    # We assert each case matches its label; in aggregate the precision test
    # is the real gate, but per-case failures give clearer diagnostics.
    assert actual == expected, (
        f"{case_dir.name}: expected={expected.value}, got={actual.value}"
    )


def test_classify_returns_none_on_success(tmp_path: Path) -> None:
    """A success=True trial should return None (no failure class)."""
    import json

    trial = tmp_path / "success_trial"
    trial.mkdir()
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_id": "sample",
                "agent_model": "m",
                "migration_id": "x",
                "success": True,
                "failure_class": None,
                "oracle_tier": "compile_only",
                "oracle_spec_sha": "a",
                "recipe_spec_sha": "b",
                "pre_reg_sha": "c",
                "score_pre_cutoff": 1.0,
                "score_post_cutoff": 1.0,
            }
        )
    )
    assert classify(trial) is None


def test_classify_missing_result_defaults_to_agent_error(tmp_path: Path) -> None:
    """No result.json → no evidence of infra/harness/oracle → agent_error."""
    trial = tmp_path / "no_result"
    trial.mkdir()
    assert classify(trial) == FailureClass.AGENT_ERROR
