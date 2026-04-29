#!/usr/bin/env python3
"""Pairwise judge-agreement calibration (bead migration_evals-cns).

Reads a hand-labelled overlap slice (~20 trials) where each trial
carries verdicts from {anthropic, other_family, human}, computes
pairwise Cohen's kappa, and prints a JSON summary. Any pair below the
0.6 floor is flagged as unreliable.

Why this is a separate script from ``scripts/calibrate.py``
-----------------------------------------------------------
``scripts/calibrate.py`` calibrates per-tier FPR/FNR from positive- and
negative-control fixtures: a per-recipe quality measurement of the
oracle funnel itself. This script calibrates the *judge family* — a
cross-cutting check on Tier 3 only, with a different input shape (one
file of labels, not a fixture tree) and a different output (kappa,
not FPR/FNR). Conflating the two confused the early M1 design and
made the publication gate brittle.

Input format
------------
Either:
* A JSON array of trial dicts: ``[{"trial_id": "...", "anthropic":
  bool, "other": bool, "human": bool}, ...]``
* A JSON object with a ``trials`` key wrapping the same array.

Trials missing a label are excluded from pairs that need that label,
not from the whole run, so partial-coverage slices still produce
useful kappa where they can.

Usage
-----

    python scripts/judge_calibrate.py \\
        --labels tests/fixtures/judge_calibration/sample_labels.json \\
        [--output runs/judge_calibration.json]

Exit codes
----------
0   Calibration ran end-to-end. Any unreliable pairs are reported in
    the summary but do NOT raise the exit code on their own — the
    publication gate is the place to enforce a hard floor.
1   Wrong CLI usage, file not found, malformed JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from migration_evals.judge_calibration import (  # noqa: E402
    load_trials,
    summarise_calibration,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--labels",
        required=True,
        type=Path,
        help="Path to a JSON file of {trial_id, anthropic, other, human} entries.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write the JSON summary; stdout when omitted.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.labels.is_file():
        print(f"error: labels file not found: {args.labels}", file=sys.stderr)
        return 1
    try:
        trials = load_trials(args.labels)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"error: failed to read labels: {exc}", file=sys.stderr)
        return 1

    summary: dict[str, Any] = summarise_calibration(trials)
    summary["labels_path"] = str(args.labels)

    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)

    if summary["unreliable_pairs"]:
        print(
            "warning: unreliable pair(s): "
            + ", ".join(summary["unreliable_pairs"])
            + f" — kappa floor is {summary['kappa_floor']}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
