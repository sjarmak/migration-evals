# Python 2→3 Falsification Probe (PRD M9)

## Intent

The Python 2→3 probe is a **falsification harness**, NOT a credible Python
eval number. Its single job: stress-test the Java-derived M2 (harness
recipe), M3 (synthetic generator), and M5 (ledger) interfaces against a
non-Java ecosystem and surface schema inadequacies BEFORE any external Java
number ships.

If you are reading this expecting a "Python migration eval result", stop.
The output is a probe finding about interface adequacy. It is intentionally
shallow — a thin Python 2→3 generator covering three idiomatic cases plus
re-runs of the Java-derived recipe-spec + oracle-spec.

## Hard Gate (PRD M9)

> If the probe flags **≥ 2** of `{harness, synthetic, ledger}` modules with a
> schema mismatch, the schema interfaces **MUST** be revised before the first
> external Java number ships.

The probe writes `runs/analysis/python23_probe/findings.json` with an
explicit `schema_revision_required: bool` field. CI / publication tooling
should read that flag and block the publication gate when it is `true`.

This gate exists because shipping a Java number with interfaces that visibly
fail to generalize to a sibling ecosystem (Python 2→3) is a credibility
hazard for the whole framework. PRD M9 calls this out as the publication
prerequisite.

## Expected v1 Outcome

Running the probe against the v1 codebase is **expected** to flip the gate
to `true`. All three of the stressed modules report a structural mismatch
on a healthy run:

| Module    | Mismatch                                                                                | Schema field             |
|-----------|-----------------------------------------------------------------------------------------|--------------------------|
| harness   | `Recipe` has no `ecosystem` / `language` discriminator; defaults bake in Maven          | `Recipe.ecosystem`       |
| synthetic | `GENERATOR_PRIMITIVES` is Java-only; Python case types (`str_bytes`, `setup_py_div`, `two_to_three`) are not representable | `GENERATOR_PRIMITIVES`   |
| ledger    | `mig_result.schema.json:oracle_tier` enum is Java-shaped; `python_2to3_runtime` is rejected | `oracle_tier` (enum)     |

These are the falsification findings. They are **not** bugs to patch in
isolation — they are the evidence motivating the schema revisions enumerated
in `python23_probe_findings.md`.

## Coverage

The synthetic generator (`src/migration_evals/synthetic/python2_generator.py`)
covers three Python-idiosyncratic cases:

- `str_bytes` — Python 2 `str`-is-`bytes` semantics; Python 3 requires
  explicit `bytes` / `str` disambiguation. Uses `"foo".encode()` and bytes/str
  concatenation.
- `setup_py_div` — Python 2 packaging via `setup.py` / `distutils`; Python 3
  ecosystem prefers `pyproject.toml`. Repos in this case carry only `setup.py`.
- `two_to_three` — runtime semantic shifts that 2to3 catches imperfectly:
  integer division (`5 / 2`), `map()` returning iterator vs. list,
  `dict.items()` view vs. list.

## Invocation

```bash
python -m migration_evals.cli probe \
    --ecosystem python23 \
    --count 20 \
    --out runs/analysis/python23_probe/
```

Or against pre-existing fixtures:

```bash
python -m migration_evals.cli probe \
    --ecosystem python23 \
    --fixture-repo-root tests/fixtures/python2_repos \
    --out runs/analysis/python23_probe/
```

Exit code is `0` whether or not revision is required. A failed probe (exit
code ≠ 0) means the probe *itself* malfunctioned, not that the schemas need
revision.

## Findings JSON Shape

```jsonc
{
  "schema_revision_required": true,
  "n_repos": 20,
  "primitive_coverage": {
    "str_bytes": 7,
    "setup_py_div": 7,
    "two_to_three": 6
  },
  "modules_with_mismatches": ["harness", "ledger", "synthetic"],
  "mismatches_by_module": {
    "harness":   [ { "module": "harness",   "issue": "...", "field": "...", "reason": "..." } ],
    "synthetic": [ { "module": "synthetic", "issue": "...", "field": "...", "reason": "..." } ],
    "ledger":    [ { "module": "ledger",    "issue": "...", "field": "...", "reason": "..." } ]
  },
  "intent": "Falsification probe (PRD M9). NOT a credible Python eval number."
}
```

Mismatch entries are deduped on `(issue, field, reason)` per module so the
file does not balloon when many repos trip the same structural gap.

## Related Docs

- `docs/python23_probe_findings.md` — TEMPLATE for the
  human-authored findings narrative once a probe run completes.
- `docs/synthetic_generator.md` — Java synthetic generator;
  contrast its `GENERATOR_PRIMITIVES` set with the Python case types.
- `docs/harness_synthesis.md` — `Recipe` schema; note the
  absence of an ecosystem discriminator.
- `prd_agentic_migration_eval_framework.md` — § M9 (falsification probe).
