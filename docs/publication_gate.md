# Publication Gate — Migration Eval (PRD D2)

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

## GitHub Actions workflow snippet

Trigger the gate on any PR that touches `runs/analysis/mig_*/`.

```yaml
name: migration-eval-publication-gate

on:
  pull_request:
    paths:
      - "runs/analysis/mig_*/**"

jobs:
  publication-gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Enumerate migration runs in this PR
        id: runs
        run: |
          git fetch origin "${{ github.base_ref }}" --depth=1
          CHANGED_RUNS=$(git diff --name-only "origin/${{ github.base_ref }}"...HEAD \
            | grep -E "^runs/analysis/mig_[^/]+/" \
            | cut -d/ -f1-3 \
            | sort -u)
          echo "runs<<EOF" >> "$GITHUB_OUTPUT"
          echo "$CHANGED_RUNS" >> "$GITHUB_OUTPUT"
          echo "EOF" >> "$GITHUB_OUTPUT"
      - name: Run publication gate on each changed run
        if: steps.runs.outputs.runs != ''
        run: |
          echo "${{ steps.runs.outputs.runs }}" | while read -r run_dir; do
            [ -z "$run_dir" ] && continue
            echo "::group::gate: $run_dir"
            python python -m migration_evals.publication_gate --check-run "$run_dir"
            echo "::endgroup::"
          done
```

## CODEOWNERS pattern (governance — not applied here)

The pre-registered hypotheses file must be owned by the agentic-migrations
leadership group so that post-hoc edits require explicit review. The
following line MUST be added to `.github/CODEOWNERS` by a team
administrator (this repository change is a governance action and is NOT
performed by this work unit):

```
docs/hypotheses_and_thresholds.md @wg-agentic-migrations-leads
```

The same pattern may be extended to the broader `docs/`
directory if the working group prefers broader coverage:

```
docs/ @wg-agentic-migrations-leads
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
went stale — only *that* it did.
