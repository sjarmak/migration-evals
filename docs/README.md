# Migration Eval Framework

This directory documents the  **migration eval framework** — a tiered-oracle funnel for defending calibrated success-rate numbers on named code migrations (v1: Java 8 -> 17). See `docs/PRD.md` at the repo root for the full PRD and `docs/premortem.md` for the risk register.

This `README.md` is scaffolded by the `foundation-module-scaffold` work unit. Downstream units add deeper docs (harness recipe format, oracle tier contracts, regression-ledger layout, pre-registration workflow).

## Architectural shape (from PRD Design Considerations)

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

## Module layout

- `src/migration_evals/`
  - `cli.py` — argparse entry point for `run` / `report` / `regression` / `harness` / `probe` subcommands.
  - `types.py` — shared enums (`FailureClass`, `OracleTier`).
  - `adapters.py` — PRD D3 external-dependency Protocols (Anthropic, Daytona, OpenRewrite, code-search backend, GitHub, Docker) with replay-cassette hooks.
  - `oracles/` — tiered oracle funnel stages (downstream).
  - `synthetic/` — procedural synthetic-repo generator + AST-conformance (downstream).
  - `harness/` — LLM-inferred build harness synthesis and caching (downstream).
- `schemas/mig_result.schema.json` — draft-07 JSON Schema for per-trial `result.json`.
- `tests/` — pytest coverage for schema validation and enum invariants.

## Invoking the CLI

```
python -m migration_evals.cli --help
python -m migration_evals.cli run --help
```

Subcommands are scaffolded stubs that print a notice to stderr and exit 0; they will be replaced by downstream work units.
