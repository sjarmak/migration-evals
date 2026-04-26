# Migration Eval CLI - Quickstart

All subcommands live under `python -m migration_evals.cli`:

```
python -m migration_evals.cli {run,report,regression,harness,probe} ...
```

This document shows a minimal invocation for each. The canonical smoke
path is the java8_17 3-repo fixture config.

---

## `run`

Two modes:

### Config-driven (recommended)

```bash
python -m migration_evals.cli run --config configs/java8_17_smoke.yaml
```

Reads a YAML configuration, cascades each repo through the tiered oracle
funnel, and writes one `result.json` per trial under
`output_root/<repo_name>_<seed>/result.json`. A `summary.json` is also
written at `output_root` with the three spec SHAs so the report step is
trivially fast.

Required config keys: `migration_id`, `agent_model`, `variant`,
`output_root`, `repos`, `adapters`, `stamps`. See the smoke config for
an annotated example.

Every emitted `result.json` validates against
`schemas/mig_result.schema.json` by construction.

### Fixture / cassette path (legacy)

```bash
python -m migration_evals.cli run \
    --repos tests/fixtures/funnel_repos \
    --out /tmp/funnel_run \
    --stage compile \
    --limit 10
```

This path is preserved for the funnel integration test in
`tests/test_funnel.py`.

### Recipes and canonical stages

Each migration ships a recipe template under `configs/recipes/<id>.yaml`
(consumed by `scripts/run_eval.py`). Most recipes exercise tiers 0..2
(`--stages diff,compile,tests`); a few intentionally cap at tier 1.

| Recipe                          | Canonical stages         | Notes                                                                 |
|---------------------------------|--------------------------|------------------------------------------------------------------------|
| `java8_17`                      | `diff,compile,tests`     | mvn build + mvn test.                                                  |
| `go_import_rewrite`             | `diff,compile,tests`     | `go build ./...` + `go test ./...`.                                    |
| `dockerfile_base_image_bump`    | `diff,compile`           | tier 2 skipped; target test cmd varies per repo. test_cmd is a sentinel that errors if invoked. |

---

## `report`

```bash
python -m migration_evals.cli report \
    --run runs/analysis/mig_java8_17/claude-sonnet-4-6/smoke \
    --out /tmp/java8_17_smoke_report.md
```

Aggregates all `result.json` files under `--run` into a single markdown
report. Sections, in order:

1. **Funnel table** - one row per tier (compile, tests, ast, judge,
   daikon) with columns `tier_name`, `n_entered`, `n_passed`, `n_failed`,
   `cumulative_pass_rate`.
2. **Contamination split** - `score_pre_cutoff`, `score_post_cutoff`,
   `gap_pp`, `warning_flag`, bucketed against `--cutoff` (defaults to the
   value from `summary.json`, or `2025-01-01` if absent).
3. **Gold-anchor correlation** *(optional)* - Phi coefficient + 95%
   bootstrap CI + `eval_broken` flag. Rendered only when `--gold` points
   at a gold-anchor JSON file.
4. **Spec stamps** - `oracle_spec_sha`, `recipe_spec_sha`, `pre_reg_sha`
   read from `summary.json` (or the first trial's `result.json` as a
   fallback).
5. **Failure class breakdown** - count per failure class across failed
   trials.

### Rendering strategy

The runtime rendering path is a hand-rolled `format_report` using plain
f-strings (see `src/migration_evals/report.py`). We deliberately do
**not** depend on Jinja2 at runtime so the CLI keeps a minimal dep
footprint.

The companion reference template at
`src/migration_evals/templates/report.md.j2` is a **documentation
artifact only** - it describes the target structure and is not loaded by
the CLI. If you need Jinja2 rendering in your own tooling, import the
template manually and pass the dict returned by
`migration_evals.report.build_report_data`.

---

## `regression`

```bash
python -m migration_evals.cli regression \
    --from runs/analysis/mig_java8_17/v1 \
    --to   runs/analysis/mig_java8_17/v2 \
    --out  /tmp/regressions.md
```

Produces a markdown table listing every task that passed in the baseline
directory and failed in the candidate directory. Both directories are
scanned recursively for `result.json` files. See
`src/migration_evals/ledger.py::run_regression` for the implementation.

The `--from` / `--to` arguments can point at ledger directories or
plain run directories - any layout that contains `result.json` under
`<root>/**/` works.

---

## `harness`

```bash
# Synthesize a recipe for one repo (cassette-driven in fixture runs):
MIGRATION_EVAL_FAKE_HARNESS_CASSETTE_DIR=tests/fixtures/harness_cassettes \
  python -m migration_evals.cli harness synth --repo path/to/repo

# Validate a committed recipe / meta.json without calling the adapter:
python -m migration_evals.cli harness validate --repo path/to/repo
```

The `synth` action delegates to
`src/migration_evals/harness/synth.py::synthesize_recipe`. The
`validate` action constructs a `Recipe` from the repo's `meta.json` and
reports the outcome without invoking the LLM.

---

## `probe`

```bash
python -m migration_evals.cli probe --ecosystem python23 --out /tmp/probe.json
```

Falsification-probe scaffolding. The full probe harness is a future
work unit; this subcommand currently emits a structured stub envelope so
operators can wire dashboards/CI against a stable CLI surface. Exits 0
and writes a small JSON payload describing the ecosystem + intent.

---

## End-to-end smoke

The 3-repo smoke completes in under 2 minutes wall-clock against the
committed fixtures:

```bash
python -m migration_evals.cli run \
    --config configs/java8_17_smoke.yaml

python -m migration_evals.cli report \
    --run runs/analysis/mig_java8_17/claude-sonnet-4-6/smoke \
    --out /tmp/java8_17_smoke_report.md
```

Both invocations are exercised by `tests/test_cli_runner.py`
and `tests/test_report.py`.
