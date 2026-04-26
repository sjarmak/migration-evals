# Publication Gate - Migration Eval (PRD D2)

The publication gate is a mechanical CI check that prevents publishing any
migration-eval run whose per-trial `result.json` files are not fully
stamped against the committed oracle spec, recipe spec, and pre-registered
hypotheses file.

## What the gate checks

`python -m migration_evals.publication_gate --check-run <dir>` exits 0 iff:

1. `<dir>/manifest.json` exists and declares `oracle_spec`, `recipe_spec`,
   and `hypotheses` paths.
2. Every `result.json` under `<dir>` (any depth) carries non-empty
   `oracle_spec_sha`, `recipe_spec_sha`, and `pre_reg_sha` fields.
3. Each stored stamp matches the sha256 of the committed file referenced in
   `manifest.json` at check time. A mismatch is a **stale stamp** and blocks
   publication until the run is re-scored (or the spec change is reverted).

On any failure, the gate exits 1 and prints a specific diagnostic to stderr
(missing stamp field, stale stamp, missing manifest, missing referenced file,
or no `result.json` files found).

## Canonical spec mapping

Each per-recipe template under `configs/recipes/<migration_id>.yaml`
declares a `stamps:` block pointing at the three (or four) files whose
sha256 is bound to every trial:

| stamp key      | typical file                           |
|----------------|----------------------------------------|
| `oracle_spec`  | `configs/oracle_spec.yaml`             |
| `recipe_spec`  | `configs/recipes/<migration_id>.yaml`  |
| `hypotheses`   | `docs/hypotheses_and_thresholds.md`    |
| `prompt_spec`  | (optional) `configs/prompts/<id>.md`   |

When `scripts/run_eval.py` creates the run's `--output-root`, it emits
`manifest.json` from this block (paths rewritten relative to the run
directory so the committed manifest is portable). The runner then
stamps each `result.json` from the same files via
`migration_evals.pre_reg.stamp_result`. The gate later checks that
every stamp matches the file the manifest still points at.

## GitHub Actions workflow

The committed workflow lives at
[`.github/workflows/publication_gate.yml`](../.github/workflows/publication_gate.yml).
It triggers on any PR that touches `runs/analysis/mig_*/`, enumerates
changed run directories from the PR diff, and runs
`python -m migration_evals.publication_gate --check-run <run_dir>` over
each. The gate is intentionally invoked via `-m` rather than by direct
script path so that Python's import machinery does not put
`src/migration_evals/` on `sys.path[0]` (the package's `types.py` would
otherwise shadow the stdlib `types` module and break `argparse`).

## CODEOWNERS pattern (governance - not applied here)

The pre-registered hypotheses file must be owned by the migration-eval-owners
leadership group so that post-hoc edits require explicit review. The
following line MUST be added to `.github/CODEOWNERS` by a team
administrator (this repository change is a governance action and is NOT
performed by this work unit):

```
docs/hypotheses_and_thresholds.md @framework-owners
```

The same pattern may be extended to the broader `docs/`
directory if the working group prefers broader coverage:

```
docs/ @framework-owners
```

## Operator playbook for stale-stamp failures

1. `publication_gate.py` names the stale field and the offending trial's
   `result.json` path.
2. Inspect the committed spec file referenced by the manifest key for that
   stamp (for example, `oracle_spec` for `oracle_spec_sha`). Determine
   whether the spec changed legitimately.
3. If the spec change was intentional: re-score the affected trials against
   the new spec so their stamps refresh. Do **not** hand-edit stored SHAs.
4. If the spec change was accidental: revert the spec and rerun the gate.

The gate is mechanical by design. It does not reason about *why* a stamp
went stale - only *that* it did.
