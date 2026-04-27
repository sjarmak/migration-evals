# Hypotheses and Thresholds - Migration Eval (v1)

This file is the **pre-registration** artifact for the migration eval
framework. Every hypothesis and threshold listed here is declared **BEFORE**
any results are collected. The file's sha256 is stamped into every
`result.json` as `pre_reg_sha` so that an auditor can prove, at any later
date, that the result was scored against these (and only these) claims.

## v1 hypotheses

One hypothesis per line. Each must be falsifiable, reference a concrete
metric defined below, and state a direction of effect.

- H1: Mean `score_pre_cutoff` for `java8_17` exceeds `score_post_cutoff` by
  >= 0.05 across the v1 task set (contamination signal).
- H2: On the `python2_3` migration, the `tests` oracle tier disagrees with
  the `compile_only` tier on at most 10% of trials.
- H3: The four-way failure classifier in `failure_class.py` reaches >= 90%
  precision on the labeled holdout fixtures.
- H4: Agents using the baseline harness produce strictly fewer
  `infra_error` trials than the sandbox harness by a margin of >= 5
  percentage points.

## Thresholds table

| metric                       | direction | value | pre_reg_date |
|------------------------------|-----------|-------|--------------|
| java_pre_vs_post_cutoff_gap  | >=        | 0.05  | 2026-04-24   |
| python_tier_disagreement     | <=        | 0.10  | 2026-04-24   |
| failure_classifier_precision | >=        | 0.90  | 2026-04-24   |
| infra_error_rate_delta       | >=        | 0.05  | 2026-04-24   |

## Calibration thresholds (per tier)

Per-tier maximum false-positive / false-negative rates for the oracle
funnel. A published headline rate must come from a recipe whose committed
`calibration.json` (produced by `scripts/calibrate.py` against the corpus
under `tests/fixtures/calibration/<migration_id>/{known_good,known_bad}/`)
satisfies every threshold listed here. The publication gate enforces this
under `--require-calibration` (m1w).

A numeric `max_fpr` / `max_fnr` requires the calibration's actual rate to
be `<=` the threshold; a `null` actual rate (no observations for that
tier) does not satisfy a numeric threshold. An empty cell means no
constraint for that rate.

Tier-1 (`compile_only`) and tier-2 (`tests`) calibration requires Docker;
the corpus under `tests/fixtures/calibration/go_import_rewrite/` (x8w)
provides 2 known-good and 2-targeted-known-bad fixtures per tier, so the
finest-grain rate observable is `0.5` per fixture flip. Thresholds below
are set with that resolution in mind: a single fixture regression in
either direction trips the gate.

| tier         | max_fpr | max_fnr |
|--------------|---------|---------|
| diff_valid   | 0.05    | 0.10    |
| compile_only | 0.10    | 0.20    |
| tests        | 0.15    | 0.25    |

## Post-hoc changes

Post-hoc changes to hypotheses or thresholds are **not permitted** as
in-place edits to this file. Any revision requires a new file (for example,
`hypotheses_and_thresholds_v2.md`) with its own `pre_reg_date`, and results
must be re-stamped against it. In-place edits invalidate the pre-registration
record for every trial that already carries the old `pre_reg_sha` and will
be caught by `migration_evals.publication_gate` as stale stamps.

## Ownership

The pre-registration file is governance-sensitive. The CODEOWNERS pattern
required to protect it from drive-by edits is documented in
[publication_gate.md](./publication_gate.md).
