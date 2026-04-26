# Calibration Starters: Priority Order

When standing up the funnel against real agent-produced changesets,
**which migration recipe you calibrate against first matters**. Picking
a recipe whose diffs trivially pass every tier produces no headroom for
the funnel to discriminate, and the resulting calibration numbers are
noise. This doc fixes the priority order so future contributors don't
silently pick a useless starter.

The ranking is by **signal-per-effort** — how much information the
funnel extracts per instance staged, weighted by how cheap the recipe
is to set up and run.

## Order

### 1. Go import-path rewrite — `configs/recipes/go_import_rewrite.yaml`

**Best signal-per-effort. Start here.**

All three tiers carry real signal:

- **Tier 0 (`diff_valid`)** — import surgery is one of the easiest
  patterns to mis-apply (line-by-line search-and-replace gone wrong,
  off-by-one in fenced code blocks). Real failures here.
- **Tier 1 (`compile_only` via `go build ./...`)** — Go's compiler
  catches the long-tail of import breakage: transitive imports through
  intermediate packages, vendored copies that no longer match the
  rewritten path, type assertions that compiled against the old
  package's types but not the new one's. High discriminative power.
- **Tier 2 (`tests` via `go test ./...`)** — surfaces cases where the
  rewritten import points at a package whose API drifted since the
  original was vendored, even when types still resolve.

10 staged instances of any agent-driven Go import rewrite are enough to
publish the first calibration number.

### 2. Dockerfile base-image bump — `configs/recipes/dockerfile_base_image_bump.yaml`

**Tier 0+1 only. Useful but lower headroom.**

- **Tier 0** — base-image bumps are mechanically simple, so tier 0 has
  thin signal (mostly catches malformed diff hunks, not semantic errors).
- **Tier 1 (`compile_only` via `docker build .`)** — the real oracle.
  Catches incompatible base images (Alpine ↔ Debian system-package
  divergence, default user changes, missing entrypoint binaries),
  removed deprecated apt packages, and changed default workdirs.
- **Tier 2 is intentionally skipped.** The target application's test
  command varies per repo, and forcing one here pushes us toward
  per-target recipes the starter doesn't need. Canonical invocation is
  `--stages diff,compile`. See [usage.md](usage.md#recipes-and-canonical-stages).

Ship after the Go import rewrite has produced its first survival number;
adds a second data point with a different tier-2-shaped hole.

### 3. Skip — pure find-and-replace in markdown / docs

**No funnel headroom. Do not calibrate against this shape.**

A pure docs/markdown rewrite has nothing that compiles, nothing that
runs as a test. Tier 0 trivially passes (a syntactically valid diff
that touches markdown almost always parses), tiers 1 and 2 are not
applicable, tier 3 (judge) is the only oracle with anything to say.
The funnel collapses to a single cognitive-tier score, which is exactly
what the funnel is designed *not* to be.

If you need to evaluate a pure-docs migration, skip the funnel and use
the judge tier directly with a calibrated rubric.

### 4. Defer — dependency-version upgrades

**Defer until the loop is proven on simpler shapes.**

Examples: Java 8 → 17, Spring Boot 2 → 3, Log4j 1 → 2, Python 2 → 3.

These migrations have very high tier-1/tier-2 signal in principle, but
require:

- A working build harness for each dependency surface (Maven, Gradle,
  npm, pip; sometimes coupled with native toolchain versions).
- Recipes that handle deprecation chains (Java 8 → 11 → 17 is a
  different shape than Java 8 → 17 direct).
- Often, per-target build customizations.

The starter recipes (Go import, Dockerfile bump) prove the funnel works
end-to-end on real agent diffs without the recipe-authoring overhead
swamping the calibration. Once those have published survival numbers,
revisit dependency upgrades.

The shipped `configs/recipes/java8_17.yaml` is a placeholder for this
shape — it works against a fixture and is exercised by tests, but is
**not** recommended as a first calibration target.

## See also

- [`docs/oracle_funnel.md`](oracle_funnel.md) — tier definitions and
  cost math.
- [`docs/usage.md`](usage.md#recipes-and-canonical-stages) — per-recipe
  canonical `--stages` invocations.
- [`docs/tier1_skip.md`](tier1_skip.md) — publication-gate conditions
  before any tier-1 number ships.
