# Python 2→3 Probe Findings — TEMPLATE

> This is a TEMPLATE. Each probe run produces a fresh `findings.json` under
> `runs/analysis/python23_probe/`. Use this template to author the
> human-readable narrative that accompanies the JSON when the gate trips.
>
> See `docs/python23_probe.md` for probe intent and the
> hard-gate definition.

---

## Run metadata

| Field                | Value                                  |
|----------------------|----------------------------------------|
| Date                 | _YYYY-MM-DD_                           |
| Probe revision       | _git sha_                              |
| `--count`            | _N_                                    |
| `--seed`             | _N_                                    |
| Findings JSON path   | `runs/analysis/python23_probe/findings.json` |
| `schema_revision_required` | _true / false_                   |

## Primitive coverage

Summary of `findings.json:primitive_coverage`. Confirm every Python case
type from `PYTHON2_CASE_TYPES` is represented.

| Case type        | Count |
|------------------|-------|
| `str_bytes`      | _N_   |
| `setup_py_div`   | _N_   |
| `two_to_three`   | _N_   |
| `unknown`        | _N_   |

Notes / coverage gaps: _free text_

## Schema mismatches by module

For each module the probe stresses, record the mismatch entries observed
and a one-line interpretation.

### Harness (M2)

- **Issue**: _e.g. `missing_ecosystem_discriminator`_
- **Field**: _e.g. `Recipe.ecosystem`_
- **Reason**: _verbatim from findings.json_
- **Interpretation**: _why this matters; what the schema needs_

### Synthetic (M3)

- **Issue**: _e.g. `case_type_not_in_generator_primitives`_
- **Field**: _e.g. `GENERATOR_PRIMITIVES`_
- **Reason**: _verbatim from findings.json_
- **Interpretation**: _why this matters_

### Ledger (M5)

- **Issue**: _e.g. `oracle_tier_enum_lacks_python_runtime`_
- **Field**: _e.g. `oracle_tier`_
- **Reason**: _verbatim from findings.json_
- **Interpretation**: _why this matters_

### Oracles (informational)

If the probe was extended to surface oracle-side gaps, record them here.
The v1 probe focuses on harness/synthetic/ledger; oracles are tracked
separately for traceability.

## Recommended schema revisions

Concrete, minimal edits required to clear the gate. Each item should map to
a single mismatch above.

1. **`Recipe` (M2)** — add an `ecosystem: Literal["java", "python"]` (or
   broader) discriminator field; default Dockerfile + build/test commands
   keyed off ecosystem. Include `ecosystem` in `harness_provenance` for
   audit.
2. **`GENERATOR_PRIMITIVES` (M3)** — split into per-ecosystem registries
   (e.g. `JAVA_GENERATOR_PRIMITIVES`, `PYTHON_GENERATOR_PRIMITIVES`); the
   public API exposes a union plus an ecosystem accessor. Update D5
   (oracle-vs-generator disjointness) accounting accordingly.
3. **`mig_result.schema.json:oracle_tier` (M5)** — extend the enum to
   include `python_2to3_runtime` and any other ecosystem-specific runtime
   tiers. Consider promoting `oracle_tier` to a structured object with
   `ecosystem` + `tier` so future ecosystems do not require new enum
   members.

## Gate decision

> If `schema_revision_required` is `true`, the publication gate for the
> first external Java number is **BLOCKED** until the revisions above land
> and a follow-up probe run flips the flag to `false`.

Decision recorded by: _author_  on _date_

Linked PRs: _list_
