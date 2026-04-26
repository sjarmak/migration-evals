# Integration guide — plug your agent pipeline into the funnel

Linear walkthrough from a fresh clone to a first stamped, gate-clean
funnel report on **your own** agent-produced changesets. Each step has
one concrete command and a *see also* link to the deep-dive doc.

This guide assumes your agent pipeline produces unified-diff artifacts
("changesets") for a single mechanical migration — the shape this
framework was designed for. If you are not sure which migration to
calibrate against first, read
[docs/calibration_starters.md](calibration_starters.md) before
continuing.

---

## 0. Prerequisites

- Python 3.11 or 3.12.
- `git` on PATH.
- A workstation with `claude` CLI logged in **only if** you intend to
  run tier 3 (LLM judge). Tiers 0–2 require neither network nor an
  API key.
- For tier 1+ on a real migration corpus: Docker on PATH (tier-1
  builds run in a sandbox container; see
  [docs/oracle_funnel.md §Sandbox backends](oracle_funnel.md#sandbox-backends-config-driven)).

---

## 1. Smoke the framework against committed fixtures

Confirm the framework runs end-to-end on your machine before plugging
in your own data.

```bash
git clone https://github.com/sjarmak/migration-evals.git
cd migration-evals
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

pytest -q                     # full test suite, no API keys, no network
python -m migration_evals.cli run --config configs/java8_17_smoke.yaml
```

The smoke completes in under two minutes against three Java fixture
repos using replay cassettes. If this step fails, the rest of the guide
will not work — fix the install before proceeding.

*See also:* [README.md §Quickstart](../README.md#quickstart).

---

## 2. Pick (or author) a recipe

A **recipe** is a per-migration template: dockerfile, build command,
test command, and the SHA-stamped spec mapping. Three ship in-tree:

| recipe | best for | canonical stages |
| --- | --- | --- |
| [`go_import_rewrite`](../configs/recipes/go_import_rewrite.yaml) | Go import-path rewrites (the highest-signal-per-effort starter) | `diff,compile,tests` |
| [`dockerfile_base_image_bump`](../configs/recipes/dockerfile_base_image_bump.yaml) | Dockerfile `FROM` rewrites | `diff,compile` (tier 2 skipped — per-target test commands vary) |
| [`java8_17`](../configs/recipes/java8_17.yaml) | Java 8 → 17 Maven migration | `diff,compile,tests` |

If your migration shape matches one of these, you are done with
step 2. If not, copy the closest recipe and edit
`recipe.dockerfile`, `recipe.build_cmd`, `recipe.test_cmd`. The
`stamps:` block must point at three distinct files (oracle_spec,
recipe_spec, hypotheses) so per-trial SHAs distinguish what changed.

*See also:*
[docs/calibration_starters.md](calibration_starters.md) for which
recipe to start with;
[docs/oracle_funnel.md §Recipes and canonical stages](oracle_funnel.md).

---

## 3. Look at a canonical worked example

Two committed examples mirror the most-cited public batch-changes
shapes:

- [`tests/fixtures/changeset_examples/go_import_rewrite/ghodss_to_sigs/`](../tests/fixtures/changeset_examples/go_import_rewrite/ghodss_to_sigs/) —
  `github.com/ghodss/yaml` → `sigs.k8s.io/yaml` rewrite. Designed to
  pass every tier when run on a workstation with Go on PATH.
- [`tests/fixtures/changeset_examples/dockerfile_base_image_bump/alpine_to_debian/`](../tests/fixtures/changeset_examples/dockerfile_base_image_bump/alpine_to_debian/) —
  `FROM alpine:3.18` → `FROM debian:bookworm-slim`. Designed to **fail
  at tier 1** (the new image lacks `apk`), demonstrating what the
  funnel catches.

Each example carries: a `repo_state/` (pre-patch project state), a
`patch.diff` (the agent's output), a `meta.json` (the
`ChangesetProvider` envelope), and a `README.md` explaining what the
funnel catches at each tier.

`tests/test_run_eval.py::test_canonical_go_import_rewrite_passes_tier0`
and `test_canonical_dockerfile_bump_passes_tier0` exercise these
examples through the driver tier-0; they are the working reference for
your own corpus layout.

---

## 4. Stage your corpus

Pick the `ChangesetProvider` that matches where your artifacts live.

### 4a. Filesystem provider (recommended starter)

Stage your changesets to disk in this layout:

```
<root>/<instance_id>/meta.json    # required keys: repo_url, commit_sha,
                                  #   workflow_id, agent_runner, agent_model
<root>/<instance_id>/patch.diff   # raw unified diff produced by the agent
```

`<instance_id>` must match `^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$` (anti-
traversal). `commit_sha` must be a 40-char lowercase hex SHA-1.

### 4b. HTTP provider (reference)

If your artifacts are already served over HTTP:

```bash
# Artifacts at https://artifacts.example.com/<id>/{meta.json,patch.diff}
python scripts/run_eval.py \
    --migration go_import_rewrite \
    --provider http \
    --base-url https://artifacts.example.com \
    --http-header "Authorization: Bearer ${ARTIFACT_TOKEN}" \
    --http-header "X-Trace: my-eval-run" \
    --http-timeout-s 30 \
    --http-max-bytes 67108864 \
    --eval-root /tmp/eval \
    --output-root runs/analysis/mig_go_import_rewrite/<your-runner>/poc \
    --variant poc --stages diff \
    inst-1 inst-2 inst-3
```

Hardening built into the reference provider: cross-origin redirects
are refused (would otherwise leak `--http-header` values to the
redirect target), and per-response reads are capped at `--http-max-bytes`
(default 64 MiB). For authenticated stores beyond static bearer
tokens, fork `HTTPChangesetProvider` and override `_get_text` to plug
in a session library (`requests`, `httpx`).

### 4c. Custom provider (S3, blob storage, GraphQL, ...)

Implement the `ChangesetProvider` Protocol and register a factory
**before** the driver runs:

```python
from migration_evals.changesets import (
    ChangesetProvider, Changeset, register_provider,
    validate_instance_id, validate_commit_sha,
)

class S3ChangesetProvider:
    def __init__(self, bucket: str, prefix: str = "") -> None:
        self._bucket = bucket
        self._prefix = prefix

    def fetch(self, instance_id: str) -> Changeset:
        validate_instance_id(instance_id)
        # ... your code ...
        return Changeset(...)

def _factory(config):
    return S3ChangesetProvider(config["bucket"], config.get("prefix", ""))

register_provider("s3", _factory)
```

Once registered, `--provider s3` works in `scripts/run_eval.py`.

*See also:*
[docs/oracle_funnel.md §Changeset providers](oracle_funnel.md#changeset-providers-pulling-agent-diffs-into-the-funnel);
[`src/migration_evals/changesets.py`](../src/migration_evals/changesets.py)
for the Protocol contract and the two reference implementations.

---

## 5. Run the driver

```bash
python scripts/run_eval.py \
    --migration go_import_rewrite \
    --provider filesystem --root /path/to/staged \
    --eval-root /tmp/eval \
    --output-root runs/analysis/mig_go_import_rewrite/<your-runner>/poc \
    --variant poc \
    --stages diff,compile,tests \
    --sandbox-provider docker \
    --instance-ids-file ids.txt
```

The driver writes one `result.json` per trial under `--output-root` and
emits a `manifest.json` in the same dir. The manifest binds each
`result.json` to the SHAs of the committed spec files in force at
scoring time.

For an offline first-pass run, drop `--sandbox-provider docker` and use
`--stages diff` (tier 0 only).

*See also:* [`scripts/run_eval.py`](../scripts/run_eval.py) docstring
for full CLI surface.

---

## 6. Generate a markdown report

```bash
python -m migration_evals.cli report \
    --run runs/analysis/mig_go_import_rewrite/<your-runner>/poc \
    --out /tmp/poc_report.md
```

The report has a fixed structure: funnel table (per-tier pass rates),
contamination split (pre/post the model's training cutoff), gold-
anchor correlation (optional, requires `--gold`), spec stamps, and a
failure-class breakdown.

*See also:* [docs/usage.md §`report`](usage.md#report).

---

## 7. Confirm gate-clean output

```bash
python -m migration_evals.publication_gate \
    --check-run runs/analysis/mig_go_import_rewrite/<your-runner>/poc
```

Exit 0 = every `result.json` carries a non-empty `oracle_spec_sha`,
`recipe_spec_sha`, and `pre_reg_sha` matching the committed spec
files referenced by `manifest.json`. The driver emits manifests and
stamps result.json files automatically; this step exists so a CI gate
can prove a committed run output has not drifted from the spec.

The repo ships
[`.github/workflows/publication_gate.yml`](../.github/workflows/publication_gate.yml)
which runs this check on any PR that touches `runs/analysis/mig_*/`.

*See also:* [docs/publication_gate.md](publication_gate.md) for the
manifest contract, the operator playbook for stale-stamp failures, and
the (optional, governance-action) CODEOWNERS pattern for the
pre-registered hypotheses file.

---

## 8. Publication readiness

A funnel-rate number only carries signal when the corpus is large
enough to push binomial sampling error below the size of the
differences the report is supposed to surface. Below ~10 real
instances the per-tier rates move by tens of percentage points.

The repo ships **no published headline numbers** until two gates open:

| gate | conditions | doc |
| --- | --- | --- |
| Tier 0 (retrospective merge-survival) | Orchestrator has shipped ≥50 merged agent PRs aged 30 days; stable workflow filter; someone with read access populates `data/agent_changesets.csv`. | [docs/tier0_skip.md](tier0_skip.md) |
| Tier 1 (per-changeset compile + tests) | Orchestrator has shipped ≥10 instances on a target migration; staged into the filesystem-provider layout. | [docs/tier1_skip.md](tier1_skip.md) |

Run the framework end-to-end before either gate opens — small-N runs
are useful for local calibration and harness validation. Publishing a
**headline rate** is the gated step.

---

## 9. Tier 3 (LLM judge), optional

Tier 3 calls an LLM judge against a per-recipe rubric. Three providers
ship behind a single Protocol:

| provider | when to use | API key |
| --- | --- | --- |
| `cassette` (default in tests) | offline replay against committed JSONL | none |
| `claude_code` (default for live runs) | workstation with `claude` CLI logged in (OAuth) | none |
| `sdk` | headless CI runners | `ANTHROPIC_API_KEY` env var |

Set `adapters.anthropic_provider: claude_code` in your run config to
enable tier 3.

*See also:*
[docs/oracle_funnel.md §Anthropic backends](oracle_funnel.md#anthropic-backends-config-driven).

---

## 10. Wiring into your pipeline as a CI feedback loop

After tier 0–2 calibration is meaningful (≥10 instances, gates met),
the next step is embedding the funnel as a workflow node so the
agent's next iteration sees the verdict and can adapt.

This work lives in your pipeline's repo, not here. The contract is
documented in
[docs/oracle_funnel.md §CI feedback loop integration](oracle_funnel.md#ci-feedback-loop-integration);
the eval node reads the agent's `result.diff` from artifact storage,
invokes the funnel via subprocess (or RPC), and writes
`FunnelResult.to_dict()` back as a workflow variable named
`last_oracle_verdict` for the next iteration's prompt context to read.

---

## Reference: deep-dive docs

| Doc | Subject |
| --- | --- |
| [docs/oracle_funnel.md](oracle_funnel.md) | Tier definitions, cost math, sandbox/anthropic/changeset backend selection, CI feedback loop. |
| [docs/calibration_starters.md](calibration_starters.md) | Which recipe to calibrate against first, ranked by signal-per-effort. |
| [docs/publication_gate.md](publication_gate.md) | Manifest contract, GH workflow, stale-stamp playbook. |
| [docs/tier0_skip.md](tier0_skip.md) | Conditions to publish a tier 0 (retrospective survival) number. |
| [docs/tier1_skip.md](tier1_skip.md) | Conditions to publish a tier 1 (per-changeset compile + tests) number. |
| [docs/hypotheses_and_thresholds.md](hypotheses_and_thresholds.md) | Pre-registered hypotheses (v1). New hypotheses go in a `_v2.md` file, never as in-place edits. |
| [docs/usage.md](usage.md) | CLI subcommand reference. |
