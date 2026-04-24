# Tiered Oracle Funnel

This document describes the cascading oracle funnel used by the automated code
migration eval framework to score per-repo migration outcomes (PRD M1).

## Tier ordering

The funnel runs the cheapest, highest-precision tiers first and only
escalates to more expensive tiers when an earlier tier passes. The first
tier whose verdict is `passed=False` short-circuits the cascade.

| Order | Tier              | Cost target / call | Module                                                        | Purpose                                                                  |
|-------|-------------------|--------------------|---------------------------------------------------------------|--------------------------------------------------------------------------|
| 1     | `compile_only`    | $0.01              | `src/migration_evals/oracles/tier1_compile.py`             | Run the recipe's `build_cmd`. Non-zero exit fails the trial.            |
| 2     | `tests`           | $0.03              | `src/migration_evals/oracles/tier2_tests.py`               | Run the recipe's `test_cmd` against the migrated repo.                   |
| 2b    | `ast_conformance` | $0.00 (local)      | `src/migration_evals/synthetic/ast_oracle.py` (wrapped)    | Synthetic-only — regex AST-spec conformance against a known migration.   |
| 3     | `judge`           | $0.08              | `src/migration_evals/oracles/tier3_judge.py`               | Single-pass Claude judge with prompt caching on the rubric block.        |
| 4     | `daikon`          | $0.10 (target)     | `src/migration_evals/oracles/tier4_daikon.py`              | Stub today; will run Daikon invariant inference once integrated.         |

Tier 2b (`ast_conformance`) is interleaved between Tier 2 and Tier 3 only
when the trial is for a synthetic repo (`is_synthetic=True`). Tier 4 is
gated behind `adapters["enable_daikon"]`. A tier that raises
`NotImplementedError` is skipped without breaking the cascade — this is
how the Daikon stub stays out of the way until the real implementation
ships.

## Funnel orchestrator

```
T1 compile_only  -> T2 tests  -> T2b ast_conformance (synthetic only)
  -> T3 judge    -> T4 daikon (only if enable_daikon)
```

```python
from migration_evals.funnel import run_funnel

result = run_funnel(
    repo_path,
    recipe,
    adapters={"daytona": daytona, "anthropic": anthropic, "enable_daikon": False},
    is_synthetic=False,
    stages=None,  # None = all applicable; or e.g. ("compile_only",)
)
```

`run_funnel` returns a `FunnelResult` with:

- `per_tier_verdict: tuple[(tier_name, OracleVerdict), ...]` — one entry per executed tier, in execution order.
- `final_verdict: OracleVerdict` — the verdict that terminated the cascade (first failure, or last pass).
- `total_cost_usd: float` — sum of `cost_usd` across executed tiers.
- `failure_class: Optional[str]` — `None` on success; `"harness_error"` if T1 failed; `"agent_error"` for any other tier failure.

## Per-tier cost targets

Defaults in `tier{1..4}_*.py` (`DEFAULT_COST_USD`):

| Tier              | $/call |
|-------------------|--------|
| compile_only      | 0.01   |
| tests             | 0.03   |
| ast_conformance   | 0.00   |
| judge             | 0.08   |
| daikon            | 0.10   |

These are estimates calibrated against the PRD's per-repo budget. They
are passed through `OracleVerdict.cost_usd` so downstream cost accounting
is end-to-end traceable.

## Inference math: <$300 per 1k repos × 3 models

Without funnel cascading (every tier runs on every repo), the per-repo
worst case is `$0.01 + $0.03 + $0.00 + $0.08 + $0.10 = $0.22`. For 1,000
repos × 3 models that is $660 — over the budget.

With cascading the funnel falls off fast. Using PRD-default failure
distributions:

- 40% of repos fail T1 (compile errors) → only T1 cost incurred ($0.01).
- Of survivors, 30% fail T2 (existing tests broke) → T1 + T2 ($0.04).
- Of survivors of both, the judge (T3) runs (~42% of all repos, $0.08).
- Daikon is opt-in, so we exclude it from the budget math.

```
avg per-repo cost ≈ 1.00·$0.01     # T1 always runs
                  + 0.60·$0.03     # T2 runs on 60%
                  + 0.42·$0.08     # T3 runs on 42%
                  ≈ $0.0616
```

For 1,000 repos × 3 models:

```
1_000 × 3 × $0.0616 ≈ $185
```

That leaves a comfortable margin under the $300 ceiling, even with a
1.5x cushion for retry/judge re-asks. Enabling Daikon (T4) adds at most
`0.42 × 0.10 = $0.042` per repo, pushing the total to ~$310 / 3-model
sweep — at the edge, which is why Daikon stays opt-in.

## Prompt caching for the judge tier

`tier3_judge.run` sends the static rubric in the `system` parameter as a
list of content blocks, with a `cache_control: ephemeral` marker on the
rubric block. The Anthropic API treats blocks marked this way as cache
keys; subsequent calls with byte-identical content reuse the cached
prefix and only pay tokens for the per-trial user message.

Per the PRD: a Haiku judge call without caching costs ~$0.08–$0.12; with
the rubric (~600 tokens) cached, marginal cost drops to roughly $0.02 on
a cache hit. Conservatively we still budget $0.08 per judge call to
reserve room for cache invalidation on rubric updates.

## Contamination split (PRD M7)

Every result records `repo_created_at`. Aggregation (in
`src/migration_evals/contamination.py`) buckets results into
pre-cutoff and post-cutoff sets and reports:

- `score_pre` and `score_post` (0.0–1.0 pass-rate per bucket).
- `gap_pp` = `(score_pre − score_post) × 100`.
- `warning_flag = abs(gap_pp) > 5.0`.

A warning indicates the model probably saw the pre-cutoff repos during
training and the post-cutoff drop is the contamination penalty.

## CLI

```
python -m migration_evals.cli run \
    --stage {compile,tests,judge,daikon,all} \
    --repos PATH/TO/REPOS \
    --limit N \
    --out PATH/TO/OUT
```

`--stage` filters the funnel to a single tier. The CLI defaults to
`all`. Each repo's result is written to `<out>/<repo>/result.json`
conforming to `schemas/mig_result.schema.json`.

The CLI also reads two env vars used in tests / replay runs:

- `MIGRATION_EVAL_FAKE_DAYTONA_CASSETTE_DIR` — directory of cassette
  files keyed by repo name; missing files default to a successful exit
  envelope.
- `MIGRATION_EVAL_FAKE_JUDGE_CASSETTE_DIR` — directory of judge response
  envelopes keyed by repo name.
