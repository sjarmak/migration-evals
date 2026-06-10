#!/usr/bin/env bash
# agents-claude-md-audit.sh — sling a polecat to audit AGENTS.md +
# CLAUDE.md across all rigs. Mechanical orchestration only; LLM
# judgment lives in the polecat task spec below.
#
# Runs as a cron-triggered exec order (no LLM in this script).
set -euo pipefail

GC_CITY="${GC_CITY:-/home/ds/gas-city}"
cd "$GC_CITY"

DATE_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DATE_SLUG=$(date -u +%Y%m%d)
RIG_LIST=$(gc rig list --json 2>/dev/null | jq -r '.rigs[].name' | sort | tr '\n' ' ')

# Sling a polecat with the audit task. mol-do-work formula; polecat
# halts at report-ready and emails mayor with the findings doc.
gc sling /home/ds/gascity/polecat --on mol-do-work --stdin <<EOF
audit: AGENTS.md + CLAUDE.md weekly hygiene sweep ($DATE_SLUG)

Triggered by the weekly cron order \`agents-claude-md-audit\` at $DATE_UTC.

GOAL: audit per-project agent-context docs (AGENTS.md, CLAUDE.md, and
PL project-brief.template.md where present) across all rigs registered
in this city. Surface staleness, drift from codebase reality, and
missing best-practice sections. NO edits — diagnose only, report
back, mayor decides which findings to dispatch as fix-beads.

Rigs to audit (current snapshot): $RIG_LIST

For each rig:

1. Locate the canonical agent-context docs. Conventional paths:
   - <rig-root>/AGENTS.md
   - <rig-root>/CLAUDE.md
   - <rig-root>/.gc/AGENTS.md (per-rig override)
   - For PL'd rigs: the rig's project-brief.template.md if present
2. Read each doc + cross-reference against the rig's current main
   code state. Check:
   - File / dir references still exist at the cited path
   - Command examples still work syntactically (\`grep -nE\` for the
     verbs against the current main binary's help output where
     practical)
   - Technology versions cited (Go versions, dolt versions, gc
     binary versions) aren't more than 2 majors behind current
   - Sections that should be present per the "best practices"
     checklist below are actually present
   - Internal-only references (bead IDs, /gascity-ship, internal
     pipeline taxonomy) don't leak — those belong in handoff docs,
     not AGENTS.md / CLAUDE.md

3. Best-practice section checklist for AGENTS.md (per the dartantic
   convention these docs aspire to):
   - "What you're working on" — current scope + active goals
   - "Codebase orientation" — entry points, key directories,
     conventions
   - "Standing rules" — what to do / what to never do, with
     reasons
   - "Testing harness" — how to run tests, what gates exist
   - "How to dispatch / sling work" — for PL-aware rigs
   - "Hands-off zones" — paths agents shouldn't touch
   - "Memory anchors" — pointers to /home/ds/.claude-homes/.../memory/
     files relevant to this rig

4. Best-practice section checklist for CLAUDE.md (per Claude Code
   docs convention):
   - "Project overview" — 1-2 sentence what-is-this
   - "Build & test commands" — verbatim commands that work
   - "Code style / conventions" — language / pattern guidance
   - "Forbidden / handcuffed paths" — security or scope guard
   - Cross-references to AGENTS.md where they overlap (avoid
     duplication)

5. For each rig, produce a section in the report:
   - **Rig**: <name>
   - **Docs found**: list of paths
   - **Staleness findings**: bullet list with severity (HIGH /
     MEDIUM / LOW)
   - **Missing best-practice sections**: bullet list
   - **Friction patterns**: bullet list, each tagged
     FRICTION:<category> where category ∈ {workaround,
     duplication, missing-feature, fragility, unclear-boundary}.
     Each bullet: <category> — <one-line evidence> — affected
     gascity surface (gc subcommand / bd / supervisor / formula /
     etc.) — suggested upstream fix shape (one line, NOT a patch).
     Only include findings that point at a real gascity-side
     improvement; doc-internal cleanups belong in "Recommended
     edits" below.
   - **Recommended edits**: 1-2 concrete bullet suggestions if
     warranted
   - **Verdict**: GREEN (no action) / YELLOW (minor cleanup) /
     RED (substantive drift; consider a fix-bead)

6. Cross-rig friction aggregation: after the per-rig pass,
   produce a "Friction patterns (cross-rig)" section at the top
   of the report. For each FRICTION:<category> + gascity-surface
   pair that appears in 2+ rigs, list it ONCE with:
   - Category + gascity surface
   - Count of rigs where it appears + rig names
   - Evidence sample (one quoted passage)
   - Suggested upstream fix shape
   Sort by rig count descending. Patterns appearing in 3+ rigs
   are high-priority gascity issue candidates; tag those with
   "**HIGH-PRIORITY GASCITY ISSUE CANDIDATE**" inline so mayor
   can route them to /gascity-issue-write.

7. Write the full report to
   \`/home/ds/.gc/agents-claude-md-audit/\$DATE_SLUG.md\` so mayor
   can read it.
8. Mail mayor with a one-paragraph summary + the report path +
   counts: GREEN / YELLOW / RED rigs, total friction findings,
   and HIGH-PRIORITY gascity issue candidates.

CONSTRAINTS:
- Read-only. No edits to any AGENTS.md / CLAUDE.md / brief.
- No git operations. No PRs.
- One pass. Don't iterate.
- Skip hands-off rigs (read \`project_hands_off_rigs\` memory)
  for the deep-edit recommendations, but DO include them in the
  staleness check — knowing their docs are stale is still useful
  context for mayor.
- If a rig has zero agent-context docs, surface that as its own
  HIGH-severity finding ("rig has no AGENTS.md / CLAUDE.md — first-
  time onboarding will be hard").

OUTPUT:
- One markdown report at /home/ds/.gc/agents-claude-md-audit/\$DATE_SLUG.md
- Mail summary to mayor
- Bead closed as report-ready (mayor decides which findings become
  follow-up beads)

NO worktree creation, NO branching, NO commits, NO push.
EOF

echo "agents-claude-md-audit slung $DATE_UTC across rigs: $RIG_LIST"
