# Hypotheses and Thresholds — Migration Eval (v1)

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

## Post-hoc changes

Post-hoc changes to hypotheses or thresholds are **not permitted** as
in-place edits to this file. Any revision requires a new file (for example,
`hypotheses_and_thresholds_v2.md`) with its own `pre_reg_date`, and results
must be re-stamped against it. In-place edits invalidate the pre-registration
record for every trial that already carries the old `pre_reg_sha` and will
be caught by `scripts/maintenance/publication_gate.py` as stale stamps.

## Ownership

The pre-registration file is governance-sensitive. The CODEOWNERS pattern
required to protect it from drive-by edits is documented in
[publication_gate.md](./publication_gate.md).
