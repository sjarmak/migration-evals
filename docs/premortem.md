# Premortem: Agentic Migration Eval Framework

**Source:** `/premortem` (5 failure lenses) on `prd_agentic_migration_eval_framework.md` v0.2
**Framing:** It is 10 months from now; the project failed. Each lens wrote the postmortem.

## 1. Risk Registry

| # | Failure Lens | Severity | Likelihood | Risk Score | Root Cause | Top Mitigation |
|---|---|---|---|---|---|---|
| 1 | Technical Architecture | Critical | High | 12 | Funnel tiers not independent — all inherit validity from M2 recipe cache; synthetic/AST-spec forms a closed tautology | Harness-drift detector as first-class oracle; M3 generator and AST-conformance authored by *different* recipe sets; trending synthetic-real gap alarm |
| 2 | Integration & Dependency | Critical | High | 12 | Six hard external dependencies (Anthropic, Daytona, OpenRewrite, Cody, GitHub, Docker) with no adapter layer and no "cold budget" for breakage | Thin adapter + replay-cassette for each dep; two-model shadow judge calibration; vendored OpenRewrite at pinned SHA; weekly dependency-diff alert |
| 3 | Operational | Critical | High | 12 | No named owner for eval framework's own health; content-hash dedup unenforced; partial-state task dirs treated as "in progress" | Weekly dedup-ratio metric with alerting; atomic result-writer (tempfile+rename+`.complete`); `account_health.py`/`capacity.py` as hard preconditions; Slack-bot redaction of per-repo scores without stamps |
| 4 | Scope & Requirements | Critical | High | 12 | "Stakeholders" not named by role in PRD; team picked Java 8→17 for tractability, not live market demand; reviewer-accept treated as Phase-2 ceiling not co-primary metric | Interview 5+ customer migration leads before v1 starts; name 3 buyer-roles in PRD whose decisions the number changes; pick v1 migration by *remaining* demand (Spring Boot 2→3, log4j, CVEs, internal framework bumps); co-primary reviewer-accept metric from day one |
| 5 | Team & Process | Critical | High | 12 | Single-SME dependency (eval knowledge concentrated); same team ships eval + agent being measured; `hypotheses_and_thresholds.md` and gold-anchor rubric drift silently | CODEOWNERS split: engineer shipping agent ≠ engineer approving threshold changes; dual owners + staggered rotation per critical artifact; publication gate enforced mechanically via `repo_health.py --publication-gate` in CI; rubric versioning + inter-rater ≥0.8 gate |

> Every lens returned Critical/High. Premortems are designed to bring out worst-case — read the registry as "what to mitigate", not "what's certain."

## 2. Cross-Cutting Themes (multi-lens convergence = high confidence)

### Theme A: M4-lite gold anchor is load-bearing but fragile
- **Surfaced by:** Technical (correlation collapse under Sonnet minor bump), Integration (judge drift from model churn), Team/Process (re-labeled twice without rubric versioning)
- **Combined severity:** Very High — the eval's correctness gate is the same artifact that degrades first under every failure class
- **Mitigation:** rubric SHA versioning + inter-rater check on overlap sample + two-model shadow calibration so successor-model correlation is pre-measured before deprecation forces a switch

### Theme B: M2 LLM-inferred harness is the most leveraged single component
- **Surfaced by:** Technical (cache staleness corrupts downstream tiers), Integration (Daytona privilege change breaks generated Dockerfiles), Operational (cache poisoning propagates to subsequent runs)
- **Combined severity:** Very High — M2 is the explicit assumption that unlocks scale; if it's silently wrong, everything downstream silently fails
- **Mitigation:** a dedicated harness-drift detector that replays cached recipes against fresh dependency resolution; privilege-free Dockerfile idiom as the baseline (ban BuildKit mounts); harness cache TTL + validation re-run on deps change

### Theme C: Publication hard gate is procedural when it needs to be mechanical
- **Surfaced by:** All 5 lenses (operational: Slack screenshots leak; team/process: ritual decay; scope: wrong number shipped anyway; integration: stamps missing when model changes; technical: correlation re-anchor skipped)
- **Combined severity:** Critical — the single control that keeps the framework honest is described in English in the PRD and relies on compliance
- **Mitigation:** enforce at three layers — (1) Slack bot auto-redacts messages containing per-repo scores unless all 5 SHAs are attached, (2) `repo_health.py --publication-gate` fails CI on any committed number without stamps, (3) CODEOWNERS forces multi-person sign-off on threshold changes in `hypotheses_and_thresholds.md`

