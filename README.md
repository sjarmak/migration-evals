# migration-evals

A **tiered-oracle funnel** for evaluating automated code migrations end to
end — defending claims like *"this migration works on 85% of in-scope
repos"* with a published funnel, contamination split, and pre-registered
spec stamps instead of a single hand-wavy success rate.

The framework is deliberately **modular and ecosystem-pluggable**. The v1
implementation targets Java 8→17 (Maven) and ships with a working Python
2→3 falsification probe; the design generalizes to JS/TS, pinned-dep
bumps, Spring Boot upgrades, and CVE fan-out without schema changes.

The whole pipeline is **automated** — no reviewer-day step, no
human-in-the-loop labeling. The gold-anchor ground-truth set is harvested
from public OSS migration PRs that were merged *and* survived ≥30 days
without a revert; the result is a calibration signal that costs API
quota, not engineering time.

---

## What's in here

| Path | Purpose |
| --- | --- |
| [`docs/PRD.md`](docs/PRD.md) | Risk-annotated v0.3 PRD — goals, non-goals, MVP/M1–M9, Should/Nice tiers, metrics, capacity plan. |
| [`docs/premortem.md`](docs/premortem.md) | Top-15 failure modes (R1–R15) — reviewer-disagreement, contamination, harness-synth, ecosystem generalization, infra blast-radius. Drives the M-list. |
| [`docs/README.md`](docs/README.md) | Per-component design notes (oracle funnel, harness synth, gold-anchor, publication gate, python23 probe). |
| [`docs/usage.md`](docs/usage.md) | CLI quickstart for `run`/`report`/`iterator-report`/`regression`/`harness`/`probe`. |
| [`src/migration_evals/`](src/migration_evals/) | Python package — CLI, funnel (Tier 0–4), oracles, gold anchor, iterator-batch report, ledger, contamination split, pre-registration / publication gate, Python 2→3 probe. |
| [`schemas/`](schemas/) | JSON Schemas for `result.json` and gold-anchor entries. |
| [`configs/java8_17_smoke.yaml`](configs/java8_17_smoke.yaml) | End-to-end smoke config: 3 fixture repos, all non-network tiers, replay cassettes — no API keys required. |
| [`scripts/mine_gold_anchor.py`](scripts/mine_gold_anchor.py) | Automated gold-anchor harvester — builds `data/gold_anchor.json` from merged-PR survival via the `gh` CLI. Two sources: OSS search (default) or a CSV of agent-generated changeset URLs. |
| [`tests/`](tests/) | 229 pytest cases: schema validation, funnel cascade, AST oracle, gold-anchor correlation + bootstrap CI, ledger diff, contamination split, publication gate, iterator-batch report, mine_gold_anchor (both sources), Tier-0 diff validity. |
| [`examples/runs/`](examples/runs/) | Committed example outputs from the smoke config and the Python 2→3 probe. |
| [`data/gold_anchor_template.json`](data/gold_anchor_template.json) | Empty seed — populated by `scripts/mine_gold_anchor.py`. |

---

## Quickstart

```bash
git clone https://github.com/sjarmak/migration-evals.git
cd migration-evals

# Editable install with dev tooling (pytest, ruff, black, mypy)
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# 1. Run the full test suite (no API keys required — all tiers replay from cassettes)
pytest -q

# 2. Run the smoke eval end-to-end against 3 fixture repos
python -m migration_evals.cli run --config configs/java8_17_smoke.yaml

# 3. Aggregate the results into a funnel + contamination + spec-stamp report
python -m migration_evals.cli report \
    --run runs/analysis/mig_java8_17/claude-sonnet-4-6/smoke \
    --out /tmp/smoke_report.md

# 4. (Optional) Harvest a gold anchor from public Java 8→17 OSS migration PRs.
#    Requires: `gh auth login` already done. Writes data/gold_anchor.json.
python scripts/mine_gold_anchor.py \
    --migration java8_17 \
    --target-count 50 \
    --out data/gold_anchor.json

# 5. (Optional) Once your agent has been shipping real PRs, classify those
#    directly by merge-survival instead of arbitrary OSS PRs. The CSV is
#    one PR URL per line (or `pr_url,...` rows).
python scripts/mine_gold_anchor.py \
    --source changesets \
    --changesets data/agent_changesets.csv \
    --out data/gold_anchor_agent.json

# 6. (Optional) Aggregate trials by iterator_id into a per-batch report
#    with completion rate, p50/p95 latency, total cost, failure-class
#    breakdown — the natural unit when a single fan-out workflow runs
#    across hundreds-to-thousands of repos.
python -m migration_evals.cli iterator-report \
    --run runs/analysis/mig_java8_17/claude-sonnet-4-6/smoke \
    --out /tmp/iterator_report.md
```

