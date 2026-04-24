# PRD: Migration Eval Framework

**Status:** Risk-annotated draft (v0.3)
**Source:** `/diverge` (5 agents) → `/converge` (3-position debate) → `/premortem` (5 failure lenses) over 30 brainstormed ideas
**Companion doc:** `docs/premortem.md`

## Problem Statement

A project shipping an automated code-migration agent needs to defend claims like *"our Java 8→17 migration works 85% of the time"* across 1000s of repos, track regressions across agent/model versions, and generalize to Python, JS/TS, and multiple build ecosystems — within a small-rotation / months-not-years budget.

Existing prior art is insufficient: MigrationBench is Java-8-Maven-only with operational brittleness; SWE-bench is per-issue not per-migration; LLM-judge rubrics are noisy and correlate across hallucinations; PR-replay assumes the human PR is gold; production PR-batch tooling scales but only measures CI-green. The industry's headline numbers (e.g., Amazon Q's "$260M / 30K apps") are PR-count, not merged-and-green-at-T+30d — to be credible the project must publish funnel numbers.

The 30-idea design space converges on a clear architecture: a **tiered-oracle funnel** driven by **LLM-inferred build harnesses**, anchored by both **procedurally-generated synthetic repos with AST-level ground truth** and a **frozen merge-survival gold set for external validity**, with all results stored in a flat trial-directory layout so regression tracking and debugging are filesystem queries, not separate pipelines.

## Goals & Non-Goals

### Goals

1. Produce defensible, calibrated success-rate numbers for a named migration (v1: Java 8→17) at 95% CI, with pre-registered oracle spec.
2. Report results as a **funnel** (generated → compiles → tests pass → semantic-invariant preserved → merge-survivaled-on-gold-set), never as a single collapsed number.
3. Run 1000 repos per quarter end-to-end on existing production infrastructure (Daytona, GitHub API) for under ~$35K/quarter inference + ~$5K infra.
4. Make regression tracking a diff-query against a growing ledger, not a separate system.
5. Be ecosystem-pluggable: adding Python 2→3 or pinned-dep bumps should take ~2 weeks of recipe-spec authoring, not a rebuild.
6. Surface four discriminated failure modes per trial (`agent_error`, `harness_error`, `oracle_error`, `infra_error`) so 3-person team can triage without reading trajectories.
7. Establish contamination-resistance as a first-class reported metric (pre-cutoff vs. post-cutoff score split).

### Non-Goals

