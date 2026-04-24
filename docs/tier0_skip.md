# Tier 0 skip decision

Tier 0 (retroactive scoring of historical merged changesets) needs a
list of agent-created PR URLs that have been merged and have aged at
least 30 days without a revert. The harvester
[`scripts/mine_gold_anchor.py --source changesets`](../scripts/mine_gold_anchor.py)
consumes that list and emits a calibrated success-rate number.

This repo currently ships
[`data/agent_changesets.csv`](../data/agent_changesets.csv) as a
**header-only stub**. Tier 0 is **skipped** until production history
exists. Real next work is tier 1 (live compile + tests against agent
diffs) and tier 2 (LLM-judge), which the framework supports today.

## What tier 0 needs from any agent-orchestration system

Tier 0 is intentionally agnostic to the orchestrator that produced the
changesets. To populate `data/agent_changesets.csv`, the orchestrator
needs to expose - by query, export, or any other mechanism - a list of
PR URLs satisfying:

1. The PR was created by an agent-driven changeset workflow (not a
   human-authored PR).
2. The PR is on a public-or-private host the harvester's `gh` CLI can
   reach for survival classification.
3. `external_state IN ('MERGED', 'CLOSED')`.
4. The merge or close happened at least 30 days ago.

The list lands as one PR URL per line in `data/agent_changesets.csv`.
A `pr_url` header is accepted; lines beginning with `#` are treated as
comments. See `load_changeset_urls` in `scripts/mine_gold_anchor.py`
for the exact parser contract.

## Why skipped today

Tier 0 is a calibration step, not a build step. It only produces a
useful number when the orchestrator under evaluation has been shipping
agent-created changesets long enough for them to merge and survive 30
days. For a green-field integration, that condition does not hold;
running tier 0 against a tiny or fresh corpus will produce a noisy or
empty number that is worse than no number.

## Three conditions that re-open tier 0

Re-open tier 0 when **all three** hold:

1. The orchestrator has shipped at least 50 agent-created PRs that are
   merged and have aged 30 days without a revert.
2. There is a stable filter for "this PR came from an agent migration
   workflow" (a workflow name, a tag, a metadata key, etc.) that an
   exporter can rely on without inspecting opaque definitions.
3. Someone with read access to the orchestrator's data populates
   `data/agent_changesets.csv` from that filter.

At that point, run:

```bash
python scripts/mine_gold_anchor.py \
    --source changesets \
    --changesets data/agent_changesets.csv \
    --target-count 50 \
    --out data/gold_anchor.json
```

and proceed to publish the survival number per
[`docs/gold_anchor.md`](./gold_anchor.md).

## Handoff to tier 1

Tier 1 does not depend on tier 0. With the
[`DockerSandboxAdapter`](../src/migration_evals/adapters_docker.py)
selected via `adapters.sandbox_provider: docker` and a directory of
repo + `patch.diff` pairs, the funnel scores agent diffs end-to-end
against a real container. See
[`docs/oracle_funnel.md` §Sandbox backends](./oracle_funnel.md#sandbox-backends-config-driven)
for the config and
[`docs/oracle_funnel.md` §Anthropic backends](./oracle_funnel.md#anthropic-backends-config-driven)
for the LLM-judge side.