After step 2: `runs/analysis/mig_java8_17/claude-sonnet-4-6/smoke/` contains
a `summary.json` plus one `result.json` per repo, each validating against
`schemas/mig_result.schema.json` and carrying `oracle_spec_sha`,
`recipe_spec_sha`, `pre_reg_sha` stamps. After step 4: a populated
`data/gold_anchor.json` validated against
`schemas/gold_anchor_entry.schema.json` ready to feed into
`migration_evals.cli report --gold ...`.

---

## Architectural shape

```
┌─────────────────────────────────────────────────────────────────┐
│                python -m migration_evals.cli run                 │
└─────────────┬───────────────────────────────────────────────────┘
              │
    ┌─────────▼──────────┐       ┌──────────────────────┐
    │  Repo Acquisition  │       │   Synthetic Gen      │
    │  - OSS mining      │       │   - AST-ground-truth │
    │  - Internal repos  │       │   - OpenRewrite spec │
    │  - Frozen gold set │       └──────────┬───────────┘
    └─────────┬──────────┘                  │
              │                             │
    ┌─────────▼─────────────────────────────▼──────────────┐
    │   LLM-Inferred Build Harness (cached by content-hash)│
    │   → Dockerfile + build/test recipe + provenance      │
    └─────────┬────────────────────────────────────────────┘
              │
    ┌─────────▼────────────────────────────────────────────┐
    │              Tiered Oracle Funnel                    │
    │  T0: diff validity              (~$0.001/repo, local)│
    │  T1: compile + typecheck        (~$0.01/repo)        │
    │  T2: existing tests             (~$0.03/repo)        │
    │  T2b: AST-spec conformance      (synthetic only)     │
    │  T3: LLM-judge (cached prompt)  (~$0.08/repo)        │
    │  T4: Daikon invariants (opt)    (~$0.10/repo)        │
    └─────────┬────────────────────────────────────────────┘
              │
    ┌─────────▼────────────────────────────────────────────┐
    │  Result Writer                                       │
    │  - runs/.../<repo>_<seed>/result.json                │
    │  - runs/.../_ledger/<task_id>/  (regressions)        │
    │  - process_telemetry.json + calibration.json         │
    │  - failure_class: {agent|harness|oracle|infra}_error │
    └─────────┬────────────────────────────────────────────┘
              │
    ┌─────────▼────────────────────────────────────────────┐
    │  Reporting (score-pre-cutoff, score-post-cutoff) +   │
    │  gold_anchor_correlation + spec stamps + 95% CI      │
    └──────────────────────────────────────────────────────┘
```

Seven non-negotiable design properties:

1. **Funnel, not a single number.** Every report breaks results into
   `diff / compile / tests / ast / judge / daikon` so failures localize
   without re-running the cascade.
2. **Synthetic + automated real-world anchor (not either).** Procedurally
   generated repos give exact AST ground-truth at zero marginal cost. The
   merge-survival anchor harvested from public OSS PRs catches the
   10–20% of cases where perfect-oracle migrations still get rejected
   in real life. Both pipelines are scriptable; neither needs a reviewer.
3. **Discriminated failure modes.** Every failed trial carries exactly one
   of `agent_error / harness_error / oracle_error / infra_error` so a small
   on-call rotation can triage 40-repo regressions in <2h.
4. **Contamination is first-class.** Every report carries a `score_pre_cutoff`
   / `score_post_cutoff` split based on repo creation date relative to the
   model cutoff; gaps >5pp auto-flag a `contamination_warning`.
5. **Pre-registration gates publication.** No headline number leaves the
   project until `oracle_spec_sha`, `recipe_spec_sha`, and `pre_reg_sha`
   match committed files. When the migration is *prompt-defined* rather
   than recipe-defined, the agent prompt itself is hashed as a 4th stamp
   (`prompt_sha`) so the prompt is auditable end-to-end
   (`python -m migration_evals.publication_gate --check-run`).
6. **Runner is separate from model.** The schema carries `agent_runner`
   (e.g. `claude_code`, `amp`, `cursor`, `aider`) alongside `agent_model`
   (e.g. `claude-sonnet-4-6`) so cost / reliability metrics can be sliced
   by harness independently of the underlying LLM.
7. **CI feedback loop is a documented integration point.** When the
   funnel is invoked inside a multi-iteration agent workflow, every
   `FunnelResult` can be written back as a structured workflow variable
   so the agent's next iteration sees the prior verdict and adapts. See
   `docs/oracle_funnel.md` §CI feedback loop integration.

---

## What's implemented vs. scaffolded

This codebase lands the **MVP scaffolding (M1–M9)** specified in the PRD.
Modules are pure Python, schema-validated, and exercised by 229 tests via
cassette-based replay so the suite needs no API keys, no sandbox container,
and no Maven install.

**Production-ready (replay-tested end to end):**

