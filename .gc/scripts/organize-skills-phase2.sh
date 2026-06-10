#!/usr/bin/env bash
# Phase 2 — stash project-bespoke + marketing + demo skills out of user-scope.
#
# Goal: ~/.claude/skills/ should hold only universally-applicable skills.
# Project-bespoke and special-purpose skills move to ~/.skill-stash/, organized
# by purpose, then registered with skillager as a "vault" collection so they
# remain searchable and on-demand activatable without polluting global discovery.
#
# Why each group is moving:
#   eval (12)            — codeprobe and other eval rigs already have their own
#                          (often forked) project-scope copies. User-scope
#                          versions are orphan defaults nothing currently uses.
#   gc (7)               — identical to ~/gas-city/.claude/skills/ copies. Pure
#                          deduplication; gas-city keeps the working copy.
#   project-bespoke (2)  — acceptance-loop (codeprobe) + scix-mcp
#                          (scix_experiments) live at their owning rig.
#   marketing (8)        — content/social/media ops; not tied to any rig. User
#                          opts in per session via skillager activate.
#   demo (1)             — sg-public-ds; only for demos, not daily work.
#
# Reversible: stash uses mv, not rm. Restore with:
#   mv ~/.skill-stash/<group>/* ~/.claude/skills/

set -euo pipefail

USER_SKILLS="$HOME/.claude/skills"
STASH="$HOME/.skill-stash"

EVAL_SKILLS=(
  agent-eval
  ai-regression-testing
  assess-codebase
  eval-harness
  experiment
  integration-test
  interpret
  mine-tasks
  probe
  ratings
  run-eval
  scaffold
)

GC_SKILLS=(
  gc-agents
  gc-city
  gc-dashboard
  gc-dispatch
  gc-mail
  gc-rigs
  gc-work
)

PROJECT_BESPOKE=(
  acceptance-loop
  scix-mcp
)

MARKETING_SKILLS=(
  article-writing
  content-engine
  crosspost
  x-api
  frontend-slides
  video-editing
  videodb
  fal-ai-media
)

DEMO_SKILLS=(
  sg-public-ds
)

if [ ! -d "$USER_SKILLS" ]; then
  echo "ERROR: $USER_SKILLS does not exist" >&2
  exit 1
fi

mkdir -p "$STASH/eval" "$STASH/gc" "$STASH/project-bespoke" "$STASH/marketing" "$STASH/demo"

stash_group() {
  local group="$1"
  shift
  local skills=("$@")
  local moved=0
  local missing=0
  for skill in "${skills[@]}"; do
    src="$USER_SKILLS/$skill"
    dst="$STASH/$group/$skill"
    if [ -d "$src" ]; then
      if [ -e "$dst" ]; then
        # Already stashed previously; remove the lingering user-scope dup
        rm -rf "$src"
        echo "  removed dup: $skill (already in stash)"
      else
        mv "$src" "$dst"
        echo "  stashed: $group/$skill"
      fi
      moved=$((moved + 1))
    else
      missing=$((missing + 1))
    fi
  done
  echo "  $group: $moved processed, $missing missing"
  echo
}

echo "=== eval cluster (12) → $STASH/eval/ ==="
stash_group eval "${EVAL_SKILLS[@]}"

echo "=== gc cluster (7) → $STASH/gc/ ==="
stash_group gc "${GC_SKILLS[@]}"

echo "=== project-bespoke (2) → $STASH/project-bespoke/ ==="
stash_group project-bespoke "${PROJECT_BESPOKE[@]}"

echo "=== marketing (8) → $STASH/marketing/ ==="
stash_group marketing "${MARKETING_SKILLS[@]}"

echo "=== demo (1) → $STASH/demo/ ==="
stash_group demo "${DEMO_SKILLS[@]}"

remaining=$(ls "$USER_SKILLS" 2>/dev/null | wc -l)
stashed=$(find "$STASH" -mindepth 2 -maxdepth 2 -type d 2>/dev/null | wc -l)
echo "User-scope skills remaining: $remaining"
echo "Stashed total:               $stashed"
echo

# Register the stash as a skillager collection so skills stay searchable.
echo "=== Registering stash as skillager collection 'vault' ==="
if skillager collection list 2>&1 | grep -q "^vault\b"; then
  echo "  already registered; refreshing"
  skillager collection refresh vault 2>&1 | tail -3 || true
else
  skillager collection add "$STASH" --name vault 2>&1 | tail -5 || true
fi

echo
echo "=== Status ==="
skillager status 2>&1 | head -10 || true

echo
echo "Stash root:   $STASH"
echo "Restore one:  mv $STASH/<group>/<skill> $USER_SKILLS/"
echo "Activate via: skillager activate vault/<group>/<skill>"
echo "Materialize:  cd <project> && skillager materialize vault/<group>/<skill> --scope project --agent claude"