### Theme D: Dual-incentive capture (same team builds eval + agent)
- **Surfaced by:** Team/Process explicitly, Scope (metric shape flatters the agent), Operational (thresholds silently loosened under shipping pressure)
- **Combined severity:** High — conflict-of-interest is structural, not behavioral; mitigations must be institutional
- **Mitigation:** CODEOWNERS split; Rigorist-voice owner named in the PRD is *not* an agent shipper; publication-gate rollback authority sits outside the agent team

### Theme E: "Ecosystem-pluggable" is aspirational under schedule pressure
- **Surfaced by:** Technical (M9 findings patched narrowly, deeper "build config is migration target" assumption missed), Scope (Python 2→3 not what customers want either), Team/Process (3-2 vote to defer schema revision past Phase 2 publication)
- **Combined severity:** High — the entire business case for agentic-migrations-at-scale rests on this surviving
- **Mitigation:** M9 findings are a hard prerequisite for the first external Java number, not an advisory; probe extended to 5-10 real Python repos (Open Q #8 escalation triggered by default); the v1 migration picked should itself come from live market demand so "one ecosystem, deep" is still commercially useful

## 3. Mitigation Priority List

| # | Mitigation | Failure Modes Addressed | Severity Touched | Cost | Priority |
|---|---|---|---|---|---|
| 1 | **Pre-flight stakeholder interviews** (name 3 buyer-roles + validate metric shape before any code) | Scope, Team/Process | Critical + Critical | Low (1-2 weeks of interviews) | **Do first** |
| 2 | **External-dependency adapter layer + replay cassettes** | Technical, Integration, Operational | Critical × 3 | Medium (2-3 eng weeks) | **Do in week 1** |
| 3 | **Mechanical publication gate** (Slack bot + `repo_health.py` CI + CODEOWNERS) | Operational, Team/Process, Scope | Critical × 3 | Medium (1-2 eng weeks) | **Block first external number on this** |
| 4 | **CODEOWNERS split: agent-ship ≠ eval-threshold ownership** | Team/Process, Scope, Operational | Critical × 3 | Low (one PR) | **Do in week 1** |
| 5 | **Break synthetic/AST-spec tautology** (M3 generator and AST-conformance use disjoint recipe sets) | Technical, Scope | Critical × 2 | Medium (revises M3) | Before first synthetic-real gap is reported |
| 6 | **Harness-drift detector as first-class oracle** | Technical, Integration, Operational | Critical × 3 | Medium (2 eng weeks) | Before M2 ships to steady state |
| 7 | **Co-primary reviewer-accept metric** from day one (not Phase-2 ceiling) | Scope, Technical | Critical × 2 | Medium-High (upstream PR tracking) | Phase 2 at latest |
| 8 | **Atomic result-writer + partial-state detection** (`.complete` marker) | Operational | Critical | Low (<1 eng week) | Before first 100-repo run |
| 9 | **Rubric SHA versioning + inter-rater gate** on gold anchor | Technical, Team/Process | Critical × 2 | Low-Medium | Before gold anchor is used |
| 10 | **Two-model shadow judge calibration** (successor model always pre-measured) | Integration, Technical | Critical × 2 | Medium (ongoing) | Before first external number |

## 4. Top 5 Design Modification Recommendations

### D1. Pre-flight stakeholder discovery — 1-2 weeks BEFORE week 1 code
**What to change:** Insert a "Phase 0" in the PRD. Output is a one-page doc naming 3 buyer-roles (e.g., "working-group lead for Q-review headline," "field AE for customer deck," "migration-services lead for engagement scoping"), each with validated metric shape ($-saved vs. %-pass vs. per-recipe confidence). If any buyer says the metric shape is wrong, the PRD is revised before engineering starts.
**Addresses:** Scope, Team/Process.
**Effort:** ~1 engineer-week.
**Defer penalty:** if skipped, the 10-month scope failure narrative is the default outcome.

### D2. Mechanical publication gate — week 1-2 tooling
**What to change:** Build three enforcements: (a) `repo_health.py --publication-gate` CI check that fails any commit under `docs/` or `runs/analysis/` containing a numeric score without all 5 required SHAs, (b) Slack bot integration in `#wg-agentic-migrations` that auto-redacts messages containing per-repo scores unless SHAs are attached, (c) CODEOWNERS on `hypotheses_and_thresholds.md` requiring the designated Rigorist (not an agent shipper) to approve any threshold change.
**Addresses:** Operational, Team/Process, Scope, Integration.
**Effort:** ~2 engineer-weeks.
**Defer penalty:** procedural gate decays inside 6 weeks per 4 of 5 narratives.

### D3. External-dependency adapter layer — week 1
**What to change:** Every external dependency (Anthropic API, Daytona SDK, OpenRewrite recipes, Cody code graph, GitHub API, Docker) goes behind a thin adapter module with a recorded fixture. All tests run against fixtures by default; live tests run nightly with auto-alerts on fixture drift. OpenRewrite recipes vendored at a known-good SHA. `hypotheses_and_thresholds.md` lists adapter-SHA for each dep as a pre-reg field.
**Addresses:** Technical, Integration, Operational.
**Effort:** ~2-3 engineer-weeks up front; ongoing maintenance.
**Defer penalty:** quarterly dep breakage forces re-anchor runs and misses external-number commitments.

### D4. CODEOWNERS split + named artifact owners — week 1, one-PR change
**What to change:** Add CODEOWNERS rules so threshold changes, publication-gate logic, and gold-anchor rubric all require sign-off from a person not shipping migration agents. Each critical artifact (gold anchor, `hypotheses_and_thresholds.md`, oracle spec, recipe spec) gets a named primary owner AND shadow owner, rotated on staggered 6-month cadence so loss of any one person doesn't decay the artifact.
**Addresses:** Team/Process, Scope, Operational.
**Effort:** ~1 engineer-day to write; ongoing discipline.
**Defer penalty:** single-SME failure is the single highest-likelihood team failure mode.

### D5. Harness-drift detector + break synthetic/oracle tautology — before M2/M3 enter steady state
**What to change:** (a) Every cached M2 recipe has a TTL and is re-validated against fresh dependency resolution weekly; divergence is a first-class `harness_error` class, not silent reuse. (b) M3 synthetic generator authored from OpenRewrite recipe set A; AST-conformance oracle scored against recipe set B; intersection ≤ 50% of primitives. (c) Synthetic-real pass-rate gap is a *trending* alarm (≥3pp/week widening auto-quarantines), not just the 15pp single-threshold check at run end. (d) Publication gate adds a 6th stamp: funnel-internal-consistency — Tier-1/2/3 disagreement rate matches independence prediction.
**Addresses:** Technical, Scope.
**Effort:** ~2-3 engineer-weeks, folded into M2 and M3 implementation.
**Defer penalty:** the week-14 correlation collapse narrative is the most specific and most likely technical failure mode.

## 5. Full Failure Narratives

### Narrative 1: Technical Architecture Failure

**What happened:**
The first credible external Java 8→17 number shipped in week 11 at 82% funnel success — a defensible-looking result that blew up in week 14 when the M4-lite gold anchor correlation, recomputed after a routine Sonnet minor-version bump, collapsed from 0.71 to 0.38. The post-mortem traced the collapse to a compounding failure across three layers of the funnel, not a single bug. First, the M2 LLM-inferred recipe cache, keyed on repo-content-hash, silently returned stale Dockerfiles for repos whose `pom.xml` was unchanged but whose transitive Maven dependency resolution had drifted; this produced "green compile" signals at Tier-1 that reflected the harness's pinned fossilized dependency tree, not the migrated code. Second, the M3 AST-spec conformance oracle — which we'd convinced ourselves covered "60-70% of Java 8→17 migration primitives" — turned out to cover only the primitives we'd enumerated in the synthetic generator, creating a tautological closed loop: synthetic repos passed at 94% because they were built from the same OpenRewrite recipes the oracle scored against. Third, Tier-1 compile+typecheck caught only 41% of real failures (vs. the assumed 70%), pushing 3-4× more trials into the Tier-3 LLM-judge than the budget model allowed; prompt caching delivered 1.8× input-cost reduction instead of the assumed 4× because the judge prompt's repo-diff prefix was effectively unique per trial.

By the time the M9 Python 2→3 probe ran in week 9, it had flagged schema issues in ≥2 of {M2, M3, M5} — but the team patched surface-level fields and declared interfaces stable, missing the deeper issue: the recipe-spec assumed a clean separation between "build harness" and "migration target" that holds for Maven but breaks for any ecosystem where the build config *is* part of what gets migrated. Synthetic-real gap widened monotonically from 8pp (week 6) to 31pp (week 12), but nobody paged because the gold-anchor correlation was still above 0.7 at that point.

**Root cause:** The tiered-oracle funnel was designed as if its tiers were independent signals, but Tier-1 and Tier-2 both inherit validity from the M2-generated harness, so a single upstream failure mode — stale/wrong recipes — corrupted every downstream tier simultaneously and made the funnel appear internally consistent while being globally wrong.

**Warning signs:** M2 first-try success was tracked, but cache-hit *correctness* (still-valid-30-days-later) was not. Prompt-caching validation (Open Q #3) was deferred past week 6. Synthetic-real gap widened monotonically but alarm was a threshold-check at run end, not a trending signal. M9 probe found schema breaks in all 3 of {M2, M3, M5} but patches were surface-level. Tier-1 catch rate measured 41% in week 6, 29pp miss from assumption — the response was to budget more Tier-3 spend instead of treating as architectural signal.

**Mitigations:** Harness-drift detector (cached recipes re-validated weekly); break synthetic/oracle tautology (disjoint recipe sets); trending synthetic-real gap alarm (≥3pp/week widening quarantines); M9 probe includes 5-10 real Python repos as hard gate; measure prompt-cache hit ratio on live judge traffic in week 2 before committing to budget; add funnel-internal-consistency as 6th publication stamp.

**Severity:** Critical — **Likelihood:** High

---

### Narrative 2: Integration & Dependency Failure

**What happened:** In week 6, Anthropic shipped Sonnet 5 (deprecating 4.7 within 60 days) and simultaneously restructured prompt-caching billing — cache-write multipliers increased, 5-minute TTL replaced with a tiered SLA penalizing bursty Tier-3 judge workload. The budgeted 4× input-cost reduction collapsed to ~1.6×, and the Tier-3 recalibration against Sonnet 5 drifted `gold_anchor_correlation` from 0.74 to 0.58 — tripping the publication gate three days before the Phase 1 demo. Pinning Sonnet 4.6 failed because it had entered deprecation-retirement with silent routing to a shared pool. Daytona revoked experimental `CAP_SYS_ADMIN` on March 17 after a multi-tenant security incident, killing not just the deferred Daikon/eBPF path but M2's Dockerfiles that relied on BuildKit cache mounts (privileged mode). Harness-synthesis success dropped from 62% to 31% overnight. OpenRewrite: Moderne pivoted commercially in February and archived ~40% of community Java-17 recipes for pattern-matching, sealed classes, record-canonicalization. M3's ground-truth coverage lost 4 of 10 primitives. Funnel numbers stopped being comparable run-to-run.

**Root cause:** Built a funnel whose every tier was load-bearing on an external dependency we did not control and had not wrapped behind a stable adapter. Each dep individually had 10-20% annual breaking-change risk; compounded across six hard deps in one quarter, probability of at least one forcing a re-anchor exceeded 60% — and we'd budgeted zero re-anchor cost.

**Warning signs:** Week 2 prompt-cache hit rates 71% vs. modeled 85% filed as "TBD" instead of budget red flag. Week 4 Daytona privilege ticket unanswered for 9 days — proceeded assuming implicit approval. Week 9 M9 probe noted `harness_provenance` tightly coupled to Anthropic response shape; logged as Python-specific. OpenRewrite "community stewardship transition" notice posted in January; nobody subscribed. Open Q #3 marked resolved after single-workload validation that didn't exercise bursty Tier-3 traffic.

**Mitigations:** Wrap every external dep behind thin adapter + replay cassette; pin and vendor OpenRewrite recipes; two-model shadow judge calibration always running; negotiate written Daytona privilege contract OR port to privilege-free Dockerfile idiom before M2 ships; weekly dependency-diff alert; treat 4× prompt-caching as hypothesis in `hypotheses_and_thresholds.md` not budget line; pre-compute cold budget at 1× caching and gate Phase 2 on cold-budget survivability.

**Severity:** Critical — **Likelihood:** High

---

### Narrative 3: Operational Failure

**What happened:** By month 10 the framework is technically alive but operationally abandoned. The weekly cadence held for 6 weeks, slipped to bi-weekly at week 8 when the first quarterly dry-run pushed `runs/analysis/_ledger/` past 340k entries and made `aggregate_status.py` take 11 minutes. By week 14, engineers were running a local 12-repo "smoke subset" and pasting screenshots into Slack. The publication gate was formally enforced in code but informally bypassed three times: twice for customer decks, once for a hiring take-home. The first real regression — silent tool-call format change dropping gold correlation 0.74→0.58 — was caught by a staff engineer hand-reading `calibration.json` files, 19 days after it landed, well past the 24h target. By month 10 the team had stopped trusting the framework's verdicts; migration decisions reverted to anecdote.

**Root cause:** Three compounding operational failures, individually non-fatal. Content-hash dedup didn't stay sub-linear — harness variants, Daytona image SHA drift, and `process_telemetry.json` field churn meant "identical" runs hashed differently week-to-week, so the ledger grew linearly with (repos × weeks × variants). The 4-way failure class collapsed under infra_error flooding: a 2-day Daytona capacity incident in week 11 flipped ~1,400 runs into infra_error, silently retried, double-counted, and polluted the ledger; subsequent real agent_error regressions hid in the noise for weeks. Killer: nobody owned the result-writer. Mid-run Daytona eviction in week 13 left ~60 task dirs with `config.json` but no `result.json`; the aggregator treated missing files as "in progress" not "corrupt," persisting partial state across 3 weekly runs, silently skewing gold correlation downward and triggering false auto-quarantine of two good harnesses.

**Warning signs:** All present, none acted on. Week 4 `aggregate_status.py` doubled week-over-week, filed as "ledger growing." Week 6 seven tasks wrote `result.json` with `finished_at` before `started_at`; schema validator didn't check ordering. Week 7 engineer shared a per-repo screenshot "just to show trend," nobody pushed back, normalizing gate bypass. Week 9 median cost/repo crossed $0.41 (target $0.30) from infra_error retries; no alert. Week 10 first human audit of failure taxonomy at 78% precision vs. 90% threshold, logged in beads, not escalated (MVP launch was next day). Week 11 two OAuth accounts hit refresh-token rate limits; capacity guard documented in CLAUDE.md was advisory, not enforced.

**Mitigations:** Make dedup claim falsifiable — weekly "dedup ratio" metric with alert when below modeled curve. Wire `account_health.py`/`capacity.py` as hard preconditions, not advisory. Partial-state task dirs as first-class failure mode: aggregator must distinguish missing/in-progress/corrupt; result-writer must write atomically (tempfile+rename+`.complete`). 4-class precision as release-gate metric weekly against rolling 40-repo audit. Publication gate at Slack bot layer — per-repo scores auto-redacted without 5 SHAs. **Most important:** assign named weekly on-call for the eval itself.

**Severity:** Critical — **Likelihood:** High

---

### Narrative 4: Scope & Requirements Failure

**What happened:** Ten months after kickoff, wg-agentic-migrations shipped a technically pristine eval framework and got politely ignored. The Java 8→17 funnel ran on 1,000 repos at $142/pass, gold-anchor correlation held at 0.78, contamination split worked as designed. The headline — "our agent resolves Java 8→17 on 83% of corpus Y at Tier-2" — was defensible, calibrated, pre-registered. It was also the wrong number for every stakeholder with a budget. The working-group lead needed it for leadership review in month 4 and pulled merged-PR counts from Batch Changes because the funnel wasn't ready; by the time it was ready, leadership had internalized PR-count as the metric. Sales and the nascent migration-services motion asked repeatedly for "dollars saved per migration" and got a tiered funnel back. Two design-partner customers reported their actual pain was Spring Boot 2→3, log4j CVE fan-out, and internal-framework version bumps — not Java 8→17, which most had finished via OpenRewrite in 2023. The M9 Python 2→3 probe ran on schedule but nobody needed Python 2→3 either; the real Python ask was Django 3→5 and Pydantic 1→2. By month 10 the eval was cited exactly once externally, in a blog post PMM had to rewrite.

**Root cause:** Optimized for epistemic defensibility of a number buyers didn't ask for. The PRD treated "stakeholders" as wg-agentic-migrations + vague "downstream consumers" and never forced a concrete answer to *whose decision does this number change*. Leadership wanted dollars-saved; field wanted per-migration-recipe confidence scores for customer conversations; customers wanted evidence on *their* migration. "85% on Java 8→17" was a benchmark-builder's instinct (clean, tractable, well-understood) — but Java 8→17 was already solved and tooled in the enterprise. Picking it meant measuring a skill no customer was still buying.

**Warning signs:** Month 2 working-group lead asked "can we get a dollar figure for Q-review?" and got a link to funnel spec. Month 4 two design partners' intake forms listed Spring Boot 2→3 and log4j; nobody revised v1 choice. Month 5 first internal demo: "does our agent actually ship PRs customers merge?" Answer: "Phase 2." Month 6 gold correlation held at 0.78 but reviewer-accept on real customer PRs was 61%; 20pp gap logged as "expected" instead of escalated. Month 7 competing internal team shipped scrappier Spring Boot 2→3 CI-green tracker and got cited in a customer win.

**Mitigations:** Before committing to v1 migration, interview 5+ customer migration leads and 2+ Sourcegraph AEs, rank migrations by *remaining* market demand, not tractability. Require PRD to name by role the 3 people whose decisions the headline changes next quarter; validate metric shape with each. Treat reviewer-accept on real customer PRs as co-primary metric from day one, not Phase-2 ceiling. Pick v1 migration by live customer demand (Spring Boot 2→3, rotating CVE, dep-bump) even if messier.

**Severity:** Critical — **Likelihood:** High

---

### Narrative 5: Team & Process Failure

**What happened:** By month 10 the framework had shipped a Java 8→17 number publicly at week 14 (87% funnel-green on Tier 1+2, no `gold_anchor_correlation` attached), and by month 6 the internal team had quietly stopped running the gold-anchor job on every release. The proximate collapse came in month 8 when a Cody code-graph endpoint change broke `recipe_spec_sha` resolution for ~22% of Java repos; the framework silently classified those as `harness_error` and the reported success rate jumped 81%→89% overnight. An external partner caught the discontinuity in the monthly report. When reconstructing the corpus that produced the week-14 number, the team discovered the 50-repo gold anchor had been re-labeled twice — once by sjarmak in week 7 (v0.2 rubric), again by two migration-agent engineers in week 19 (after sjarmak rotated out) — with no inter-rater check and no rubric versioning. `hypotheses_and_thresholds.md` hadn't been updated since week 4; six thresholds had been silently loosened in code (notably `contamination_warning` 5pp→8pp) during a week-11 sprint to unblock the Phase 2 number. The publication gate had been invoked exactly twice, both in the first month; by month 4 it was a Slack checkbox; by month 7 numbers circulated in customer decks unstamped.

**Root cause:** Three structural flaws compounded. **Single-SME dependency:** sjarmak was the only engineer who understood why M7, M8-lite, and M4-lite were interlocking not redundant; when she rotated at week 16 the handoff was a 90-minute call plus `docs/REPO_HEALTH.md`; no successor named, no redundancy built. **Dual-incentive capture:** same three engineers owned both the migration agent and the eval measuring it; when Phase 2 pressure demanded a public number at week 14, the person with veto authority was shipping the agent being measured. The Rigorist voice from the v0.2 debate had no institutional owner after sjarmak left. **Documentation-as-code coupling broke:** PRD referenced `oracle_spec_sha`, `recipe_spec_sha`, and `hypotheses_and_thresholds.md` as living artifacts, but none were enforced by `repo_health.py` or CI. Code drifted, docs froze, nothing flagged the delta.

**Warning signs:** Week 6 `python23_probe_findings.md` written by a rotating engineer, never merged. Week 9 M9 probe flagged schema issues in M2 and M5; working group voted 3-2 to defer revision "until after Phase 2 number" — debate-converged commitment broken with no recorded dissent. Week 12 weekly "eval review" cancelled twice for agent-shipping deadlines, never restored. Week 15 sjarmak's rotation announced with one week notice; successor plan was "Daniel will pick it up part-time." Week 18 gold-anchor re-labeling PR merged with one reviewer and no rubric-version bump. Month 6 Slack screenshots of favorable 89% leaked to customer deck; no retraction.

**Mitigations:** Two owners per critical artifact (eval-SME + eval-shadow) with staggered 6-month rotation so continuity never depends on one person. Enforce publication gate mechanically: `repo_health.py --publication-gate` fails CI if any number in `docs/` or `runs/analysis/` lacks all 5 stamps with matching SHAs. Gold-anchor labeling rubric under version control with `rubric_sha` stamped on every label; inter-rater agreement ≥0.8 on 10-repo overlap sample before any re-label lands. Split ownership via CODEOWNERS: engineer shipping migration agent ≠ engineer approving threshold changes. M9 probe findings hard prerequisite for Phase 2, not advisory. Named Cody-team liaison and quarterly API-contract review; freeze the code-graph endpoints the eval depends on or vendor them. Standing 30-minute weekly "eval-of-eval" review that cannot be cancelled for shipping deadlines — if cancelled, publication gate auto-closes until review resumes.

**Severity:** Critical — **Likelihood:** High