- Tiered oracle funnel (`migration_evals.funnel`) — cascade T0 → T1 → T2 →
  T2b → T3 → T4 with stage-level reporting and short-circuit on first
  failure. T0 (`oracles.tier0_diff`) catches malformed patches before
  paying for a sandbox.
- LLM-inferred build harness synthesis + content-hash cache + drift detector
  (`migration_evals.harness.{synth,cache,drift,recipe}`).
- Procedural Java 8 repo generator across 10 migration primitives + AST-
  conformance oracle (`migration_evals.synthetic.*`). D5-disjoint by design:
  generator and oracle never share code paths.
- Failure-class discriminator with deterministic precedence
  (`migration_evals.failure_class`).
- Regression ledger with content-hash dedup
  (`migration_evals.ledger` + `cli regression`).
- Gold-anchor correlation with bootstrap 95% CI + `eval_broken` gate
  (`migration_evals.gold_anchor`).
- Automated gold-anchor harvester via merged-PR survival
  (`scripts/mine_gold_anchor.py`) — two sources: OSS search (default) or a
  CSV of agent-generated changeset URLs. Replaces the original "schedule
  reviewer days" step entirely.
- Iterator-batch report (`migration_evals.iterator_report`) — groups
  trials by `iterator_id` and emits per-batch completion rate, p50/p95
  latency, total cost, and failure-class breakdown. CLI:
  `iterator-report --run <dir> --out <md>`.
- Contamination split (`migration_evals.contamination`).
- Pre-registration / publication gate (3 required stamps + optional
  `prompt_sha` for prompt-defined migrations) — `migration_evals.pre_reg`
  + `migration_evals.publication_gate`.
- Python 2→3 falsification probe with synthetic generator + findings JSON
  (`migration_evals.python23_probe`).
- End-to-end CLI runner and funnel report
  (`migration_evals.{runner,report,cli}`).

**Scaffolded as Protocols (need vendor adapters wired in):**

- `migration_evals.adapters` defines `AnthropicAdapter`, `SandboxAdapter`,
  `OpenRewriteAdapter`, `CodeSearchAdapter`, `GitHubAdapter`, `DockerAdapter`
  Protocols. The cassette-replay implementations in `cli.py` keep the funnel
  testable; production adapters that hit real Anthropic / sandbox / a code
  search backend / etc. are the next integration step.
- `oracles.tier4_daikon` is a stub that returns `daikon_skipped`; integrating
  the real Daikon binary is deferred to v2 (PRD N1).

**Not yet implemented (Should-Have v1.1 and Nice-to-Have v2 from the PRD):**

- Process-telemetry feature extraction + classifier (S1)
- IRT adaptive-difficulty calibration (S2)
- Time-windowed live dataset refresh (S3)
- Agent self-reported confidence + ECE calibration (S4)
- Held-out transferability / chaos-injection / merge-rate signal (N2/N3/N5)

See `docs/PRD.md` §Requirements for the full roadmap and acceptance criteria
per module.

---

## Recommended next steps

The highest-leverage path from MVP scaffold to a regular reporting cadence,
in order. Every step below is fully automatable — none of them require
recurring reviewer time.

1. **Wire production adapters.** Implement `AnthropicAdapter` against the
   real Claude SDK and `SandboxAdapter` against a sandbox SDK (replacing
   the cassette stand-ins in `migration_evals.cli`). All other modules are
   already Protocol-typed and need no changes.
2. **Mine the first 200 Java 8→17 OSS candidates.** Use the GitHub Search
   API (`gh search repos --language=java 'pom.xml'`) to assemble a
   `data/oss_candidates.json` catalog. Apply the harness-synthesis cache so
   repeat eval runs are content-hash deduplicated.
3. **Harvest the 50-repo gold anchor automatically.** Run
   `python scripts/mine_gold_anchor.py --migration java8_17 --target-count 50`.
   This pulls merged PRs that touched Java 8→17 idioms (lambda rewrites,
   `var` introductions, Optional chains, etc.), checks they survived
   ≥30 days without a revert, and writes `data/gold_anchor.json` validated
   against `schemas/gold_anchor_entry.schema.json`. Until this lands, the
   publication gate runs in `--require-gold-anchor=off` mode.
4. **Stand up the publication gate in CI.** Add a GitHub Action that runs
   `python -m migration_evals.publication_gate --check-run runs/analysis/mig_*`
   on every PR that touches a run directory. See `docs/publication_gate.md`
   for the contract.
5. **Run the Python 2→3 probe and revise schemas.** PRD §M9 gives the rule:
   if ≥2 of {M2 harness, M3 synthetic, M5 ledger} need schema revisions,
   freeze the Java schema *before* shipping any external Java number. The
   probe machinery is already wired; just run it on a real Python 2→3 repo
   set.
