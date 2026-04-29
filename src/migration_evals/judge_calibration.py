"""Pairwise Cohen's kappa calibration for cross-family judge agreement.

Bead migration_evals-cns: on a hand-labelled overlap slice (~20 trials)
this module measures pairwise agreement between {anthropic, other,
human} verdicts and flags any pair whose kappa falls below the
"substantial agreement" floor (0.6 by convention; see Landis & Koch
1977 — but interpret as a floor, not a target). A flagged pair means
the two raters disagree more than chance-corrected agreement allows;
publication of dual-judge results should not proceed until the cause
is investigated and rerun.

Why kappa, not raw accuracy
----------------------------
Two judges that always say PASS will agree 100% of the time. Raw
accuracy hides that they are both useless. Cohen's kappa
chance-corrects: agreement is only credited above what two random
raters with the same marginal class rates would produce. A constant
rater therefore produces undefined kappa (denominator zero); the
implementation returns NaN so the calibration step explicitly flags
constant-rater pairs as unreliable rather than crashing.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "KAPPA_FLOOR",
    "JudgeAgreement",
    "cohen_kappa_binary",
    "load_trials",
    "pairwise_kappa",
    "summarise_calibration",
]


# Conventional "substantial agreement" floor (Landis & Koch 1977 bands:
# 0.41–0.60 moderate, 0.61–0.80 substantial, 0.81–1.00 almost perfect).
# Bead spec calls 0.6 the unreliable cutoff, which sits just inside the
# moderate band.
KAPPA_FLOOR: float = 0.6


@dataclass(frozen=True)
class JudgeAgreement:
    """Pairwise inter-rater agreement for one (rater1, rater2) pair.

    ``kappa`` may be NaN when one or both raters are constant on the
    sample (no chance-corrected agreement is computable). ``unreliable``
    is True for NaN OR for any kappa strictly below
    :data:`KAPPA_FLOOR` so the publication gate has one boolean to
    check.
    """

    rater1: str
    rater2: str
    n: int
    kappa: float
    unreliable: bool


def cohen_kappa_binary(rater1: Sequence[bool], rater2: Sequence[bool]) -> float:
    """Compute Cohen's kappa for two boolean rater sequences.

    Returns NaN when expected agreement equals 1 (one or both raters
    constant on the sample) — kappa is undefined in that case.
    """
    if len(rater1) != len(rater2):
        raise ValueError(
            f"length mismatch: rater1 has {len(rater1)} samples, " f"rater2 has {len(rater2)}"
        )
    n = len(rater1)
    if n == 0:
        raise ValueError("cohen_kappa_binary requires at least one sample (got empty)")

    n_agree = sum(1 for a, b in zip(rater1, rater2) if bool(a) == bool(b))
    p_o = n_agree / n
    p1_pass = sum(1 for x in rater1 if bool(x)) / n
    p2_pass = sum(1 for x in rater2 if bool(x)) / n
    p_e = p1_pass * p2_pass + (1 - p1_pass) * (1 - p2_pass)
    if p_e >= 1.0 - 1e-12:
        return float("nan")
    return (p_o - p_e) / (1.0 - p_e)


def _trials_with_field(
    trials: Iterable[Mapping[str, Any]], fields: Sequence[str]
) -> list[Mapping[str, Any]]:
    """Return trials that have *every* listed field present (not None)."""
    out: list[Mapping[str, Any]] = []
    for t in trials:
        if all(t.get(f) is not None for f in fields):
            out.append(t)
    return out


def pairwise_kappa(trials: Iterable[Mapping[str, Any]]) -> list[JudgeAgreement]:
    """Compute kappa for every pair across {anthropic, other, human}.

    Each trial dict must carry boolean keys ``anthropic``, ``other``,
    and (optionally) ``human``. Trials missing a label for any rater in
    a given pair are skipped from that pair only.
    """
    materialised = list(trials)
    pairs = (
        ("anthropic", "other"),
        ("anthropic", "human"),
        ("other", "human"),
    )
    results: list[JudgeAgreement] = []
    for r1, r2 in pairs:
        slice_ = _trials_with_field(materialised, [r1, r2])
        if not slice_:
            results.append(
                JudgeAgreement(rater1=r1, rater2=r2, n=0, kappa=float("nan"), unreliable=True)
            )
            continue
        v1 = [bool(t[r1]) for t in slice_]
        v2 = [bool(t[r2]) for t in slice_]
        kappa = cohen_kappa_binary(v1, v2)
        unreliable = math.isnan(kappa) or kappa < KAPPA_FLOOR
        results.append(
            JudgeAgreement(rater1=r1, rater2=r2, n=len(slice_), kappa=kappa, unreliable=unreliable)
        )
    return results


def summarise_calibration(trials: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return a JSON-friendly summary table for CLI / report consumption.

    The ``n_trials`` field reports the count of trials that carry all
    three rater labels; per-pair counts are inside each ``pairs`` entry
    so a partial-coverage slice still produces a usable kappa where
    possible.
    """
    materialised = list(trials)
    full = _trials_with_field(materialised, ["anthropic", "other", "human"])
    agreements = pairwise_kappa(materialised)
    return {
        "n_trials": len(full),
        "kappa_floor": KAPPA_FLOOR,
        "pairs": [
            {
                "rater1": a.rater1,
                "rater2": a.rater2,
                "n": a.n,
                "kappa": _kappa_for_json(a.kappa),
                "unreliable": a.unreliable,
            }
            for a in agreements
        ],
        "unreliable_pairs": [f"{a.rater1}-{a.rater2}" for a in agreements if a.unreliable],
    }


def _kappa_for_json(value: float) -> float | None:
    """JSON has no NaN — encode as null for downstream tooling."""
    return None if math.isnan(value) else round(value, 4)


def load_trials(path: Path) -> list[Mapping[str, Any]]:
    """Read a JSON list of trial labels from disk.

    Accepts either a JSON array of trial dicts or a JSON object with a
    top-level ``trials`` key — both forms are produced by the
    calibration starter docs.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [dict(t) for t in raw if isinstance(t, Mapping)]
    if isinstance(raw, Mapping):
        trials = raw.get("trials")
        if isinstance(trials, list):
            return [dict(t) for t in trials if isinstance(t, Mapping)]
    raise ValueError(f"{path}: expected JSON array of trials or object with 'trials' key")
