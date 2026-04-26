#!/usr/bin/env python3
"""Per-recipe oracle calibration driver (m1w).

Runs every fixture under
``tests/fixtures/calibration/<migration_id>/{known_good,known_bad}/``
through the tiered-oracle funnel and writes ``calibration.json`` with
per-tier FPR / FNR. The publication gate consumes the result.

Layout the driver expects (one directory per fixture):

    tests/fixtures/calibration/<migration_id>/known_good/<fixture_id>/
        label.json
        repo/                 <- staged repo the funnel runs against
            patch.diff        <- (optional) patch artifact for tier 0
            ...

Tier-0 (``diff_valid``) is local-only and runs in every invocation.
Higher tiers (``compile_only``, ``tests``, etc.) require a sandbox
adapter and are gated by ``--stages``: pass ``--stages diff,compile``
or wider once Docker is available, else accept the offline tier-0
calibration.

Usage
-----
    python scripts/calibrate.py \\
        --migration go_import_rewrite \\
        --fixtures tests/fixtures/calibration/go_import_rewrite \\
        --output configs/recipes/go_import_rewrite.calibration.json

Exit codes
----------
0   Calibration ran end-to-end and the JSON was written.
1   Wrong CLI usage / missing fixtures / no label files found.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from migration_evals.calibration import (  # noqa: E402
    FixtureLabel,
    FixtureObservation,
    compute_calibration,
    observations_from_funnel_dicts,
)
from migration_evals.funnel import STAGE_ALIASES, run_funnel  # noqa: E402
from migration_evals.harness.recipe import Recipe  # noqa: E402

# The funnel's tier names are stable; we keep the canonical run order here
# so the calibration report's per_tier list reflects funnel order.
DEFAULT_TIER_ORDER: tuple[str, ...] = (
    "diff_valid",
    "compile_only",
    "tests",
    "ast_conformance",
    "judge",
    "daikon",
)


def _placeholder_recipe() -> Recipe:
    """Tier-0 calibration only needs a Recipe shell (build/test_cmd unused)."""
    return Recipe(
        dockerfile="FROM scratch",
        build_cmd="true",
        test_cmd="true",
        harness_provenance={
            "model": "calibration",
            "prompt_version": "calibration-v1",
            "timestamp": "1970-01-01T00:00:00Z",
        },
    )


def _resolve_stages(raw: str | None) -> tuple[str, ...] | None:
    """Translate ``--stages`` (CLI alias list) into funnel tier names.

    ``None`` keeps the funnel default (run every enabled tier). The CLI
    accepts the same stage aliases as ``scripts/run_eval.py`` (``diff``,
    ``compile``, ``tests``, ``judge``, ``daikon``, ``all``).
    """
    if not raw:
        return None
    requested: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if token not in STAGE_ALIASES:
            raise ValueError(
                f"unknown --stages token {token!r}; "
                f"valid values: {sorted(STAGE_ALIASES)}"
            )
        requested.extend(STAGE_ALIASES[token])
    return tuple(dict.fromkeys(requested))


def _iter_fixtures(root: Path) -> Iterable[tuple[Path, FixtureLabel]]:
    """Yield ``(fixture_dir, FixtureLabel)`` for every committed fixture.

    Recurses into ``known_good/`` and ``known_bad/`` and treats every
    direct subdirectory as a fixture if it has a ``label.json``.
    """
    for sub in ("known_good", "known_bad"):
        bucket = root / sub
        if not bucket.is_dir():
            continue
        for fixture in sorted(bucket.iterdir()):
            label_path = fixture / "label.json"
            if not label_path.is_file():
                continue
            yield fixture, FixtureLabel.from_path(label_path)


def _run_one(
    fixture_dir: Path,
    label: FixtureLabel,
    *,
    stages: tuple[str, ...] | None,
) -> FixtureObservation:
    """Run the funnel for one fixture and return its observation."""
    repo = fixture_dir / "repo"
    if not repo.is_dir():
        raise FileNotFoundError(
            f"calibration fixture {fixture_dir} has no repo/ subdir"
        )
    funnel_result = run_funnel(
        repo,
        _placeholder_recipe(),
        adapters={},  # tier-0 only by default; higher tiers need adapters
        is_synthetic=False,
        stages=stages,
    )
    return observations_from_funnel_dicts(label, funnel_result.to_dict())


def calibrate(
    *,
    migration_id: str,
    fixtures_root: Path,
    stages: tuple[str, ...] | None,
    notes: str = "",
):
    """Drive the funnel over every fixture and return a CalibrationReport."""
    observations: list[FixtureObservation] = []
    fixture_count = 0
    for fixture_dir, label in _iter_fixtures(fixtures_root):
        observations.append(
            _run_one(fixture_dir, label, stages=stages)
        )
        fixture_count += 1
    if fixture_count == 0:
        raise FileNotFoundError(
            f"no calibration fixtures found under {fixtures_root}"
        )
    return compute_calibration(
        observations,
        migration_id=migration_id,
        tier_order=DEFAULT_TIER_ORDER,
        notes=notes,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the calibration corpus through the funnel and emit "
            "calibration.json with per-tier FPR / FNR."
        )
    )
    parser.add_argument(
        "--migration",
        required=True,
        help="migration_id (e.g. go_import_rewrite)",
    )
    parser.add_argument(
        "--fixtures",
        required=True,
        type=Path,
        help=(
            "Path to the per-recipe calibration root, containing "
            "known_good/ and known_bad/ subdirectories"
        ),
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Where to write calibration.json",
    )
    parser.add_argument(
        "--stages",
        default="diff",
        help=(
            "Comma-separated stage aliases (diff,compile,tests,judge,"
            "daikon,all). Default: diff (tier-0 only; offline)."
        ),
    )
    parser.add_argument(
        "--notes",
        default="",
        help=(
            "Optional free-text notes embedded in calibration.json "
            "(e.g. 'tier-0 only; tier-1 calibration deferred to bd-XXX')"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        stages = _resolve_stages(args.stages)
    except ValueError as exc:
        print(f"calibrate: {exc}", file=sys.stderr)
        return 1

    fixtures = args.fixtures.resolve()
    if not fixtures.is_dir():
        print(
            f"calibrate: fixtures dir does not exist: {fixtures}",
            file=sys.stderr,
        )
        return 1

    try:
        report = calibrate(
            migration_id=args.migration,
            fixtures_root=fixtures,
            stages=stages,
            notes=args.notes,
        )
    except FileNotFoundError as exc:
        print(f"calibrate: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report.to_json() + "\n")
    print(
        f"calibrate: wrote {args.output} "
        f"(known_good={report.n_known_good}, "
        f"known_bad={report.n_known_bad})"
    )
    for tier in report.per_tier:
        if tier.n_known_good_observed == 0 and tier.n_known_bad_targeted_observed == 0:
            continue
        fpr = "n/a" if tier.fpr is None else f"{tier.fpr:.3f}"
        fnr = "n/a" if tier.fnr is None else f"{tier.fnr:.3f}"
        print(
            f"  tier={tier.tier:<16} fpr={fpr:<5} fnr={fnr:<5} "
            f"(tp={tier.tp} fp={tier.fp} tn={tier.tn} fn={tier.fn})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