6. **Mine candidate repos via the GitHub Search API.** The
   `CodeSearchAdapter` Protocol abstracts away the backend — point it at
   `gh search code` for the OSS lane and at any internal code-search
   backend (OpenGrok, Hound, Zoekt, etc.) for an internal lane.
7. **Ship S1–S4** (process-telemetry classifier, IRT difficulty, monthly
   live-data rotation, ECE calibration) per the PRD Should-Have tier once
   the v1 funnel has been running for ~1 quarter.
8. **Open external publication.** Once gold-anchor ≥0.7 with CI bound ≥0.5,
   publication gate green, contamination split <5pp, and ≥2 migrations
   evaluated under the same schema — file an external technical report.

---

## Repo layout

```
migration-evals/
├── README.md                   # this file
├── LICENSE                     # Apache 2.0
├── pyproject.toml              # PEP 621 + setuptools src/ layout
├── docs/
│   ├── PRD.md                  # full risk-annotated PRD v0.3
│   ├── premortem.md            # top-15 failure modes (R1–R15)
│   ├── README.md               # per-component design overview
│   ├── usage.md                # CLI quickstart
│   ├── oracle_funnel.md
│   ├── harness_synthesis.md
│   ├── synthetic_generator.md
│   ├── gold_anchor.md
│   ├── failure_classification.md
│   ├── publication_gate.md
│   ├── hypotheses_and_thresholds.md
│   ├── python23_probe.md
│   └── python23_probe_findings.md
├── src/
│   └── migration_evals/
│       ├── cli.py              # run/report/regression/harness/probe
│       ├── runner.py           # config-driven run loop
│       ├── funnel.py           # tiered oracle cascade
│       ├── adapters.py         # external-dependency Protocols
│       ├── failure_class.py    # 4-way failure discriminator
│       ├── contamination.py    # pre/post-cutoff split
│       ├── gold_anchor.py      # merge-survival correlation + bootstrap CI
│       ├── ledger.py           # regression diff
│       ├── pre_reg.py          # spec-SHA stamping
│       ├── publication_gate.py # CI gate (importable + script)
│       ├── python23_probe.py   # Python 2→3 falsification probe
│       ├── report.py           # markdown funnel report
│       ├── types.py            # FailureClass, OracleTier enums
│       ├── harness/            # LLM-inferred build harness + cache + drift
│       ├── oracles/            # tier1_compile, tier2_tests, tier3_judge, tier4_daikon, verdict
│       ├── synthetic/          # Java/Python generators + AST oracle + 10 primitives
│       └── templates/          # report.md.j2
├── scripts/
│   └── mine_gold_anchor.py     # automated gold-anchor harvester
├── tests/                      # 174 pytest cases + fixtures (cassettes)
├── schemas/
│   ├── mig_result.schema.json
│   └── gold_anchor_entry.schema.json
├── configs/
│   └── java8_17_smoke.yaml
├── data/
│   ├── README.md
│   └── gold_anchor_template.json
└── examples/runs/              # committed smoke output for inspection
    ├── mig_java8_17/
    └── python23_probe/
```

---

## Key design decisions, in one place

These are the choices most worth questioning before committing more
engineering effort. Each links to the section of the PRD that justifies it.

| Decision | Rationale | PRD § |
| --- | --- | --- |
| **Tiered funnel, not a single oracle** | 70% of failures caught at $0.01/repo compile tier; LLM-judge runs only on residual ambiguous cases. | M1 |
| **LLM-inferred harnesses, not hand-written Dockerfiles** | Harness authoring is the bottleneck in MigrationBench; LLM synthesis + content-hash cache amortizes the cost. | M2 |
| **Synthetic AST + automated merge-survival anchor** | Synthetic gives exact accuracy at zero marginal cost; merge-survival labels catch the 10–20% where perfect-oracle migrations still get reverted. Both lanes are scriptable. | M3 + M4-lite |
| **4-way failure classes (agent/harness/oracle/infra)** | Triage budget for a small on-call rotation is 2h per regression batch; no class = no triage. | M6 |
| **Pre/post-cutoff contamination split is mandatory** | Industry headline numbers (Amazon Q etc.) are widely suspected of contamination; we get ahead of the critique. | M7 |
| **Pre-registration via spec-SHA stamps** | Prevents post-hoc threshold-shifting; gates external publication. | M8-lite |
| **Python 2→3 probe before freezing schema** | Falsification check that the Java-derived schema generalizes; if it doesn't, fix it *before* shipping the first external Java number. | M9 |
| **Not a public leaderboard** | Eval set has ~12-month half-life; public leaderboard becomes a contamination vector for the next model generation. | Non-Goals |
| **Not a benchmark-as-product** | This is internal tooling; publication is a separate decision that requires re-thinking contamination. | Non-Goals |

---

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
