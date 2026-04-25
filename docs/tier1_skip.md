# Tier 1 publication-gate skip decision

Tier 1 (per-changeset compile + tests scoring of agent-produced diffs)
needs a list of agent-driven workflow instance ids and an artifact
backend that exposes, for each instance, the diff plus the base commit
it was produced against. The driver
[`scripts/run_eval.py`](../scripts/run_eval.py) consumes that list and
writes per-trial `result.json` files, which the existing CLI
(`python -m migration_evals.cli report`) then summarises as a markdown
funnel report.

The companion bead's acceptance gate — committing a calibrated
`docs/poc_tier1_report.md` showing diff-valid / compile / tests rates
across N>=10 real instances — is **deferred** until the orchestrator
under evaluation is producing instances at that scale. This file
documents the gate, the orchestrator preconditions, and the steps to
unskip.

## What tier 1 needs from any agent-orchestration system

Tier 1 is intentionally agnostic to the orchestrator that produced the
changesets. To run the driver against real data the orchestrator
needs to expose, for each completed instance, an artifact directory
with the layout consumed by `FilesystemChangesetProvider`:

```
<root>/<instance_id>/meta.json   # required keys:
                                 #   repo_url, commit_sha, workflow_id,
                                 #   agent_runner, agent_model
<root>/<instance_id>/patch.diff  # raw unified diff produced by the agent
```

Other backends (S3-compatible object store, blob storage, HTTP artifact
server) implement `migration_evals.changesets.ChangesetProvider`
alongside the consuming pipeline and extend
`migration_evals.changesets.get_provider`. See
[`docs/oracle_funnel.md` §Changeset providers](./oracle_funnel.md#changeset-providers-pulling-agent-diffs-into-the-funnel)
for the interface contract.

## Why deferred today

A funnel-rate number is a calibration measurement, not a build
artefact. It only carries signal when the corpus is big enough to push
binomial sampling error below the size of the differences the report
is supposed to surface. Below ~10 real instances the per-tier rates
move by tens of percentage points based on which one or two changesets
ended up in the slice. The framework is wired and tested; the report
is held until the slice is meaningful.

## Three conditions that re-open tier 1 publication

Re-open the publication gate when **all three** hold:

1. The orchestrator has shipped at least 10 agent-driven workflow
   instances on the target migration (e.g. `java8_17`).
2. There is a stable filter for "this instance came from the agent
   migration workflow" (a workflow name, a tag, an artifact-storage
   prefix) that an exporter can rely on without inspecting opaque
   pipeline definitions.
3. Someone with read access to the orchestrator's artifact storage
   stages the changesets into the
   `<root>/<instance_id>/{meta.json, patch.diff}` layout above.

At that point, run:

```bash
# 1. Drive the funnel over real instances. Picks up
#    configs/recipes/<migration>.yaml automatically.
python scripts/run_eval.py \
    --migration java8_17 \
    --provider filesystem --root /path/to/staged \
    --eval-root /tmp/eval \
    --output-root runs/analysis/mig_java8_17/<agent_model>/poc \
    --variant poc \
    --stages diff,compile,tests \
    --sandbox-provider docker \
    --instance-ids-file ids.txt

# 2. Generate the markdown report from the result.json files.
python -m migration_evals.cli report \
    --run runs/analysis/mig_java8_17/<agent_model>/poc \
    --out docs/poc_tier1_report.md

# 3. (Optional) per-iteration view if iterator_id is populated.
python -m migration_evals.cli iterator-report \
    --run runs/analysis/mig_java8_17/<agent_model>/poc \
    --out docs/poc_tier1_iterator.md
```

Tier 3 (LLM judge) is intentionally not wired into the driver for the
POC; the agent-pipeline integration bead can pick that up after tier 1
publishes a survival number. See
[`docs/oracle_funnel.md` §Anthropic backends](./oracle_funnel.md#anthropic-backends-config-driven)
when wiring it in.

## Smoke evidence the wiring works today

The driver is exercised end-to-end without Docker or external networks
in `tests/test_run_eval.py`. The smoke seeds a local `file://` git
remote, stages two changesets (one with a valid patch, one with a
broken patch), runs `run_eval.py --stages diff` (Tier-0 only), and
asserts that:

- both result.json files land under the configured `--output-root`,
- the valid patch produces `success=True`,
- the broken patch produces `success=False` with
  `failure_class=agent_error`.

The staged-but-unscored case (a missing instance id in the provider)
returns exit code `2` while still emitting a result.json for the
successfully-staged instances — that is the contract CI consumers
should rely on for partial-run handling.
