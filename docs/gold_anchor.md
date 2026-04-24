# Gold Anchor Correlation (PRD M4-lite)

The gold anchor is a small, frozen set of **merge-survival labels** used to
sanity-check the tiered oracle funnel. If the funnel's pass/fail signal
correlates strongly with these external verdicts, we trust it to gate
publication. If it does not, we mark the evaluation as broken and block
publication.

## Where the labels come from

Labels are harvested automatically by
[`scripts/mine_gold_anchor.py`](../scripts/mine_gold_anchor.py) from public
OSS migration PRs. The procedure:

1. Use the GitHub Search API (`gh search prs`) to find PRs that
   demonstrably executed the target migration - for Java 8→17, PRs that
   touched lambda rewrites, `var` introductions, Optional chains, text
   blocks, records, sealed classes, pattern matching, enhanced switches,
   or deprecated-API swaps as identified by file diff content.
2. For each candidate PR, check the merge state. PRs that were merged AND
   survived ≥30 days without a revert commit on the same files become
   `human_verdict="accept"` labels. PRs that were closed-unmerged or
   merged-then-reverted become `human_verdict="reject"` labels.
3. Persist the result as a JSON array validated against
   [`schemas/gold_anchor_entry.schema.json`](../schemas/gold_anchor_entry.schema.json).

The `human_verdict` field name is preserved for backward compatibility with
existing fixtures, but it represents an *implicit maintainer verdict*
observed via merge-survival, not a fresh per-trial human review. The
correlation analysis treats `accept` / `reject` as binary outcomes
regardless of how the label was sourced.

The gold-anchor refresh is a scheduled CI job, not a planning meeting.

## Scope: 50 repos

The gold set is sized at roughly **50 repositories** sampled across the
migration domains (Java 8→17, Python 2→3, etc.). This is the smallest
sample that gives a tight enough confidence interval (~±0.15 CI width at
N=50 via 10,000-iteration bootstrap) to distinguish a healthy funnel
(point estimate ≥ 0.7) from a broken one with acceptable statistical
power.

Why 50 specifically:

- N=30 is too small; bootstrap CI widths exceed ±0.2 which cannot
  reliably distinguish "healthy" (point ≥ 0.7) from "marginal"
  (point ≈ 0.6).
- N=100 is better statistically but doubles the GitHub API budget for
  the harvest job.
- N=50 hits the bootstrap CI target while a re-harvest comfortably fits in
  a single CI job inside the GitHub API's hourly secondary rate limit.

## Re-anchoring cadence

Re-anchor (re-run the harvester, regenerate the gold set) on these
triggers:

- **Quarterly** as a default cadence - the harvester is a scheduled job.
- **Oracle spec change**: any non-trivial edit to `oracle_spec.yaml` (new
  tier, threshold change, failure-class reclassification) invalidates the
  existing gold set; re-anchor before resuming publication.
- **Model family change**: a new agent model or a model version bump large
  enough to shift the funnel's operating curve re-anchors immediately.
- **Eval-broken event**: if `eval_broken=true` fires in a run, pause
  publication, investigate, and re-anchor before the next publication
  attempt.

## 12-month half-life

Individual labels have a 12-month half-life: a label harvested a year ago
carries roughly half the weight of a fresh one, and labels older than 12
months are dropped entirely before correlation is computed. This prevents
ecosystem drift (language releases, new library majors) from inflating or
deflating the correlation against a stale reference.

In practice, the quarterly re-harvesting keeps the median label age well
under 6 months, so the half-life is a safety net rather than a load-bearing
mechanism.

## What's checked in

- [`data/gold_anchor_template.json`](../data/gold_anchor_template.json) - an
  empty array `[]`, the seed for `mine_gold_anchor.py`.
- [`schemas/gold_anchor_entry.schema.json`](../schemas/gold_anchor_entry.schema.json)
  - the validation schema.
- This document.

The harvested `data/gold_anchor.json` is **not checked in by default**. It
is generated on demand by the harvester and consumed by the report step.
Add it to `.gitignore` if you regenerate frequently and don't want
contamination from old labels in `git diff`.

## Computing the correlation

`src/migration_evals/gold_anchor.py::correlate(funnel_results, gold)`
computes the Phi coefficient (equivalent to Pearson on two binary
variables) between funnel `success` and `human_verdict=="accept"`, and
returns a `CorrelationReport`:

- `point` - Phi coefficient on the full matched pair set.
- `ci_low`, `ci_high` - 95% bootstrap CI bounds (10,000 iterations,
  seeded; same seed + same input → identical CI).
- `eval_broken` - True iff `point < 0.7` **or** `ci_low < 0.5`. The OR is
  deliberate: a low point estimate alone is disqualifying, and a wide CI
  whose lower bound dips below 0.5 indicates we cannot rule out a broken
  funnel even if the point estimate looks acceptable.
- `details` - diagnostic dict (n_pairs, dropped_funnel, dropped_gold,
  n_bootstrap, seed).

## Publication gate integration

`python -m migration_evals.publication_gate --check-run <run_dir>` also
inspects `<run_dir>/summary.json` if present:

- Missing/null `gold_anchor_correlation` section in an otherwise present
  `summary.json`: gate fails.
- `eval_broken=true` in `gold_anchor_correlation`: gate fails.
- Absent `summary.json`: gate falls back to the prior behaviour (stamp
  validation only).
- `--require-gold-anchor` flag: promotes the check to required. Absent
  `summary.json` now also fails.

See [`docs/publication_gate.md`](publication_gate.md) for the full gate
contract.
