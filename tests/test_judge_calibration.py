"""Tests for Cohen's kappa pairwise judge calibration (bead migration_evals-cns).

The calibration step measures inter-judge agreement on a hand-labelled
overlap slice (~20 trials with human verdicts) and emits Cohen's kappa
across pairs of {claude, other_family, human}. Any pair with kappa
below 0.6 is flagged as unreliable — that threshold is the conventional
floor for "substantial agreement" in the inter-rater reliability
literature.

The kappa implementation here is binary (PASS/FAIL only) because every
judge tier emits a boolean verdict. Multi-class kappa is out of scope.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from migration_evals.judge_calibration import (  # noqa: E402
    KAPPA_FLOOR,
    JudgeAgreement,
    cohen_kappa_binary,
    pairwise_kappa,
    summarise_calibration,
)

# ---------------------------------------------------------------------------
# Cohen's kappa primitive
# ---------------------------------------------------------------------------


def test_cohen_kappa_perfect_agreement_is_one() -> None:
    rater1 = [True, True, False, False, True]
    rater2 = [True, True, False, False, True]
    assert cohen_kappa_binary(rater1, rater2) == pytest.approx(1.0)


def test_cohen_kappa_total_disagreement_is_negative() -> None:
    """Inverse responses → kappa ≤ −1 (perfect anti-agreement)."""
    rater1 = [True, True, False, False]
    rater2 = [False, False, True, True]
    k = cohen_kappa_binary(rater1, rater2)
    assert k < 0.0
    assert k == pytest.approx(-1.0)


def test_cohen_kappa_chance_level_near_zero() -> None:
    """Independent random raters → kappa ≈ 0 within sample noise."""
    # Both raters PASS half the time, but uncorrelated (4 of 8 agree).
    rater1 = [True, True, True, True, False, False, False, False]
    rater2 = [True, True, False, False, True, True, False, False]
    # Observed agreement = 4/8 = 0.5; expected by chance = 0.5*0.5 + 0.5*0.5 = 0.5.
    # kappa = (0.5 - 0.5) / (1 - 0.5) = 0
    assert cohen_kappa_binary(rater1, rater2) == pytest.approx(0.0)


def test_cohen_kappa_constant_rater_returns_nan() -> None:
    """If both raters always PASS, expected agreement is 1 and kappa is
    undefined. Return NaN so callers can flag it without crashing."""
    rater1 = [True, True, True, True]
    rater2 = [True, True, True, True]
    import math

    assert math.isnan(cohen_kappa_binary(rater1, rater2))


def test_cohen_kappa_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="length"):
        cohen_kappa_binary([True, False], [True])


def test_cohen_kappa_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        cohen_kappa_binary([], [])


# ---------------------------------------------------------------------------
# Pairwise across 3 raters
# ---------------------------------------------------------------------------


def _sample_trials() -> list[dict]:
    """20-trial labelled slice. Anthropic agrees with human 18/20; openai
    agrees 16/20. Anthropic vs openai diverges on 4 trials."""
    return (
        [{"trial_id": f"t{i}", "human": True, "anthropic": True, "other": True} for i in range(10)]
        + [
            {"trial_id": f"t{i}", "human": False, "anthropic": False, "other": False}
            for i in range(10, 18)
        ]
        + [
            # Anthropic agrees with human, openai dissents.
            {"trial_id": "t18", "human": True, "anthropic": True, "other": False},
            # Both judges miss the human.
            {"trial_id": "t19", "human": False, "anthropic": True, "other": True},
        ]
    )


def test_pairwise_kappa_returns_three_pairs() -> None:
    """Three raters → C(3,2) = 3 pairs (anthropic-other, anthropic-human,
    other-human)."""
    trials = _sample_trials()
    result = pairwise_kappa(trials)
    pairs = {(r.rater1, r.rater2) for r in result}
    assert pairs == {
        ("anthropic", "other"),
        ("anthropic", "human"),
        ("other", "human"),
    }


def test_pairwise_kappa_each_entry_has_kappa_and_flag() -> None:
    trials = _sample_trials()
    result = pairwise_kappa(trials)
    for entry in result:
        assert isinstance(entry, JudgeAgreement)
        assert isinstance(entry.kappa, float)
        assert isinstance(entry.unreliable, bool)
        assert entry.n == len(trials)


def test_pairwise_kappa_flags_pairs_below_floor() -> None:
    """Force kappa < 0.6 by making one judge nearly random."""
    trials = [
        {"trial_id": f"t{i}", "human": i % 2 == 0, "anthropic": i % 2 == 0, "other": True}
        for i in range(10)
    ]
    result = {(r.rater1, r.rater2): r for r in pairwise_kappa(trials)}
    # other vs human: other is constant True; kappa undefined → flagged.
    other_human = result[("other", "human")]
    assert other_human.unreliable, "constant rater pair should be flagged"


def test_kappa_floor_is_0_6() -> None:
    """The floor matches the bead spec (0.6 = 'substantial agreement')."""
    assert KAPPA_FLOOR == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# Calibration summary (CLI-facing)
# ---------------------------------------------------------------------------


def test_summarise_calibration_returns_table_and_unreliable_list() -> None:
    trials = _sample_trials()
    summary = summarise_calibration(trials)
    assert "n_trials" in summary
    assert summary["n_trials"] == 20
    assert isinstance(summary["pairs"], list)
    assert len(summary["pairs"]) == 3
    assert "unreliable_pairs" in summary
    # Each pair entry is a JSON-friendly dict.
    for pair in summary["pairs"]:
        assert {"rater1", "rater2", "kappa", "unreliable"} <= pair.keys()


def test_summarise_calibration_handles_missing_human_label() -> None:
    """Trials missing a human label are skipped, not crashed on."""
    trials = [
        {"trial_id": "t0", "human": True, "anthropic": True, "other": True},
        {"trial_id": "t1", "anthropic": True, "other": True},  # no human
        {"trial_id": "t2", "human": False, "anthropic": False, "other": False},
    ]
    summary = summarise_calibration(trials)
    # Only 2 trials had all three labels.
    assert summary["n_trials"] == 2


def test_summarise_calibration_loads_from_json_file(tmp_path: Path) -> None:
    """The CLI reads a JSON file; verify the file path entry point works."""
    from migration_evals.judge_calibration import load_trials

    f = tmp_path / "labels.json"
    f.write_text(json.dumps(_sample_trials()))
    trials = load_trials(f)
    assert len(trials) == 20


# ---------------------------------------------------------------------------
# CLI integration (scripts/judge_calibrate.py)
# ---------------------------------------------------------------------------


def test_judge_calibrate_cli_writes_summary(tmp_path: Path) -> None:
    """End-to-end: the CLI loads labels and writes a JSON summary."""
    import subprocess

    labels_path = tmp_path / "labels.json"
    labels_path.write_text(json.dumps(_sample_trials()))
    output_path = tmp_path / "summary.json"
    script_path = _REPO_ROOT / "scripts" / "judge_calibrate.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--labels",
            str(labels_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(output_path.read_text())
    assert summary["n_trials"] == 20
    assert len(summary["pairs"]) == 3


def test_judge_calibrate_cli_missing_labels_file_exits_1(tmp_path: Path) -> None:
    import subprocess

    script_path = _REPO_ROOT / "scripts" / "judge_calibrate.py"
    completed = subprocess.run(
        [sys.executable, str(script_path), "--labels", str(tmp_path / "missing.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "labels file not found" in completed.stderr
