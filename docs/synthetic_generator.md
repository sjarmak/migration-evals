# Synthetic Java 8 generator + AST-spec conformance oracle

This document describes the procedural Java 8 synthetic-repo generator and the
AST-spec conformance oracle that scores pre/post migration diffs.

Scope: PRD milestone **M3** (`docs/PRD.md`), with
the D5 anti-tautology constraint explicitly encoded.

## Components

- `src/migration_evals/synthetic/java8_generator.py` - CLI generator.
- `src/migration_evals/synthetic/ast_oracle.py` - CLI oracle.
- `src/migration_evals/synthetic/primitives/*.py` - one module per
  migration primitive. Independently importable.
- `tests/fixtures/ast_pairs/<primitive>/{positive,negative}/{orig,migrated}/`
  - oracle fixture pairs.

## Primitive taxonomy (10 primitives)

| # | Primitive        | Module                | Pre (Java 8)                                 | Post (Java 17)                 |
|---|------------------|-----------------------|----------------------------------------------|--------------------------------|
| 1 | `lambda`         | `lambda_.py`          | anonymous inner class                        | lambda expression              |
| 2 | `var_infer`      | `var_infer.py`        | `ArrayList<T> xs = new ArrayList<T>()`        | `var xs = new ArrayList<T>()`  |
| 3 | `optional`       | `optional_.py`        | `if (x != null) { ... }` nested null checks   | `Optional.ofNullable(x)…`      |
| 4 | `text_blocks`    | `text_blocks.py`      | `"..." + "\n" + "..."` concatenation          | `"""…"""` text block           |
| 5 | `records`        | `records.py`          | POJO (private final fields + getters)        | `record X(...) {}`             |
| 6 | `sealed`         | `sealed.py`           | `abstract class` + subclasses                 | `sealed class ... permits ...` |
| 7 | `pattern_match`  | `pattern_match.py`    | `if (o instanceof T) { T t = (T) o; ... }`    | `if (o instanceof T t) { ... }`|
| 8 | `enhanced_switch`| `enhanced_switch.py`  | classic `switch` + `break;`                   | switch arrow arms              |
| 9 | `deprecated_api` | `deprecated_api.py`   | `new Integer(n)`, `new Date(y, m, d)`, `.stop()` | modern equivalents          |
| 10| `dep_bumps`      | `dep_bumps.py`        | legacy dep versions (JUnit 4, Guava 20…)      | bumped versions                |

Each module exports:

- `NAME: str` - stable identifier.
- `generate(rng: random.Random, out_dir: pathlib.Path) -> dict` - writes files
  under `out_dir`, returns an emission descriptor.

## PRD D5 - disjoint recipe sets (anti-tautology)

> *From PRD D5:* "M3 generator and AST-conformance authored from disjoint
> recipe sets (intersection ≤ 50% of primitives)."

The oracle intentionally covers **only a documented subset** of the
generator's primitives. If the oracle checked every primitive the generator
emits, synthetic pass-rate would collapse into a self-referential measurement:
the oracle would accept whatever the generator produced, by construction.

**Oracle-checked primitives (5 / 10 = 50%):**

- `lambda`
- `var_infer`
- `optional`
- `text_blocks`
- `records`

**Primitives exercised by the generator but NOT by the oracle (5 / 10):**

- `sealed`
- `pattern_match`
- `enhanced_switch`
- `deprecated_api`
- `dep_bumps`

These still appear in the generator output and are expected to be exercised by
other tiers of the funnel (compile, tests, LLM judge). The oracle simply does
not rubber-stamp them - which is the point.

Enforcement:

- `java8_generator.GENERATOR_PRIMITIVES: set[str]` - the full primitive set.
- `ast_oracle.ORACLE_CHECKED_PRIMITIVES: set[str]` - the check-set.
- `tests/test_ast_oracle.py::test_oracle_is_disjoint_from_generator_per_d5`
  asserts `len(oracle_set & generator_set) / len(generator_set) <= 0.5`.

Any widening of `ORACLE_CHECKED_PRIMITIVES` past 5 entries requires expanding
the generator's primitive set first so the ratio stays ≤ 0.5.

## CLI

### Generator

```bash
python src/migration_evals/synthetic/java8_generator.py \
  --out /tmp/gen --count 10 --seed 42
```

Produces `/tmp/gen/repo_0000 … repo_0009`, each with:

- `pom.xml` (Maven, `<maven.compiler.source>1.8`, `<maven.compiler.target>1.8`)
- `src/main/java/com/example/*.java` (one file per primitive chosen for the repo)
- `emission.json` (primitive list + per-primitive emission descriptors)

### Oracle

```bash
python src/migration_evals/synthetic/ast_oracle.py \
  --orig /path/to/orig --migrated /path/to/migrated
```

Emits a JSON payload with `overall ∈ {pass, fail, skip}` and a
`primitives` dict keyed by `ORACLE_CHECKED_PRIMITIVES` entries.

## Determinism

- Top-level `--seed` drives the generator.
- Per-repo child seeds are computed as `seed * 1_000_003 + index` so that any
  contiguous range of indices remains decorrelated.
- Primitive selection uses `rng.sample` over the *sorted* primitive set so
  iteration ordering does not contaminate content.
- `--count 500 --seed 42` produces 500 distinct content hashes (asserted by
  `test_generator_500_distinct_seed42`).

## Performance

The oracle is pure-Python regex scanning. Median wall time on 10 generated
repos is well under 2 seconds (asserted by
`test_oracle_median_under_2_seconds`). There are no runtime dependencies
beyond the Python standard library.
