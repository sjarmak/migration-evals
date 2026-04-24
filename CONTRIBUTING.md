# Contributing

The working assumption is that contributors are also the operators — there
is no separate maintainer layer. Anyone running the eval is responsible for
keeping the test suite green and the docs in sync.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

The full test suite must stay green at all times: it runs in <10s on a
laptop, has no API key dependencies, and uses cassette-replayed Anthropic /
Daytona / harness responses so you never need a sandbox to iterate.

## Code style

- **Black** + **ruff** + **mypy** — wired via `pyproject.toml`.
- 100-char line length.
- All public functions need type annotations.
- Prefer immutability: `@dataclass(frozen=True)` for value objects;
  return new dicts/lists rather than mutating arguments.
- Many small files > few large files; aim for <400 lines per module.

## Architecture rules

1. **Adapters are Protocols.** Production vendor SDKs (Anthropic, Daytona,
   the code-search backend, etc.) are imported only inside the concrete adapter implementations
   in `migration_evals/adapters.py`. Domain code (`funnel`, `oracles`,
   `gold_anchor`, etc.) imports the Protocol, never the SDK.
2. **Schemas are the contract.** Every `result.json` validates against
   `schemas/mig_result.schema.json` by construction; if you add a field,
   update the schema and add a fixture.
3. **No silent failures.** Every error path either propagates or sets a
   discriminated `failure_class`. Do not swallow exceptions to "be safe".
4. **No semantic heuristics in orchestration code.** Difficulty / quality /
   complexity classification belongs to the LLM judge tier, not to a
   regex or hardcoded threshold in a Python file. See
   `~/.claude/rules/common/patterns.md` §ZFC for the reasoning.

## Adding a new oracle tier

1. Add a `migration_evals/oracles/tier{N}_{name}.py` that returns an
   `OracleVerdict` from `oracles.verdict`.
2. Wire it into the cascade in `funnel.run_funnel`.
3. Add a `STAGE_CHOICES` entry to `cli.py`.
4. Add an `OracleTier` enum value in `types.py`.
5. Update `docs/oracle_funnel.md` and `schemas/mig_result.schema.json`.
6. Add a unit test in `tests/test_oracles.py` and a cassette fixture if
   the tier needs an external call.

## Adding a new ecosystem (e.g., JS/TS, Go, Spring Boot)

Use the Python 2→3 probe (`migration_evals/python23_probe.py`) as the
template:

1. Add a synthetic generator under `synthetic/<lang>_generator.py` covering
   the migration primitives.
2. Add a recipe-spec example under `configs/`.
3. Run the falsification probe pattern: generate ~20 synthetic repos,
   exercise them against the existing M2/M3/M5 schemas, and emit a findings
   JSON enumerating which schema fields fail to generalize.
4. If ≥2 schema revisions are needed, freeze the existing ecosystem's
   external numbers until schemas are revised.

## Commit / PR style

- Follow conventional commits: `feat:`, `fix:`, `docs:`, `test:`,
  `refactor:`, `chore:`.
- Each PR should keep `pytest -q` green.
- Architectural changes (new tier, new ecosystem, schema revision) get a
  one-paragraph note in the PR description that updates the relevant
  doc page in `docs/`.

## Design history

The full design rationale — diverge/converge over 30 candidate ideas, then a
3-position debate (Pragmatist / Rigorist / Ecosystem-Hawk), then a 5-lens
premortem — is captured in [`docs/PRD.md`](docs/PRD.md) and
[`docs/premortem.md`](docs/premortem.md). Read these before proposing
architectural changes; the M-list and risk-class IDs (M1–M9, R1–R15) are
referenced throughout the codebase.
