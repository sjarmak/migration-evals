# Architecture diagram (LikeC4)

Architecture-as-code model of `migration-evals`, rendered with
[LikeC4](https://likec4.dev). The model is the source of truth across
[`spec.c4`](spec.c4) (element kinds, tags, deployment node kinds),
[`model.c4`](model.c4) (the system), and [`views.c4`](views.c4) (structure,
walkthrough, and risk views), with the deployment model in
[`deployment.c4`](deployment.c4). The narrative companions are the repo-root
[`README.md`](../README.md), the risk-annotated [`docs/PRD.md`](../docs/PRD.md),
and the [`docs/premortem.md`](../docs/premortem.md).

Every element `link`s to its source (`src/migration_evals/…`, `configs/…`,
`schemas/…`) and, where one exists, to the relevant design doc, PRD section, or
ADR — so any box in the explorer is one click from the code and the rationale
behind it.

## Delivery state is tagged, not guessed

Every element carries a tag so **planned and research work renders distinctly
from what is already built** (legend in `spec.c4`):

| Tag | Meaning | Render |
|---|---|---|
| `#built` | code path exists and is exercised (replay-tested end to end) | solid |
| `#evolving` | built, but the science/contract is still moving | solid |
| `#planned` | designed; not yet implemented (or v1 is a stub/heuristic) | **dashed, dimmed** |
| `#research` | speculative Nice-to-Have v2 track | **dashed, indigo** |

What ships today is the **MVP scaffolding (M1–M9)**: the tiered oracle funnel,
LLM-inferred harness synthesis + content-hash cache, the synthetic Java 8
generator + AST oracle, the 4-way failure-class discriminator, the regression
ledger, gold-anchor correlation, the contamination split, and the
pre-registration / publication gate — all exercised offline via replay
cassettes. Tagged `#planned`/`#research`: the tier-4 Daikon stub, the
process-telemetry classifier (S1), IRT difficulty calibration (S2), live
dataset refresh (S3), and the held-out transferability / chaos / author-in-loop
v2 tracks (N2–N5). The harness drift detector, the gold anchor, and the
calibration stack are `#evolving` — built, but with contracts still moving.

## Views

**Structure** — the static map:

| View | Scope |
|---|---|
| `index` | system landscape — `migration-evals` in context of Docker, the LLM endpoints, GitHub, and the migration agent |
| `evalsSystem` | the `migration-evals` system decomposed into containers (built vs planned) |
| `funnelContainer` | the tiered oracle funnel (M1) — T0 diff → T1 compile → T2 tests → T2b AST → T3 judge → T4 daikon + quality oracles |
| `intakeContainer` | task definition & intake — run/recipe configs, oracle spec, changeset provider |
| `harnessContainer` | LLM-inferred build-harness synthesis (M2) — synth, recipe, content-hash cache, drift detector |
| `adaptersContainer` | execution & inference adapters — Docker sandbox + egress, Anthropic / Claude Code / OpenAI, cassette replay |
| `syntheticContainer` | synthetic task generation (M3 / M9) — Java 8 generator, Python 2 generator, schema-break probe |
| `runnerContainer` | runner, stamping & gates — run loop, failure-class, pre-reg SHA stamping, publication gate, result/ledger stores |
| `reportingContainer` | reporting & gold anchor — funnel report, iterator report, stats, contamination split, merge-survival correlation |
| `planned` | planned + research work, with built dependencies dimmed |
| `deployment` | where each piece runs — process & data boundaries (Python process + runs/ artifacts, Docker host, LLM endpoints, GitHub) |

**Walkthrough flows** (dynamic / numbered-step views) — the narrative spine for
a design-review walkthrough:

| View | Flow |
|---|---|
| `evalRun` | one eval run, config to stamped result (the offline smoke path: cassette adapters, no API keys, no Docker) |
| `funnelCascade` | the funnel cascade against live adapters (cheapest-first, short-circuit, failure-class stamping) |
| `syntheticFlow` | synthetic generation + AST-ground-truth scoring (the D5 generator↔oracle disjointness) |
| `reportFlow` | aggregation → contamination split → gold-anchor correlation → publication gate |

**Risk lens:**

| View | Scope |
|---|---|
| `risks` | the `#risk`-flagged elements with each open question stated in-box (harness drift, AST-oracle shallowness, schema-break gate, failure-class heuristic precision, procedural publication gate, contamination on small n, fragile load-bearing gold anchor) |

### Running the walkthrough

For a design review, present in this order: `index` → `evalsSystem` (orient on
structure) → the four walkthrough flows in sequence (what actually happens) →
`deployment` (where it runs) → `risks` (what to probe) → `planned` (what's next).
In `npx likec4 start`, the dynamic views animate step-by-step and each view's
notes panel carries the gotchas (the no-keys/no-Docker smoke path, the
short-circuit cost math, the D5 disjointness guarantee, the eval_broken gate).

## Viewing & regenerating

```bash
# Interactive, hot-reloading explorer (recommended)
npx likec4 start architecture

# Re-export the static PNGs in exports/ (needs a one-time browser download:
#   npx playwright install chromium-headless-shell)
npx likec4 export png architecture -o architecture/exports

# Validate the model (strict — the source of truth for correctness)
npx likec4 validate architecture
```

### Viewing the interactive explorer over SSH (headless remote)

`likec4 start` serves a Vite dev server on `localhost:5173`. From a headless
remote, forward that port to your laptop and open it locally — three options,
easiest first:

1. **VS Code / Cursor Remote-SSH** — run `npx likec4 start architecture` in the
   integrated terminal; the editor auto-forwards 5173 and offers "Open in
   Browser". Nothing else to configure.
2. **SSH local port-forward** — on your laptop:
   ```bash
   ssh -N -L 5173:localhost:5173 user@remote   # leave running
   ```
   then on the remote `npx likec4 start architecture` and open
   <http://localhost:5173> locally. (Already in an SSH session? Add the tunnel
   without reconnecting: press `~C` then type `-L 5173:localhost:5173`.)
3. **Bind + reach directly** — `npx likec4 start architecture --listen 0.0.0.0`
   and browse to `http://<remote-ip>:5173` (only if that port is reachable /
   firewall-open; the tunnel in option 2 is safer).
