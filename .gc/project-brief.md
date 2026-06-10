# Project Brief — `migration-evals`

## What this project is

A grading framework for agent-produced batch-change diffs — one
mechanical rule applied across many repos, scored by a cascading
funnel (diff validity, compile, tests, AST conformance, LLM judge,
invariants). It explicitly does NOT grade how the agent got to the
patch; the input is always (repo, base commit, diff). Three
migration shapes ship today: Java 8→17, Go import-path rewrites,
Dockerfile base-image bumps. Success this quarter is a defensible
funnel, judge calibration we'd publish, and a real-world anchor via
correlation against merged-PR survival.

## How I want to hear about it

Plain English with the methodology lens. Tell me when scoring shifts
in a way that would change the headline finding, or when contamination
or judge calibration creates a credibility issue.

## When to wake me up

- A judge-calibration drift (kappa, dual-judge agreement) that
  threatens the comparability of past runs
- A contamination finding (pre-cutoff vs post-cutoff repos) that
  changes how we'd report results
- A sandbox or oracle issue with blast radius beyond a single
  fixture
- A new migration shape we're considering taking on, or scope
  expansion beyond the three shipped today
- Anything affecting the gold-anchor harvesting from public OSS PRs

## When NOT to wake me up

- Single-fixture oracle fixes
- Lint, formatting, and CI gate work
- Adapter polish for sandbox/host edge cases
- Routine wave-review cleanup

## What "going well" looks like

In four weeks, the funnel produces results that hold up to a
skeptical reviewer, the judge calibration is documented well enough
that disagreement rates are predictable, and the merged-PR-survival
anchor gives us a real-world signal we trust. The three shipped
migration shapes are each producing data we'd publish.
