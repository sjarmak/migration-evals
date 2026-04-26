"""Per-recipe oracle calibration (m1w).

Funnel pass-rates are uncalibrated until we can answer two questions per
tier:

- **FPR** (false-positive rate): on a known-good diff, how often does this
  tier wrongly reject?
- **FNR** (false-negative rate): on a known-bad diff that should be rejected
  at this tier, how often does the tier let it through?

This module ships the data model + computation. The corpus
(``tests/fixtures/calibration/<migration_id>/{known_good,known_bad}/``) and
the driver (``scripts/calibrate.py``) wire this into the funnel; the
publication gate consumes the resulting ``calibration.json`` and refuses
headline runs whose calibration violates the thresholds declared in
``docs/hypotheses_and_thresholds.md``.

The shape mirrors the SWE-bench Verified / Defects4J pattern: a vetted seed
corpus orthogonal to whatever an agent produces.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

SCHEMA_VERSION = "v1"
PASS_ALL = "pass_all"
REJECT = "reject"
ALLOWED_OUTCOMES = (PASS_ALL, REJECT)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureLabel:
    """Hand-authored expectation for a calibration fixture.

    ``expected_outcome`` is ``"pass_all"`` for known-good fixtures and
    ``"reject"`` for known-bad fixtures. When the outcome is ``"reject"``,
    ``expected_reject_tier`` names the tier that is expected to do the
    rejecting (so a fixture that exists to exercise tier-1 catches a
    different miscalibration from one that targets tier-0).
    """

    fixture_id: str
    expected_outcome: str
    expected_reject_tier: Optional[str] = None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.expected_outcome not in ALLOWED_OUTCOMES:
            raise ValueError(
                f"expected_outcome must be one of {ALLOWED_OUTCOMES}; "
                f"got {self.expected_outcome!r}"
            )
        if self.expected_outcome == REJECT and not self.expected_reject_tier:
            raise ValueError(
                "fixtures with expected_outcome='reject' must declare "
                "expected_reject_tier"
            )
        if self.expected_outcome == PASS_ALL and self.expected_reject_tier:
            raise ValueError(
                "fixtures with expected_outcome='pass_all' must not "
                "declare expected_reject_tier"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FixtureLabel":
        return cls(
            fixture_id=str(data["fixture_id"]),
            expected_outcome=str(data["expected_outcome"]),
            expected_reject_tier=(
                str(data["expected_reject_tier"])
                if data.get("expected_reject_tier")
                else None
            ),
            notes=str(data.get("notes", "")),
        )

    @classmethod
    def from_path(cls, path: Path) -> "FixtureLabel":
        return cls.from_dict(json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class FixtureObservation:
    """One fixture's hand-label paired with the tier verdicts it produced.

    ``tier_passed`` is an ordered mapping of ``tier_name -> passed`` for the
    tiers that actually ran. A tier that never ran (because an earlier tier
    short-circuited the cascade, or because the driver was invoked with a
    restricted ``--stages``) is intentionally absent so it does not count
    against any rate.
    """

    label: FixtureLabel
    tier_passed: Mapping[str, bool]


@dataclass(frozen=True)
class TierCalibration:
    """Per-tier confusion-matrix counts and derived rates.

    ``fpr`` is computed against known-good fixtures that reached this tier
    (``fp + tn``). ``fnr`` is computed against known-bad fixtures whose
    ``expected_reject_tier`` equals this tier and which actually reached it
    (``fn + tp``).

    ``fpr`` / ``fnr`` are ``None`` when their denominator is zero, which is
    visible in the JSON output and therefore visible to the publication
    gate (``None`` cannot satisfy a numeric threshold).
    """

    tier: str
    tp: int
    fp: int
    tn: int
    fn: int
    n_known_good_observed: int
    n_known_bad_targeted_observed: int
    fpr: Optional[float]
    fnr: Optional[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "tp": self.tp,
            "fp": self.fp,
            "tn": self.tn,
            "fn": self.fn,
            "n_known_good_observed": self.n_known_good_observed,
            "n_known_bad_targeted_observed": (
                self.n_known_bad_targeted_observed
            ),
            "fpr": self.fpr,
            "fnr": self.fnr,
        }


@dataclass(frozen=True)
class CalibrationReport:
    """Per-recipe calibration artifact written to ``calibration.json``.

    ``per_tier`` is ordered to match ``tier_order`` so a downstream reader
    sees the tiers in funnel order.
    """

    migration_id: str
    schema_version: str
    n_known_good: int
    n_known_bad: int
    per_tier: tuple[TierCalibration, ...]
    notes: str = ""

    def tier(self, name: str) -> TierCalibration:
        for entry in self.per_tier:
            if entry.tier == name:
                return entry
        raise KeyError(f"no calibration entry for tier {name!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "migration_id": self.migration_id,
            "n_known_good": self.n_known_good,
            "n_known_bad": self.n_known_bad,
            "per_tier": [t.to_dict() for t in self.per_tier],
            "notes": self.notes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CalibrationReport":
        per_tier = tuple(
            TierCalibration(
                tier=str(t["tier"]),
                tp=int(t["tp"]),
                fp=int(t["fp"]),
                tn=int(t["tn"]),
                fn=int(t["fn"]),
                n_known_good_observed=int(t["n_known_good_observed"]),
                n_known_bad_targeted_observed=int(
                    t["n_known_bad_targeted_observed"]
                ),
                fpr=(
                    None if t.get("fpr") is None else float(t["fpr"])
                ),
                fnr=(
                    None if t.get("fnr") is None else float(t["fnr"])
                ),
            )
            for t in data.get("per_tier", [])
        )
        return cls(
            migration_id=str(data["migration_id"]),
            schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
            n_known_good=int(data["n_known_good"]),
            n_known_bad=int(data["n_known_bad"]),
            per_tier=per_tier,
            notes=str(data.get("notes", "")),
        )

    @classmethod
    def from_path(cls, path: Path) -> "CalibrationReport":
        return cls.from_dict(json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class CalibrationThresholds:
    """Per-tier max-FPR / max-FNR thresholds parsed from the docs.

    A tier with no entry in ``per_tier`` has no enforced threshold (the
    publication gate will accept any value for that tier). A tier with
    ``max_fpr`` or ``max_fnr`` set requires the calibration's actual rate
    to be ``<=`` the threshold; ``None`` rates fail because no observations
    were collected for that tier.
    """

    per_tier: Mapping[str, Mapping[str, float]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def _safe_rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return numerator / denominator


def compute_calibration(
    observations: Sequence[FixtureObservation],
    *,
    migration_id: str,
    tier_order: Sequence[str],
    notes: str = "",
) -> CalibrationReport:
    """Aggregate per-fixture verdicts into per-tier FPR / FNR.

    Conventions
    -----------
    For each tier ``T`` in ``tier_order``:

    - A *known-good* fixture (``expected_outcome=='pass_all'``) that reached
      ``T``:

        - tier passed -> TN (clean diff, tier let it through)
        - tier failed -> FP (clean diff, tier wrongly rejected)

    - A *known-bad* fixture whose ``expected_reject_tier == T`` and which
      reached ``T``:

        - tier passed -> FN (broken diff, tier missed it)
        - tier failed -> TP

    - A *known-bad* fixture whose ``expected_reject_tier != T`` does not
      contribute to ``T``'s confusion matrix (it is not the tier this
      fixture exists to calibrate). The early-rejection case where a bad
      fixture is wrongly caught at an earlier tier is reflected in that
      earlier tier's FP rate via the known-good axis only - we do not
      double-count by also charging it to the targeted tier's FNR
      denominator, because the targeted tier was never observed.

    Fixtures that did not reach a given tier (because an earlier tier
    short-circuited the cascade) contribute neither numerator nor
    denominator for that tier.
    """
    n_known_good = sum(
        1 for o in observations if o.label.expected_outcome == PASS_ALL
    )
    n_known_bad = sum(
        1 for o in observations if o.label.expected_outcome == REJECT
    )

    entries: list[TierCalibration] = []
    for tier in tier_order:
        tp = fp = tn = fn = 0
        n_kg_obs = 0
        n_kb_obs = 0
        for obs in observations:
            if tier not in obs.tier_passed:
                continue
            passed = obs.tier_passed[tier]
            if obs.label.expected_outcome == PASS_ALL:
                n_kg_obs += 1
                if passed:
                    tn += 1
                else:
                    fp += 1
            elif obs.label.expected_reject_tier == tier:
                n_kb_obs += 1
                if passed:
                    fn += 1
                else:
                    tp += 1
        entries.append(
            TierCalibration(
                tier=tier,
                tp=tp,
                fp=fp,
                tn=tn,
                fn=fn,
                n_known_good_observed=n_kg_obs,
                n_known_bad_targeted_observed=n_kb_obs,
                fpr=_safe_rate(fp, fp + tn),
                fnr=_safe_rate(fn, fn + tp),
            )
        )

    return CalibrationReport(
        migration_id=migration_id,
        schema_version=SCHEMA_VERSION,
        n_known_good=n_known_good,
        n_known_bad=n_known_bad,
        per_tier=tuple(entries),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Funnel adapter
# ---------------------------------------------------------------------------


def observations_from_funnel_dicts(
    label: FixtureLabel, funnel_dict: Mapping[str, Any]
) -> FixtureObservation:
    """Build a :class:`FixtureObservation` from ``FunnelResult.to_dict()``.

    The funnel emits ``per_tier_verdict`` as a list of ``{tier, passed,
    cost_usd, details}`` records in execution order. We project that into
    an ordered ``tier_passed`` mapping.
    """
    raw = funnel_dict.get("per_tier_verdict") or []
    tier_passed: dict[str, bool] = {}
    for entry in raw:
        tier_passed[str(entry["tier"])] = bool(entry["passed"])
    return FixtureObservation(label=label, tier_passed=tier_passed)


# ---------------------------------------------------------------------------
# Threshold parsing
# ---------------------------------------------------------------------------


_TABLE_HEADER_RE = re.compile(r"^\s*\|\s*tier\s*\|", re.IGNORECASE)
_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_CALIB_SECTION_HEADER = "## Calibration thresholds (per tier)"


def parse_calibration_thresholds(doc_text: str) -> CalibrationThresholds:
    """Extract per-tier max-FPR / max-FNR from the hypotheses doc.

    The expected block under the ``## Calibration thresholds (per tier)``
    heading is a markdown table:

        | tier         | max_fpr | max_fnr |
        |--------------|---------|---------|
        | diff_valid   | 0.05    | 0.10    |
        | compile_only | 0.10    | 0.20    |

    Lines outside that table are ignored. Missing columns leave the rate
    unconstrained for that tier.
    """
    if _CALIB_SECTION_HEADER not in doc_text:
        return CalibrationThresholds(per_tier={})

    section = doc_text.split(_CALIB_SECTION_HEADER, 1)[1]
    # Cut at the next top-level (## ) heading so we don't read past the
    # block.
    next_heading = re.search(r"\n##\s+\S", section)
    if next_heading:
        section = section[: next_heading.start()]

    rows: dict[str, dict[str, float]] = {}
    headers: list[str] = []
    in_table = False
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            in_table = False
            headers = []
            continue
        if _SEP_RE.match(line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not in_table and _TABLE_HEADER_RE.match(line):
            headers = [c.lower() for c in cells]
            in_table = True
            continue
        if not in_table:
            continue
        if len(cells) != len(headers):
            continue
        row = dict(zip(headers, cells))
        tier = row.get("tier", "")
        if not tier:
            continue
        per: dict[str, float] = {}
        for key in ("max_fpr", "max_fnr"):
            value = row.get(key, "")
            if not value:
                continue
            try:
                per[key] = float(value)
            except ValueError:
                continue
        if per:
            rows[tier] = per
    return CalibrationThresholds(per_tier=rows)


def load_calibration_thresholds(doc_path: Path) -> CalibrationThresholds:
    return parse_calibration_thresholds(Path(doc_path).read_text())


# ---------------------------------------------------------------------------
# Validation against thresholds
# ---------------------------------------------------------------------------


def validate_against_thresholds(
    report: CalibrationReport, thresholds: CalibrationThresholds
) -> list[str]:
    """Return human-readable violations; empty list means the report passes.

    A tier with no threshold entry is unconstrained. A tier with a numeric
    threshold but a ``None`` actual rate fails (no observations means we
    cannot prove the threshold is met).
    """
    violations: list[str] = []
    for tier_name, limits in thresholds.per_tier.items():
        try:
            tier = report.tier(tier_name)
        except KeyError:
            violations.append(
                f"tier {tier_name!r}: missing from calibration report"
            )
            continue
        if "max_fpr" in limits:
            if tier.fpr is None:
                violations.append(
                    f"tier {tier_name!r}: fpr is null "
                    "(no known-good observations); "
                    f"required <= {limits['max_fpr']}"
                )
            elif tier.fpr > limits["max_fpr"]:
                violations.append(
                    f"tier {tier_name!r}: fpr={tier.fpr:.3f} "
                    f"exceeds max_fpr={limits['max_fpr']}"
                )
        if "max_fnr" in limits:
            if tier.fnr is None:
                violations.append(
                    f"tier {tier_name!r}: fnr is null "
                    "(no known-bad observations targeted at this tier); "
                    f"required <= {limits['max_fnr']}"
                )
            elif tier.fnr > limits["max_fnr"]:
                violations.append(
                    f"tier {tier_name!r}: fnr={tier.fnr:.3f} "
                    f"exceeds max_fnr={limits['max_fnr']}"
                )
    return violations


__all__ = [
    "ALLOWED_OUTCOMES",
    "PASS_ALL",
    "REJECT",
    "SCHEMA_VERSION",
    "CalibrationReport",
    "CalibrationThresholds",
    "FixtureLabel",
    "FixtureObservation",
    "TierCalibration",
    "compute_calibration",
    "load_calibration_thresholds",
    "observations_from_funnel_dicts",
    "parse_calibration_thresholds",
    "validate_against_thresholds",
]
