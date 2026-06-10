# migration-evals — agent notes

Post-hoc grading framework for agent-produced **batch-change diffs** (one mechanical
rule applied across many repos). Per-changeset trials cascade through cheap→expensive
oracles (diff validity → compile → tests → AST conformance → LLM judge → invariants)
and emit one stamped `result.json` per trial; aggregation produces a per-tier funnel,
a contamination split (repos before/after model cutoff), and correlation against
merged-PR survival. The input is always `(repo, base_commit, patch.diff)` — see README
for the full design. This file captures only what the code/README does not say.

Issue tracker: bd (beads). Run `bd prime` for workflow; use `bd` for all task tracking
(not TodoWrite / markdown TODOs) and `bd remember` for persistent knowledge (not MEMORY.md).

## Don't

- Don't add issue-understanding, retrieval, planning, or task-to-patch scoring. Those
  are SWE-bench's domain; this framework grades a diff the agent _already produced_.
  Mixing them in breaks the stated non-goal and the `(repo, base_commit, patch.diff)`
  contract every oracle assumes.
- Don't make the smoke path require API keys, Docker, or a live agent platform. The
  fresh-clone smoke (`configs/java8_17_smoke.yaml`) replays from cassettes on purpose;
  adding a network/key dependency there silently breaks offline CI for every contributor.
- Don't hand-edit `data/gold_anchor.json`. It is harvested by `scripts/mine_gold_anchor.py`
  from merged+survived OSS PRs; manual entries poison the real-world correlation anchor.
- Don't drop or reorder the result stamps (`oracle_spec_sha`, `recipe_spec_sha`,
  `pre_reg_sha`). The publication gate and contamination split depend on them; unstamped
  results can't be reproduced or pre-registered.
- Don't re-enable Tier 0 casually — see `docs/tier0_skip.md` for the three conditions
  that must hold before it re-opens.

## Do

- Run the suite offline before any change: `pip install -e '.[dev]'` then `pytest -q`
  (all tiers replay from cassettes; no keys needed).
- Smoke end-to-end: `python -m migration_evals.cli run --config configs/java8_17_smoke.yaml`,
  then `python -m migration_evals.cli report --run <run-dir> --out /tmp/report.md`.
- CLI subcommands: `run`, `report`, `iterator-report`, `regression`, `harness`, `probe`.
- Lint/format/type before commit: `ruff check src tests`, `black src tests`, `mypy src`
  (line-length 100, target py310 — set in `pyproject.toml`).
- When adding a migration shape, ship the full quartet: recipe config, sandbox image
  note, a committed fixture under `tests/fixtures/changeset_examples/`, and a test that
  drives it through `tests/test_run_eval.py`.

## Layout

- `src/migration_evals/` — package: `cli.py`, `funnel.py`, `runner.py`, `report.py`,
  oracles, `gold_anchor.py`, `contamination.py`, `publication_gate.py`, `pre_reg.py`,
  adapters (`adapters_docker.py`, `adapters_anthropic.py`, `adapters_claude_code.py`,
  `adapters_openai.py`, `adapters_judge.py`), `python23_probe.py`.
- `schemas/` — JSON Schemas for `result.json` and gold-anchor entries.
- `configs/` — smoke + per-migration recipes. `docs/` — PRD, premortem (R1–R15),
  integration guide, oracle-funnel and tier0-skip design notes.
- `tests/`, `tests/fixtures/changeset_examples/`, `examples/runs/` — suite + worked fixtures.
