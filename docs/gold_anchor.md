# Gold Anchor Correlation (PRD M4-lite)

The gold anchor is a small, frozen set of human-adjudicated labels used to
sanity-check the tiered oracle funnel. If the funnel's pass/fail signal
correlates strongly with human verdicts, we trust it to gate publication.
If it does not, we mark the evaluation as broken and block publication.

## Scope: 50 repos

The gold set is sized at roughly **50 repositories** sampled across the
migration domains (Java 8->17, Python 2->3, etc.). This is the smallest
sample that gives a tight enough confidence interval (~+-0.15 CI width at
N=50 via 10,000-iteration bootstrap) to distinguish a healthy funnel
(point estimate >= 0.7) from a broken one with acceptable statistical
power.

Why 50 specifically:

- N=30 is too small; bootstrap CI widths exceed +-0.2 which cannot
  reliably distinguish "healthy" (point >= 0.7) from "marginal"
  (point ~= 0.6).
- N=100 is better statistically but doubles the review burden and we
  cannot afford that cadence (see re-anchoring below).
- N=50 hits the bootstrap CI target while remaining re-reviewable inside a
  two-week window.

## Re-anchoring cadence

We re-anchor (review fresh trials, regenerate the gold set) on the
following triggers:

- **Quarterly** as a default cadence. Every 3 months a 50-repo slice is
  re-reviewed end-to-end.
- **Oracle spec change**: any non-trivial edit to `oracle_spec.yaml` (new
  tier, threshold change, failure-class reclassification) invalidates the
  existing gold set; re-anchor before resuming publication.
- **Model family change**: a new agent model or a model version bump large
  enough to shift the funnel's operating curve re-anchors immediately.
- **Eval-broken event**: if `eval_broken=true` fires in a run, we pause
  publication, investigate, and re-anchor before the next publication
  attempt.

## 12-month half-life

Individual labels have a 12-month half-life: a label conducted a year ago
carries roughly half the weight of a fresh one, and labels older than 12
months are dropped entirely before correlation is computed. This prevents
drift in the ecosystem (language releases, new library majors) from
inflating or deflating the correlation against a stale reference.

In practice, the quarterly re-anchoring keeps the median label age well
under 6 months, so the half-life is a safety net rather than a load-bearing
mechanism.

## Privacy

Reviewer notes may contain internal or sensitive context (reviewer names,
customer repo paths, internal project codes). Therefore:

- **Real gold labels never ship in this repository.** Only
  `data/gold_anchor_template.json` (an empty array `[]`)
  and this document are checked in.
- Gold labels are stored in the private analysis bucket and loaded at
  analysis time via `src/migration_evals/gold_anchor.py::load_gold_set`.
- `reviewer_notes` should avoid personally identifying information about
  the reviewer; use role-based signatures where possible.
- Before exporting any gold-set-derived artifact beyond the internal team,
  scrub `reviewer_notes` and replace `repo_url` with a hashed identifier.

## Computing the correlation

`src/migration_evals/gold_anchor.py::correlate(funnel_results, gold)`
computes the Phi coefficient (equivalent to Pearson on two binary
variables) between funnel `success` and `human_verdict=="accept"`, and
returns a `CorrelationReport`:

- `point` — Phi coefficient on the full matched pair set.
- `ci_low`, `ci_high` — 95% bootstrap CI bounds (10,000 iterations,
  seeded; same seed + same input -> identical CI).
- `eval_broken` — True iff `point < 0.7` **or** `ci_low < 0.5`. The OR is
  deliberate: a low point estimate alone is disqualifying, and a wide CI
  whose lower bound dips below 0.5 indicates we cannot rule out a broken
  funnel even if the point estimate looks acceptable.
- `details` — diagnostic dict (n_pairs, dropped_funnel, dropped_gold,
  n_bootstrap, seed).

## Publication gate integration

`python -m migration_evals.publication_gate --check-run <run_dir>` now also
inspects `<run_dir>/summary.json` if present:

- Missing/null `gold_anchor_correlation` section in an otherwise present
  `summary.json`: gate fails.
- `eval_broken=true` in `gold_anchor_correlation`: gate fails.
- Absent `summary.json`: gate falls back to the prior behaviour (stamp
  validation only).
- `--require-gold-anchor` flag: promotes the check to required. Absent
  `summary.json` now also fails.

See `docs/publication_gate.md` for the full gate contract.
