"""Gold-anchor loader and correlation analysis for migration eval (PRD M4-lite).

This module provides a frozen gold set (human accept/reject labels) and the
correlation analysis that compares the oracle funnel's pass/fail signal to
those human verdicts. The correlation coefficient is Phi (equivalent to
Pearson correlation on two binary variables).

Key outputs:

- ``load_gold_set(path)`` -> ``list[GoldEntry]`` — deserialise a frozen gold
  set from JSON.
- ``correlate(funnel_results, gold)`` -> ``CorrelationReport`` — compute Phi
  with a 95% bootstrap CI (n_bootstrap=10000, seeded) and an ``eval_broken``
  flag that trips when ``point < 0.7`` or ``ci_low < 0.5``.

The public surface uses stdlib only (no numpy dependency).
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

ACCEPT = "accept"
REJECT = "reject"
VALID_VERDICTS = frozenset({ACCEPT, REJECT})

# Eval-broken thresholds. Documented in docs/migration_eval/gold_anchor.md.
POINT_THRESHOLD = 0.7
CI_LOW_THRESHOLD = 0.5


@dataclass(frozen=True)
class GoldEntry:
    """A single human-adjudicated label for an oracle-funnel trial.

    Fields:
        repo_url: Canonical repo URL used as part of the join key.
        commit_sha: Commit SHA that was evaluated.
        human_verdict: ``"accept"`` or ``"reject"``.
        reviewer_notes: Free-form reviewer commentary.
        labeled_at: ISO-8601 timestamp string.
    """

    repo_url: str
    commit_sha: str
    human_verdict: str
    reviewer_notes: str
    labeled_at: str

    def __post_init__(self) -> None:
        if self.human_verdict not in VALID_VERDICTS:
            raise ValueError(
                f"GoldEntry.human_verdict must be one of {sorted(VALID_VERDICTS)}; "
                f"got {self.human_verdict!r}"
            )


@dataclass(frozen=True)
class CorrelationReport:
    """Bootstrap-estimated Phi correlation + eval_broken verdict.

    Fields:
        point: Phi coefficient (point estimate) on the full joined pair set.
        ci_low: Lower bound of the 95% bootstrap CI.
        ci_high: Upper bound of the 95% bootstrap CI.
        eval_broken: True iff point < 0.7 or ci_low < 0.5.
        details: Extra diagnostic info (n_pairs, dropped counts, etc.).
    """

    point: float
    ci_low: float
    ci_high: float
    eval_broken: bool
    details: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_gold_set(path: Path) -> list[GoldEntry]:
    """Load a gold-anchor JSON array from disk.

    The file must be a JSON array of objects with the GoldEntry fields. An
    empty array is valid (template state). Raises ``ValueError`` on a
    malformed file or invalid entries.
    """
    path = Path(path)
    with path.open() as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(
            f"gold set at {path} must be a JSON array; got {type(data).__name__}"
        )
    entries: list[GoldEntry] = []
    for idx, raw in enumerate(data):
        if not isinstance(raw, dict):
            raise ValueError(
                f"gold set entry #{idx} at {path} must be an object; "
                f"got {type(raw).__name__}"
            )
        try:
            entry = GoldEntry(
                repo_url=str(raw["repo_url"]),
                commit_sha=str(raw["commit_sha"]),
                human_verdict=str(raw["human_verdict"]),
                reviewer_notes=str(raw.get("reviewer_notes", "")),
                labeled_at=str(raw["labeled_at"]),
            )
        except KeyError as exc:
            raise ValueError(
                f"gold set entry #{idx} at {path} missing required field {exc!s}"
            ) from exc
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


def _phi(x: Sequence[int], y: Sequence[int]) -> float:
    """Phi coefficient for two parallel binary (0/1) sequences."""
    if len(x) != len(y):
        raise ValueError("_phi: x and y must be the same length")
    n11 = n10 = n01 = n00 = 0
    for a, b in zip(x, y):
        if a == 1 and b == 1:
            n11 += 1
        elif a == 1 and b == 0:
            n10 += 1
        elif a == 0 and b == 1:
            n01 += 1
        else:
            n00 += 1
    numerator = n11 * n00 - n10 * n01
    denom_parts = (n11 + n10, n01 + n00, n11 + n01, n10 + n00)
    if any(part == 0 for part in denom_parts):
        # Degenerate: one marginal is empty; Phi is undefined. Treat as 0.
        return 0.0
    denom = math.sqrt(
        denom_parts[0] * denom_parts[1] * denom_parts[2] * denom_parts[3]
    )
    return numerator / denom


def _percentile(sorted_values: Sequence[float], percent: float) -> float:
    """Linear-interpolation percentile on a pre-sorted list."""
    if not sorted_values:
        raise ValueError("_percentile: empty input")
    if percent <= 0:
        return sorted_values[0]
    if percent >= 100:
        return sorted_values[-1]
    n = len(sorted_values)
    rank = (percent / 100.0) * (n - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return sorted_values[lo]
    frac = rank - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


# ---------------------------------------------------------------------------
# Pair matching + correlation
# ---------------------------------------------------------------------------


def _funnel_pass(result: Mapping[str, Any]) -> int:
    """Convert a funnel result dict to a binary 0/1 score."""
    return 1 if bool(result.get("success")) else 0


def _gold_pass(entry: GoldEntry) -> int:
    return 1 if entry.human_verdict == ACCEPT else 0


def _join_pairs(
    funnel_results: Sequence[Mapping[str, Any]],
    gold: Sequence[GoldEntry],
) -> tuple[list[int], list[int], dict[str, int]]:
    """Inner-join funnel results and gold entries on (repo_url, commit_sha)."""
    gold_by_key: dict[tuple[str, str], GoldEntry] = {}
    for entry in gold:
        gold_by_key[(entry.repo_url, entry.commit_sha)] = entry

    funnel_x: list[int] = []
    gold_y: list[int] = []
    matched: set[tuple[str, str]] = set()
    dropped_funnel = 0
    for result in funnel_results:
        key = (
            str(result.get("repo_url", "")),
            str(result.get("commit_sha", "")),
        )
        entry = gold_by_key.get(key)
        if entry is None:
            dropped_funnel += 1
            continue
        funnel_x.append(_funnel_pass(result))
        gold_y.append(_gold_pass(entry))
        matched.add(key)

    dropped_gold = sum(1 for key in gold_by_key if key not in matched)
    details = {
        "n_pairs": len(funnel_x),
        "dropped_funnel": dropped_funnel,
        "dropped_gold": dropped_gold,
    }
    return funnel_x, gold_y, details


def correlate(
    funnel_results: Sequence[Mapping[str, Any]],
    gold: Sequence[GoldEntry],
    *,
    n_bootstrap: int = 10_000,
    seed: int = 42,
) -> CorrelationReport:
    """Compute Phi + 95% bootstrap CI for a funnel / gold pair set.

    ``eval_broken`` is True iff ``point < 0.7`` or ``ci_low < 0.5`` — either
    branch marks the oracle signal as insufficiently aligned with human
    verdicts to publish.
    """
    x, y, details = _join_pairs(funnel_results, gold)

    if not x:
        # No overlap — cannot compute Phi. Treat as broken so the gate trips.
        return CorrelationReport(
            point=0.0,
            ci_low=0.0,
            ci_high=0.0,
            eval_broken=True,
            details={**details, "reason": "no overlapping pairs"},
        )

    point = _phi(x, y)

    rng = random.Random(seed)
    n = len(x)
    boot_values: list[float] = []
    for _ in range(n_bootstrap):
        idxs = [rng.randrange(n) for _ in range(n)]
        bx = [x[i] for i in idxs]
        by = [y[i] for i in idxs]
        boot_values.append(_phi(bx, by))

    boot_values.sort()
    ci_low = _percentile(boot_values, 2.5)
    ci_high = _percentile(boot_values, 97.5)
    eval_broken = point < POINT_THRESHOLD or ci_low < CI_LOW_THRESHOLD

    return CorrelationReport(
        point=point,
        ci_low=ci_low,
        ci_high=ci_high,
        eval_broken=eval_broken,
        details={
            **details,
            "n_bootstrap": n_bootstrap,
            "seed": seed,
        },
    )


__all__ = [
    "ACCEPT",
    "REJECT",
    "POINT_THRESHOLD",
    "CI_LOW_THRESHOLD",
    "GoldEntry",
    "CorrelationReport",
    "load_gold_set",
    "correlate",
]
