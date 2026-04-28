#!/usr/bin/env python3
"""Mechanical publication gate for migration-eval runs (PRD D2).

Fails CI if any ``result.json`` under a run directory is missing an
``oracle_spec_sha`` / ``recipe_spec_sha`` / ``pre_reg_sha`` stamp, or if any
stored stamp does not match the sha256 of the committed spec file it refers
to. With ``--require-calibration``, the gate also refuses runs whose
recipe lacks a committed ``calibration.json`` or whose per-tier FPR / FNR
exceeds the thresholds declared in ``docs/hypotheses_and_thresholds.md``.

Manifest contract
-----------------
Each run directory must contain a ``manifest.json`` with the keys
``oracle_spec``, ``recipe_spec``, and ``hypotheses``. Values are filesystem
paths (absolute, or relative to the run directory) pointing at the committed
files whose SHAs must match the per-trial stamps. The manifest may also
declare ``calibration_report`` (path to the per-recipe calibration.json);
the gate enforces it under ``--require-calibration``.

Usage
-----
    python -m migration_evals.publication_gate --check-run <run_dir>

Exit codes
----------
0   Every result.json under <run_dir> has non-empty, non-stale stamps.
1   manifest missing / invalid, result.json missing a stamp, stale stamp, or
    no result.json files found.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from migration_evals.calibration import (
    CalibrationReport,
    load_calibration_thresholds,
    validate_against_thresholds,
)
from migration_evals.pre_reg import compute_spec_sha

REQUIRED_MANIFEST_KEYS = ("oracle_spec", "recipe_spec", "hypotheses")
# Optional manifest keys: enforced as a stamp only when present in the
# manifest.json. ``prompt_spec`` is the canonical artifact for a
# prompt-defined migration (the migration target lives in the agent prompt
# rather than a hand-authored recipe). ``calibration_report`` points at
# the per-recipe calibration.json produced by ``scripts/calibrate.py``;
# the gate consults it whenever ``--require-calibration`` is set.
OPTIONAL_MANIFEST_KEYS = ("prompt_spec", "calibration_report")
STAMP_FIELDS = {
    "oracle_spec_sha": "oracle_spec",
    "recipe_spec_sha": "recipe_spec",
    "pre_reg_sha": "hypotheses",
}
OPTIONAL_STAMP_FIELDS = {
    "prompt_sha": "prompt_spec",
}

GOLD_ANCHOR_KEY = "gold_anchor_correlation"
GOLD_ANCHOR_REQUIRED_FIELDS = ("point", "ci_low", "ci_high", "eval_broken")

_MISSING = object()


def _fail(message: str) -> int:
    print(f"publication_gate: FAIL: {message}", file=sys.stderr)
    return 1


def _load_manifest(run_dir: Path) -> dict | None:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        with manifest_path.open() as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("manifest.json must be a JSON object")
    missing = [k for k in REQUIRED_MANIFEST_KEYS if k not in data]
    if missing:
        raise ValueError(f"manifest.json missing required keys: {', '.join(missing)}")
    return data


def _resolve_spec_path(run_dir: Path, raw: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (run_dir / candidate).resolve()
    return candidate


def _check_gold_anchor(run_dir: Path, *, require: bool) -> int | None:
    """Inspect summary.json for a gold_anchor_correlation section.

    Returns:
        ``None`` when the check passes (or is skipped because summary.json is
        absent and ``require`` is False). ``1`` when the gate should fail.
    """
    summary_path = run_dir / "summary.json"
    if not summary_path.is_file():
        if require:
            return _fail(
                f"summary.json missing under {run_dir} - required by " "--require-gold-anchor"
            )
        return None
    try:
        with summary_path.open() as fh:
            summary = json.load(fh)
    except json.JSONDecodeError as exc:
        return _fail(f"{summary_path}: invalid JSON ({exc})")
    if not isinstance(summary, dict):
        return _fail(f"{summary_path}: summary.json must be a JSON object")

    section = summary.get(GOLD_ANCHOR_KEY, _MISSING)
    if section is _MISSING:
        if require:
            return _fail(
                f"{summary_path}: missing gold_anchor_correlation section "
                "(required by --require-gold-anchor)"
            )
        return None
    if section is None:
        return _fail(f"{summary_path}: missing gold_anchor_correlation (section is null)")
    if not isinstance(section, dict):
        return _fail(
            f"{summary_path}: missing gold_anchor_correlation " "(section is not an object)"
        )
    missing_fields = [f for f in GOLD_ANCHOR_REQUIRED_FIELDS if section.get(f) is None]
    if missing_fields:
        return _fail(
            f"{summary_path}: missing gold_anchor_correlation fields: "
            f"{', '.join(missing_fields)}"
        )
    if section["eval_broken"] is True:
        return _fail(f"{summary_path}: gold_anchor_correlation reports " "eval_broken=true")
    return None


def _check_calibration(run_dir: Path, manifest: dict, *, require: bool) -> int | None:
    """Enforce the per-recipe calibration contract (m1w).

    When ``--require-calibration`` is set, the manifest must declare a
    ``calibration_report`` path, the file must be loadable as a
    :class:`CalibrationReport`, and its per-tier rates must satisfy the
    thresholds parsed from ``hypotheses_and_thresholds.md`` (the file the
    manifest already references via ``hypotheses``).

    Returns ``None`` on success or skip; ``1`` on failure.
    """
    if not require:
        return None
    raw = manifest.get("calibration_report")
    if not raw:
        return _fail(
            f"manifest.json missing 'calibration_report' "
            f"(required by --require-calibration) under {run_dir}"
        )
    calibration_path = _resolve_spec_path(run_dir, raw)
    if not calibration_path.is_file():
        return _fail(f"calibration_report file missing: {calibration_path}")
    try:
        report = CalibrationReport.from_path(calibration_path)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        return _fail(f"{calibration_path}: cannot load calibration report ({exc})")

    hypotheses_path = _resolve_spec_path(run_dir, manifest["hypotheses"])
    if not hypotheses_path.is_file():
        # The required-stamp check below will surface this; bail early to
        # avoid a confusing trace.
        return None
    thresholds = load_calibration_thresholds(hypotheses_path)
    if not thresholds.per_tier:
        return _fail(
            f"{hypotheses_path}: --require-calibration set but the doc "
            "declares no '## Calibration thresholds (per tier)' table"
        )
    violations = validate_against_thresholds(report, thresholds)
    if violations:
        return _fail(
            f"{calibration_path}: calibration violates thresholds: " + "; ".join(violations)
        )
    return None


def check_run(
    run_dir: Path,
    *,
    require_gold_anchor: bool = False,
    require_calibration: bool = False,
) -> int:
    """Return 0 if the run passes the gate, 1 otherwise."""
    if not run_dir.is_dir():
        return _fail(f"run dir does not exist: {run_dir}")

    try:
        manifest = _load_manifest(run_dir)
    except ValueError as exc:
        return _fail(str(exc))
    if manifest is None:
        return _fail(f"manifest.json missing under {run_dir}")

    calibration_failure = _check_calibration(run_dir, manifest, require=require_calibration)
    if calibration_failure is not None:
        return calibration_failure

    # Precompute expected SHAs from the committed files referenced in the
    # manifest. Required stamps are always enforced; optional stamps
    # (e.g. prompt_sha) are enforced only when the manifest declares them.
    expected_shas: dict[str, str] = {}
    for stamp_field, manifest_key in STAMP_FIELDS.items():
        spec_path = _resolve_spec_path(run_dir, manifest[manifest_key])
        if not spec_path.is_file():
            return _fail(f"manifest references missing file for {manifest_key!r}: " f"{spec_path}")
        expected_shas[stamp_field] = compute_spec_sha(spec_path)
    for stamp_field, manifest_key in OPTIONAL_STAMP_FIELDS.items():
        if manifest_key not in manifest:
            continue
        spec_path = _resolve_spec_path(run_dir, manifest[manifest_key])
        if not spec_path.is_file():
            return _fail(f"manifest references missing file for {manifest_key!r}: " f"{spec_path}")
        expected_shas[stamp_field] = compute_spec_sha(spec_path)

    result_paths = sorted(run_dir.rglob("result.json"))
    if not result_paths:
        return _fail(f"no result.json files found under {run_dir}")

    for result_path in result_paths:
        try:
            with result_path.open() as fh:
                payload = json.load(fh)
        except json.JSONDecodeError as exc:
            return _fail(f"{result_path}: invalid JSON ({exc})")
        if not isinstance(payload, dict):
            return _fail(f"{result_path}: result.json must be a JSON object")

        for stamp_field, expected in expected_shas.items():
            stored = payload.get(stamp_field)
            if not stored:
                return _fail(f"{result_path}: missing stamp {stamp_field!r}")
            if stored != expected:
                return _fail(
                    f"{result_path}: stale stamp {stamp_field!r} "
                    f"(stored={stored!r}, expected={expected!r})"
                )

    gold_failure = _check_gold_anchor(run_dir, require=require_gold_anchor)
    if gold_failure is not None:
        return gold_failure

    print(
        f"publication_gate: OK - {len(result_paths)} result.json file(s) "
        f"verified under {run_dir}"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that every result.json under a run directory carries "
            "valid oracle_spec_sha / recipe_spec_sha / pre_reg_sha stamps "
            "matching the committed spec files referenced in manifest.json."
        )
    )
    parser.add_argument(
        "--check-run",
        required=True,
        type=Path,
        help="Path to the run directory to verify.",
    )
    parser.add_argument(
        "--require-gold-anchor",
        action="store_true",
        help=(
            "Require summary.json with a valid gold_anchor_correlation "
            "section. Without this flag, the gate only enforces "
            "gold-anchor correctness when summary.json is present."
        ),
    )
    parser.add_argument(
        "--require-calibration",
        action="store_true",
        help=(
            "Require manifest.json to declare a 'calibration_report' "
            "path; load it and refuse the run if any tier's FPR / FNR "
            "exceeds the thresholds in hypotheses_and_thresholds.md "
            "under '## Calibration thresholds (per tier)'."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return check_run(
        args.check_run,
        require_gold_anchor=args.require_gold_anchor,
        require_calibration=args.require_calibration,
    )


if __name__ == "__main__":
    raise SystemExit(main())