1. **Not a public leaderboard.** The eval set is treated as a secret with ~12-month half-life. No public diffs, no Twitter screenshots of sample migrations.
2. **Not measuring "idiomatic" as a primary metric.** Idiomaticity is a Tier-5 optional signal if we ship it at all; the headline is semantic preservation + compile + test.
3. **Not a replacement for human review.** The gold-set anchor acknowledges that 10-20% of perfect-oracle migrations still get rejected by real reviewers; we measure toward that ceiling, not past it.
4. **Not measuring agent performance on live production services** (#8 shadow canary is out of scope; too infra-heavy, too narrow).
5. **Not publishing a paper or benchmark-as-product.** This is internal tooling for the working group. Publication is a separate decision that requires re-thinking contamination.
6. **Not extending to 10,000 repos in v1.** Quarterly 1000-repo cadence is feasible at the stated budget; 10K requires further funnel compression and is deferred.
7. **Not trying to score cross-repo ripple effects (#19) or micro-ecosystems (#30).** Second-order; deferred beyond v1.

## Requirements

### Must-Have (MVP — target: 10 engineering weeks, revised from 8)

> **Convergence note (v0.2):** Original MVP was 8 weeks with M4/M7/M8 deferred. A 3-position debate (Pragmatist / Rigorist / Ecosystem-Hawk) converged on a 10-week MVP that (a) ships M4-lite (50-repo gold anchor, not 200), (b) ships M7 fully wired (contamination split is a reporting-layer add, cheap), (c) downgrades M8 to M8-lite (spec-SHA stamping + pre-committed hypothesis file, not full adjudication), and (d) adds a 1-week Python 2→3 **falsification probe** (M9) to stress-test the Java-derived recipe-spec before interfaces freeze on a public number. The Pragmatist's asymmetric-retrofit-cost framing, the Rigorist's uncheckability-of-EoE-criterion-3 point, and the Ecosystem-Hawk's generalization-failure-precedent (Poly-MigrationBench) all drove specific changes below.

**M1. Tiered oracle funnel (idea #22)** — Cascade of oracles ordered cheapest-first, with stage-level reporting.
- Tier 1: compile + typecheck (cost ~$0.01/repo, target ~70% of failures caught here)
- Tier 2: existing test suite runs green in Daytona sandbox (cost ~$0.03/repo, ~20% more caught)
- Tier 3: LLM-judge single-pass on residual ambiguous cases (cost ~$0.08-$0.25/repo with prompt caching)
- Tier 4 (optional, rate-limited): Daikon-style dynamic-invariant check or convex-hull rerun
- **Acceptance:** Running `csb mig run --stage=compile` on 10 repos returns in ≤ 3 minutes and emits `result.json` with `oracle_tier: compile_only` flag. Full stack on 1000 repos completes overnight for under $300 total inference at current Sonnet pricing with prompt caching enabled.

**M2. LLM-inferred build-harness recipes (idea #3)** — Haiku-class model reads manifest files + CI YAML + README, emits Dockerfile + build/test recipe, caches successful recipes as reusable artifacts keyed on repo-content-hash.
- **Acceptance:** On a held-out set of 50 random Java Maven repos from GitHub, recipe-synthesis succeeds on ≥ 60% at first try. Every emitted recipe is persisted to `runs/analysis/_harnesses/<content-hash>/recipe.json` with `harness_provenance` field identifying the model + prompt-version that generated it.

**M3. Procedural synthetic-repo generator + AST-spec conformance (ideas #13 + #18)** — Programmatically composed Java 8 repos with known-correct OpenRewrite-recipe ground truth.
- **Acceptance:** Generator produces ≥ 500 synthetic Java 8 repos covering ≥ 10 migration primitives (lambda rewrite, `var` inference, `Optional` chains, text blocks, records, sealed classes, pattern-matching, enhanced switches, deprecated-API swaps, dep-version bumps). AST-conformance oracle runs in under 2 seconds per repo and returns deterministic pass/fail with per-primitive scoring.

**M4-lite. Human-accept gold anchor — 50 repos in MVP, scale to 200 in Phase 2 (derived from failure-mode R1)** — Frozen set of real migrations where a human reviewer accepted or rejected the diff. Never used for tuning; used only to report correlation.
- **Acceptance:** Every full eval run emits `gold_anchor_correlation: float` with 95% CI (wide at N=50, ±~0.15) in the summary. Point estimate below 0.7 OR lower CI bound below 0.5 triggers an "eval broken" flag and blocks headline-number publication until re-anchored. Scaling to 200 repos in Phase 2 narrows the CI enough for tight pp-level claims.

**M5. Regression ledger stored in the flat trial-directory layout (idea #15)** — Every failed trial persisted with full provenance (agent version, model, prompt hash, harness recipe hash, seed, timestamp) under `runs/analysis/_ledger/<task_id>/`.
- **Acceptance:** `csb mig regression --from=v1 --to=v2` produces a `regressed_tasks.md` with one row per newly-failing repo, linking to trial dir and prior-pass provenance. Content-hash deduplication keeps ledger size sub-linear in run count.

**M6. Four-way failure-mode discrimination** — Every trial emits `result.json` with exactly one of `{agent_error, harness_error, oracle_error, infra_error}` as the top-level failure class when `success=false`.
- **Acceptance:** Spot-check of 50 failed trials shows ≥ 90% correct classification. Triage of a 40-repo regression takes ≤ 2 hours using only these classes and process-telemetry fields.

**M7. Contaminated-vs-clean split reporting (derived from prior-art + failure-mode R2)** — Every result reports `(score_pre_cutoff, score_post_cutoff)` split by repo-creation-date relative to model cutoff.
- **Acceptance:** Summary JSON always includes both numbers. Gap exceeding 5pp auto-emits a `contamination_warning` flag; numbers in that state are not valid for external claims.

**M8-lite. Lightweight pre-registration** — Before any numeric claim leaves the working-group Slack, every result stamps `oracle_spec_sha`, `recipe_spec_sha`, and references a pre-committed `hypotheses_and_thresholds.md` file (hypotheses declared BEFORE seeing results).
- **Acceptance:** Published numbers reference all three stamps. Pre-registration file is committed to git with timestamp before any run that produces a published number; post-hoc threshold changes require a new file and do not retroactively relabel. Full adjudication machinery (external witnesses, replication packages) deferred to Phase 2 when n≥2 migrations makes full pre-reg worth the overhead.

**M9. Python 2→3 falsification probe (1 week, new in v0.2 from debate)** — After Java pipeline stabilizes (~week 8), spend 1 week running the Java-derived recipe-spec + oracle-spec against ~20 synthetic Python 2→3 repos to find where the interface breaks. Output is a `python23_probe_findings.md` document enumerating schema inadequacies, NOT a credible Python eval number.
- **Acceptance:** Probe exercises at least three Python-idiosyncratic cases: (a) string/bytes semantic shifts, (b) `setup.py`/`pyproject.toml`/`poetry.lock` divergence from Java manifest shape, (c) 2to3 cases where semantic equivalence requires runtime not static checks. Findings document lists each schema field that fails to generalize with proposed fix. If ≥2 of {M2, M3, M5} require schema revision, the working group commits to revising BEFORE the first external Java number ships.

### Should-Have (v1.1 — target: +1 month after MVP)

**S1. Process-telemetry artifacts (idea #20)** — Per-trial capture of files opened, re-read counts, tool calls, tokens spent, retry attempts in `process_telemetry.json`.
- **Acceptance:** A trained classifier on telemetry features predicts trial outcome (success/fail) with ≥ 75% accuracy on held-out repos, enabling cheap pre-flight confidence scoring.

**S2. IRT adaptive-difficulty calibration (idea #9)** — Each task gets a difficulty score; each agent-run updates both task difficulty and agent ability via item-response-theory model.
- **Acceptance:** Report produces statements of the form "agent-X passes difficulty-k tasks at p%" where difficulty bands are stable across task-set changes (Spearman ρ > 0.8 on held-out task additions).

**S3. Time-windowed live dataset refresh** — Monthly rotation of a held-out slice drawn from repos created after the evaluated model's training cutoff.
- **Acceptance:** First of each month, ~20 new post-cutoff repos enter the clean split; the clean split never contains repos whose last-commit date precedes the model cutoff.

**S4. Agent self-reported confidence + calibration (idea #29)** — Agent emits predicted probability-of-success; calibration reported alongside accuracy.
- **Acceptance:** ECE (expected calibration error) is reported on every full run. Calibration failures (ECE > 0.15) are surfaced as a `calibration_warning` flag.

### Nice-to-Have (v2+, deferred)

**N1. Daikon dynamic-invariant preservation (idea #4)** — Tier-4 oracle for real repos where AST-conformance is inapplicable. Deferred due to per-repo setup cost.

**N2. Held-out transferability measure (idea #23)** — Explicit train/test split across repos to detect overfitting.

**N3. Chaos-injection robustness curves (idea #17)** — Noise-injection to measure graceful degradation.

**N4. Primitive-task-genome scoring (idea #6)** — Fine-grained per-primitive accuracy breakdown. Subsumed partially by M3's per-primitive AST scoring.

**N5. Author-in-the-loop merge-rate (idea #2)** — Actual OSS PR submission. Strong signal but high variance, high latency, and unclear deployment policy.

## Design Considerations

### Key tensions and how we resolved them

1. **Synthetic vs. real repos — both, with cross-check as quality indicator.** Synthetic gives reproducibility + zero-cost-per-eval + ground truth. Real gives ecological validity. The divergence between synthetic-pass-rate and real-pass-rate is itself a benchmark-quality signal: small gap = eval is calibrated, large gap = synthetic's construct validity is collapsing. Target 50/50 ratio at v1.

2. **Single-number vs. funnel reporting — funnel, always.** The credible industry pattern (Amazon, Meta, Google) is a 4-5 stage funnel. The team must resist manager pressure to collapse to one number; funnel-reporting is what makes "85%" defensible because it specifies *which tier*.

3. **Statistical rigor (N≥3000 for 3pp claims) vs. budget (1000 quarterly feasible).** Resolved by pre-registration + CI reporting. Sub-3pp claims require power and are not published. Within-tier larger differences are reportable at 1000.

4. **Oracle proliferation vs. cost.** Every candidate oracle was evaluated against the cost cascade. Primary: compile + test + AST-conformance. Secondary: LLM-judge as Tier-3 tiebreaker. Reserve: Daikon (#4) for non-mechanical residuals. Rejected: trace (#1, Goodhart-bait), eBPF (#24, Daytona privilege risk), BMC (#21, exponential blow-up), convex-hull (#26, luxury good), digital-twin (#28, engineering-heavy).

5. **LLM-judge role.** Tier-3 tiebreaker only, single-pass (not a jury), on residuals that survived Tier-1+2. Jury (#16) and convex-hull (#26) stack judgment on judgment without adding independent signal — one judge with calibrated rubric is the right dose.

6. **Stable, flat trial-directory layout.** All artifacts land under `runs/analysis/mig_<migration_id>/<agent_model>/<variant>/<repo>_<seed>/` so any future analysis tooling (status aggregation, config comparison, cost reports) can be a filesystem walk, not a database query.

### Architectural shape (ASCII)

```
┌─────────────────────────────────────────────────────────────────┐
│                      csb mig run <config>                       │
└─────────────┬───────────────────────────────────────────────────┘
              │
    ┌─────────▼──────────┐       ┌──────────────────────┐
    │  Repo Acquisition  │       │   Synthetic Gen      │
    │  - OSS mining      │       │   - AST-ground-truth │
    │  - Customer repos  │       │   - OpenRewrite spec │
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
    │  ┌──────────────────────────────────────────────┐    │
    │  │ T1: compile + typecheck       (~$0.01/repo) │    │
    │  │  ↓ (survivors)                              │    │
    │  │ T2: existing tests            (~$0.03/repo) │    │
    │  │  ↓                                          │    │
    │  │ T2b: AST-spec conformance (synthetic only)  │    │
    │  │  ↓                                          │    │
    │  │ T3: LLM-judge (cached prompt) (~$0.08/repo) │    │
    │  │  ↓                                          │    │
    │  │ T4: Daikon invariants (opt)   (~$0.10/repo) │    │
    │  └──────────────────────────────────────────────┘    │
    └─────────┬────────────────────────────────────────────┘
              │
    ┌─────────▼────────────────────────────────────────────┐
    │  Result Writer                                       │
    │  - runs/analysis/mig_.../<repo>_<seed>/result.json   │
    │  - runs/analysis/_ledger/<task_id>/  (regressions)   │
    │  - process_telemetry.json + calibration.json         │
    │  - failure_class: {agent|harness|oracle|infra}_error │
    └─────────┬────────────────────────────────────────────┘
              │
    ┌─────────▼────────────────────────────────────────────┐
    │  Reporting (score-pre-cutoff, score-post-cutoff) +   │
    │  gold_anchor_correlation + oracle_spec_sha + CI      │
    └──────────────────────────────────────────────────────┘
```

## Phased Build Plan

### Phase 1 — MVP (weeks 1-10, Java 8→17 + Python 2→3 falsification probe) [REVISED v0.2]
- **Weeks 1-8:** Ship M1 (funnel), M2 (LLM-harness), M3 (synthetic+AST), M4-lite (50-repo gold anchor), M5 (ledger), M6 (failure classes), M7 (contamination split reporting), M8-lite (spec-SHA + pre-committed hypotheses).
- **Week 9:** M9 — Python 2→3 falsification probe on 20 synthetic repos; produce `python23_probe_findings.md`.
- **Week 10:** Schema revisions IFF probe flagged ≥2 of {M2, M3, M5}; otherwise first end-to-end Java 8→17 number (internal, hard-gated behind publication rules in M4/M7/M8).
- **Publication hard gate:** no number leaves the working group without `gold_anchor_correlation` (+ CI), both cutoff scores, `oracle_spec_sha`, `recipe_spec_sha`, and pre-reg file SHA attached.

### Phase 2 — Credibility at scale (weeks 11-14)
- Scale M4 to 200 repos (narrows correlation CI for tight pp claims).
- Ship S1 (process telemetry), S3 (monthly live-dataset refresh).
- First defensible external-facing Java number.
- Upgrade M8-lite → full M8 (external witnesses, replication packages) once n≥2 migrations justifies overhead.

### Phase 3 — Second ecosystem as proper deliverable (weeks 15-22)
- Ship S2 (IRT calibration), S4 (agent self-reported confidence).
- Python 2→3 as full deliverable (not just probe): real corpus ~200 repos, its own gold anchor, own contamination split.
- Expand to 1000-repo quarterly cadence on Java.

### Stretch (post-v1)
- N1 (Daikon) on residual ambiguity.
- N2 (held-out transferability) as eval-health metric.
- Third ecosystem (JS/TS or .NET) with the recipe-spec format already stress-tested by Python probe.

### Stretch (post-v1)
- N1 (Daikon) on residual ambiguity.
- N2 (held-out transferability) as eval-health metric.
- Publish a sanitized subset for external credibility (decoupled from the internal eval by design).

## Success Criteria for the Eval Itself (Eval-of-Eval)

1. **Gold-anchor correlation ≥ 0.7** between funnel outcome and merge-survivaled gold set at all times; < 0.7 flags eval broken.
2. **Synthetic-real pass-rate gap ≤ 15pp**; larger gap triggers synthetic-generator review (construct-validity collapse signal).
3. **Contamination gap ≤ 5pp** between pre-cutoff and post-cutoff score splits; larger triggers quarantine.
4. **Failure-class precision ≥ 90%** on human-audited sample of 50 failed trials per quarter.
5. **Regression-detection latency ≤ 24 hours** from a bad-prompt ship to a red row in `regressed_tasks.md`.
6. **Median per-repo eval cost ≤ $0.30** at steady state (target: ~$150 per 1000-repo pass).
7. **Eval half-life ≥ 9 months** — held-out gold anchor correlation should not decay faster than this; if it does, rotate corpus.

## Open Questions

1. What's the true Tier-1 catch rate on real Java 8→17? Need a 100-repo pilot to measure. If 90% not 70%, Tier-3+4 costs drop 3×.
2. Does Daytona allow `CAP_BPF`? Decides whether eBPF (#24) is ever viable as a replacement for Daikon.
3. Prompt-caching economics for Tier-3 judge: expected 4× input-cost cut. Needs validation on actual workload.
4. Who owns harness repair when M2 synthesis fails? Auto-quarantine + loud summary vs. block-until-human-triage. Small-rotation operations probably force auto-quarantine.
5. Does the regression ledger need a privacy-classification policy when it joins data from internal repos with results from public OSS repos?
6. **[MOVED TO MVP as M9]** ~Can the ecosystem-plugin API really be stable enough that Python 2→3 is a 2-week port?~ — Resolved by debate convergence: addressed in MVP week 9 via falsification probe.
7. Does inter-juror disagreement on idiomaticity correlate with real reviewer disagreement, or with juror prompt-seed noise? Determines whether any form of LLM-jury belongs anywhere in the stack.
8. **[NEW from debate]** Is the M9 probe too cheap to be probative? A 20-repo synthetic-only stress-test may fail to exercise divergences that only appear on real repos (e.g., 2to3 failure modes on production reflection-heavy Python). Escalation path: if the probe finds nothing, extend to 5-10 real Python repos in week 10 before declaring interfaces stable.
9. **[NEW from debate]** What's the retrofit cost of upgrading M8-lite → full M8 once n≥2 migrations exist? Rigorist's Round-2 concession accepts "low cost" on faith; unverified. Track this claim explicitly and revisit when Phase 3 begins.

## Risk Annotations (from `/premortem`)

> Full narratives in `docs/premortem.md`. Top-5 design modifications required before kickoff:

**D1. Pre-flight stakeholder discovery (Phase 0, ~1 week)** — before M1 code, name 3 buyer-roles whose decisions the headline changes; validate metric shape with each. If any says the shape is wrong, revise PRD. Addresses Scope + Team/Process.

**D2. Mechanical publication gate (weeks 1-2)** — `repo_health.py --publication-gate` CI check + Slack bot auto-redaction of un-stamped scores + CODEOWNERS on `hypotheses_and_thresholds.md`. Replaces procedural gate. Addresses Operational + Team/Process + Scope + Integration.

**D3. External-dependency adapter layer (week 1)** — thin adapter + replay cassette per dep (Anthropic, Daytona, OpenRewrite, code-search backend, GitHub, Docker). OpenRewrite vendored at pinned SHA. Two-model shadow judge calibration always running. Addresses Technical + Integration + Operational.

**D4. CODEOWNERS split (week 1, one PR)** — engineer shipping migration agent ≠ engineer approving threshold changes or gold-anchor re-labels. Each critical artifact has primary + shadow owner on staggered rotation. Addresses Team/Process + Scope + Operational.

**D5. Harness-drift detector + break synthetic/oracle tautology** — cached M2 recipes TTL + weekly re-validation against fresh dep resolution; M3 generator and AST-conformance authored from *disjoint* recipe sets (intersection ≤50% of primitives); synthetic-real gap as *trending* alarm not single-threshold; funnel-internal-consistency added as 6th publication stamp. Addresses Technical + Scope.

### Top risks to watch (all Critical × High before D1-D5 applied)

| # | Theme | Specific failure mode | Mitigation reference |
|---|---|---|---|
| A | Gold anchor is load-bearing but fragile | Sonnet version bump drops correlation 0.74→0.38 overnight | D3, D4, rubric versioning |
| B | M2 harness cache silently stale | "Green compile" signals from fossilized dep tree, not migrated code | D5 harness-drift detector |
| C | Publication gate is procedural | Slack screenshots leak unstamped numbers within 6 weeks of launch | D2 mechanical enforcement |
| D | Dual-incentive capture | Thresholds silently loosened to unblock Phase 2 ship | D4 CODEOWNERS split |
| E | "Ecosystem-pluggable" aspirational | M9 findings patched narrowly; deeper "build config IS migration target" assumption missed | D5 + M9 hard-gate with 5-10 real Python repos |

### Updated Open Questions (from premortem)

10. **[NEW]** Is our Phase-0 stakeholder interview protocol sharp enough? If all three buyers tell us "we need $-saved not pass-rate," are we willing to restructure the headline metric, or do we ship the % number anyway? Pre-commit the answer.

11. **[NEW]** What's the Tier-1 catch rate on real Java 8→17 before we commit to Tier-3 budget? Measurable in a 50-100 repo week-2 pilot. If <60% vs. assumed 70%, the cost model is wrong by 2-3×.

12. **[NEW]** Is the Rigorist-voice owner named and granted veto authority in a document this working group actually respects? If no, the publication gate is already broken.

## Research Provenance

**Diverge session:** 5 agents (Prior-art, First-principles, Workflow, Failure-modes, Cost).

**Converge session:** 3-position debate (Pragmatist / Rigorist / Ecosystem-Hawk), 2 rounds. Emerged position: 10-week MVP with M4-lite + full M7 + M8-lite + M9 probe, hard-gated publication rules.

**Premortem:** 5 failure lenses (Technical / Integration / Operational / Scope / Team-Process). All returned Critical × High; 5 cross-cutting themes surfaced; 5 design modifications (D1-D5) prescribed as prerequisite to kickoff.

**Convergence:** Tiered funnel (#22), LLM-inferred harness (#3), synthetic+AST (#13+#18), regression ledger (#15), funnel-reporting, anti-LLM-jury-as-primary.

**Divergence resolved:** Trace/eBPF/WASM dropped as primary; Daikon kept as reserve; 50/50 synthetic-real mix; pre-registration for sub-3pp claims.

**Key surprises that reshape priorities:** (a) inference cost dominates infra 70-170× → oracle optimization not container optimization; (b) eval is a training-data leak vector → treat as secret with 12-month half-life; (c) "perfect on all oracles" migrations get rejected by real reviewers at ~15% → error bar ≈ signal size without human anchor; (d) Amazon's PR-count is misleading industry precedent → funnel reporting is a differentiator.

**Ideas explicitly ruled out (with reason):** #1 trace (Goodhart-bait), #5 WASM (doesn't cover JVM), #8 shadow canary (out-of-scope infra), #16 jury as primary (correlated hallucination), #19 ecosystem ripple (second-order), #21 BMC as default (exponential blow-up), #24 eBPF (Daytona privileges), #27 WTP market (out of scope), #28 digital twin (engineering-heavy), #30 micro-ecosystem (second-order).
