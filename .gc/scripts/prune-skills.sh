#!/usr/bin/env bash
# Prune ~/.claude/skills/ — remove 71 unused skills decided in audit (2026-05-01).
#
# Strategy: move each unwanted skill dir into a timestamped backup. Reversible
# by mv-ing back. After moves, refresh skillager's index so the registry
# reflects reality.
#
# Decision log:
#   Group A (26)  — domains/stacks not used in any of the 13 rigs
#   Group B (15)  — mobile + Rust, no active work
#   BMad (12)     — methodology bundle being explored, replaced by brainstorm
#                   + santa-method coverage. (Wait — santa-method is being
#                   killed too. Just brainstorm + existing review.)
#   Investor (3)  — investor-materials, investor-outreach, market-research:
#                   no active fundraising/market-sizing work
#   Dead tools (4) — referenced binaries (nanoclaw, devfleet, dmux, plankton)
#                    are not installed on this system; skills can't function
#   Methodologies (8) — pattern docs not actively used; gascity covers the
#                       multi-agent orchestration space
#   Misc (3)      — continuous-learning v1 (superseded by v2),
#                   data-scraper-agent (never run), project-guidelines-example
#                   (template, no operational value)
#
# Kept but to be tagged separately in Phase 2 (not in this script):
#   marketing tag — article-writing, content-engine, crosspost, x-api,
#                   frontend-slides, video-editing, videodb, fal-ai-media,
#                   imagegen
#   demo tag     — sg-public-ds

set -euo pipefail

SKILLS_DIR="$HOME/.claude/skills"
TS="$(date +%Y%m%dT%H%M%S)"
BACKUP_DIR="$HOME/.claude/skills.backup.$TS"

GROUP_A=(
  # Freight / retail / industrial ops (8)
  carrier-relationship-management
  customs-trade-compliance
  energy-procurement
  inventory-demand-planning
  logistics-exception-management
  production-scheduling
  quality-nonconformance
  returns-reverse-logistics
  # Document/personal-life utilities (2)
  visa-doc-translate
  nutrient-document-processing
  # PHP/Laravel (4)
  laravel-patterns
  laravel-tdd
  laravel-verification
  laravel-security
  # Perl (3)
  perl-patterns
  perl-testing
  perl-security
  # Java/Spring (6)
  java-coding-standards
  jpa-patterns
  springboot-patterns
  springboot-tdd
  springboot-verification
  springboot-security
  # C++ (2)
  cpp-coding-standards
  cpp-testing
  # Flutter (1)
  flutter-dart-code-review
)

GROUP_B=(
  # Kotlin/Android/KMP (7)
  kotlin-patterns
  kotlin-testing
  kotlin-coroutines-flows
  kotlin-exposed-patterns
  kotlin-ktor-patterns
  android-clean-architecture
  compose-multiplatform-patterns
  # Swift/iOS (6)
  swift-actor-persistence
  swift-concurrency-6-2
  swift-protocol-di-testing
  swiftui-patterns
  foundation-models-on-device
  liquid-glass-design
  # Rust (2)
  rust-patterns
  rust-testing
)

GROUP_BMAD=(
  bmad-advanced-elicitation
  bmad-brainstorming
  bmad-distillator
  bmad-editorial-review-prose
  bmad-editorial-review-structure
  bmad-help
  bmad-index-docs
  bmad-init
  bmad-party-mode
  bmad-review-adversarial-general
  bmad-review-edge-case-hunter
  bmad-shard-doc
)

GROUP_INVESTOR=(
  investor-materials
  investor-outreach
  market-research
)

GROUP_DEAD_TOOLS=(
  nanoclaw-repl
  claude-devfleet
  dmux-workflows
  plankton-code-quality
)

GROUP_METHODOLOGIES=(
  santa-method
  blueprint
  ralphinho-rfc-pipeline
  team-builder
  agentic-engineering
  ai-first-engineering
  agent-harness-construction
  enterprise-agent-ops
)

GROUP_MISC=(
  continuous-learning
  data-scraper-agent
  project-guidelines-example
)

ALL=(
  "${GROUP_A[@]}"
  "${GROUP_B[@]}"
  "${GROUP_BMAD[@]}"
  "${GROUP_INVESTOR[@]}"
  "${GROUP_DEAD_TOOLS[@]}"
  "${GROUP_METHODOLOGIES[@]}"
  "${GROUP_MISC[@]}"
)

if [ ! -d "$SKILLS_DIR" ]; then
  echo "ERROR: $SKILLS_DIR does not exist" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
echo "Backup target: $BACKUP_DIR"
echo "Removing ${#ALL[@]} skills..."
echo

removed=0
missing=0
for skill in "${ALL[@]}"; do
  src="$SKILLS_DIR/$skill"
  if [ -d "$src" ]; then
    mv "$src" "$BACKUP_DIR/"
    echo "  removed: $skill"
    removed=$((removed + 1))
  else
    echo "  missing: $skill (already gone)"
    missing=$((missing + 1))
  fi
done

echo
echo "Summary: $removed removed, $missing missing"
echo "Backup:  $BACKUP_DIR"
echo "Restore: mv \"$BACKUP_DIR\"/* \"$SKILLS_DIR\"/"
echo
echo "Refreshing skillager index..."
skillager scan --all 2>&1 | tail -5 || true
echo
echo "New status:"
skillager status 2>&1 | head -20 || true
