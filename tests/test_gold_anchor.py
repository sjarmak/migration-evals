"""Tests for the migration-eval gold anchor loader and correlation analysis.

Covers the acceptance criteria for the "gold-anchor-and-correlation" work
unit:

- ``load_gold_set`` reads the empty template and rejects invalid verdicts.
- ``correlate`` computes Phi and a 95% bootstrap CI on known-answer fixtures
  (perfect correlation, anti-correlation, random noise).
- ``eval_broken`` trips on both the point-estimate branch and the
  lower-CI-bound branch, and stays False on a healthy input.
- The bootstrap CI is deterministic for a given seed.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from migration_evals.gold_anchor import (  # noqa: E402
    CorrelationReport,
    GoldEntry,
    correlate,
    load_gold_set,
)

REPO_ROOT = _REPO_ROOT
TEMPLATE_PATH = REPO_ROOT / "data" / "gold_anchor_template.json"


# ---------------------------------------------------------------------------
# load_gold_set
# ---------------------------------------------------------------------------


def test_load_gold_set_empty_template() -> None:
    entries = load_gold_set(TEMPLATE_PATH)
    assert entries == []


def test_load_gold_set_parses_valid_entries(tmp_path: Path) -> None:
    payload = [
        {
            "repo_url": "https://github.com/ex/a",
            "commit_sha": "a" * 40,
            "human_verdict": "accept",
            "reviewer_notes": "clean diff",
            "labeled_at": "2025-01-01T00:00:00Z",
        },
        {
            "repo_url": "https://github.com/ex/b",
            "commit_sha": "b" * 40,
            "human_verdict": "reject",
            "reviewer_notes": "regression in test",
            "labeled_at": "2025-01-02T00:00:00Z",
        },
    ]
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(payload))
    entries = load_gold_set(path)
    assert len(entries) == 2
    assert isinstance(entries[0], GoldEntry)
    assert entries[0].human_verdict == "accept"
    assert entries[1].human_verdict == "reject"


def test_load_gold_set_rejects_invalid_verdict(tmp_path: Path) -> None:
    payload = [
        {
            "repo_url": "https://github.com/ex/a",
            "commit_sha": "a" * 40,
            "human_verdict": "maybe",
            "reviewer_notes": "",
            "labeled_at": "2025-01-01T00:00:00Z",
        }
    ]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_gold_set(path)


def test_load_gold_set_rejects_non_array(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"not": "an array"}))
    with pytest.raises(ValueError):
        load_gold_set(path)


def test_load_gold_set_rejects_missing_field(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            [
                {
                    "repo_url": "https://github.com/ex/a",
                    "commit_sha": "a" * 40,
                    # "human_verdict" is missing
                    "reviewer_notes": "",
                    "labeled_at": "2025-01-01T00:00:00Z",
                }
            ]
        )
    )
    with pytest.raises(ValueError):
        load_gold_set(path)


# ---------------------------------------------------------------------------
# correlate: known-answer fixtures
# ---------------------------------------------------------------------------


def _pair(repo_id: int, funnel_success: bool, verdict: str) -> tuple[dict, GoldEntry]:
    repo_url = f"https://github.com/ex/repo{repo_id:03d}"
    commit_sha = f"{repo_id:040x}"
    result = {
        "repo_url": repo_url,
        "commit_sha": commit_sha,
        "success": funnel_success,
    }
    entry = GoldEntry(
        repo_url=repo_url,
        commit_sha=commit_sha,
        human_verdict=verdict,
        reviewer_notes="",
        labeled_at="2025-01-01T00:00:00Z",
    )
    return result, entry


def _build_pairs(
    pattern: list[tuple[bool, str]],
) -> tuple[list[dict], list[GoldEntry]]:
    results: list[dict] = []
    gold: list[GoldEntry] = []
    for i, (success, verdict) in enumerate(pattern):
        result, entry = _pair(i, success, verdict)
        results.append(result)
        gold.append(entry)
    return results, gold


def test_correlate_perfect_correlation_is_one() -> None:
    # 20 accept+pass, 20 reject+fail -> Phi = 1.0.
    pattern = [(True, "accept")] * 20 + [(False, "reject")] * 20
    results, gold = _build_pairs(pattern)
    report = correlate(results, gold)
    assert isinstance(report, CorrelationReport)
    assert report.point == pytest.approx(1.0, abs=1e-9)
    assert report.ci_low >= 0.99
    assert report.ci_high >= 0.99
    assert report.eval_broken is False


def test_correlate_anti_correlation_trips_point_branch() -> None:
    # 20 accept+fail, 20 reject+pass -> Phi = -1.0.
    pattern = [(False, "accept")] * 20 + [(True, "reject")] * 20
    results, gold = _build_pairs(pattern)
    report = correlate(results, gold)
    assert report.point == pytest.approx(-1.0, abs=1e-9)
    # point < 0.7 branch must fire.
    assert report.eval_broken is True


def test_correlate_random_trips_ci_low_branch() -> None:
    rng = random.Random(7)
    pattern: list[tuple[bool, str]] = []
    for _ in range(40):
        pattern.append(
            (bool(rng.getrandbits(1)), "accept" if rng.getrandbits(1) else "reject")
        )
    results, gold = _build_pairs(pattern)
    report = correlate(results, gold)
    # Random noise: point near zero, CI wide. ci_low < 0.5 must hold.
    assert abs(report.point) < 0.3
    assert report.ci_low < 0.5
    assert report.eval_broken is True


def test_correlate_healthy_not_broken() -> None:
    # 18 accept+pass, 2 accept+fail, 18 reject+fail, 2 reject+pass.
    # Phi > 0.7, CI tight.
    pattern = (
        [(True, "accept")] * 18
        + [(False, "accept")] * 2
        + [(False, "reject")] * 18
        + [(True, "reject")] * 2
    )
    results, gold = _build_pairs(pattern)
    report = correlate(results, gold)
    assert report.point >= 0.7
    assert report.ci_low >= 0.5
    assert report.eval_broken is False


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_correlate_bootstrap_is_deterministic() -> None:
    pattern = (
        [(True, "accept")] * 15
        + [(False, "accept")] * 5
        + [(False, "reject")] * 15
        + [(True, "reject")] * 5
    )
    results, gold = _build_pairs(pattern)
    first = correlate(results, gold, seed=42)
    second = correlate(results, gold, seed=42)
    assert first.point == second.point
    assert first.ci_low == second.ci_low
    assert first.ci_high == second.ci_high
    assert first.eval_broken == second.eval_broken


def test_correlate_bootstrap_different_seed_differs() -> None:
    pattern = (
        [(True, "accept")] * 15
        + [(False, "accept")] * 5
        + [(False, "reject")] * 15
        + [(True, "reject")] * 5
    )
    results, gold = _build_pairs(pattern)
    a = correlate(results, gold, seed=1)
    b = correlate(results, gold, seed=2)
    # Same point estimate (not bootstrapped), but CI bounds should differ
    # at least a little across distinct seeds.
    assert a.point == b.point
    assert (a.ci_low, a.ci_high) != (b.ci_low, b.ci_high)


# ---------------------------------------------------------------------------
# Joining / dropped counts
# ---------------------------------------------------------------------------


def test_correlate_drops_unmatched_entries() -> None:
    # Two matched pairs + one funnel-only + one gold-only.
    results = [
        {"repo_url": "r1", "commit_sha": "c1", "success": True},
        {"repo_url": "r2", "commit_sha": "c2", "success": False},
        {"repo_url": "rX", "commit_sha": "cX", "success": True},
    ]
    gold = [
        GoldEntry("r1", "c1", "accept", "", "2025-01-01T00:00:00Z"),
        GoldEntry("r2", "c2", "reject", "", "2025-01-01T00:00:00Z"),
        GoldEntry("rY", "cY", "accept", "", "2025-01-01T00:00:00Z"),
    ]
    report = correlate(results, gold)
    assert report.details["n_pairs"] == 2
    assert report.details["dropped_funnel"] == 1
    assert report.details["dropped_gold"] == 1


def test_correlate_no_overlap_is_broken() -> None:
    results = [{"repo_url": "r1", "commit_sha": "c1", "success": True}]
    gold = [GoldEntry("rZ", "cZ", "accept", "", "2025-01-01T00:00:00Z")]
    report = correlate(results, gold)
    assert report.eval_broken is True
    assert report.details["n_pairs"] == 0
