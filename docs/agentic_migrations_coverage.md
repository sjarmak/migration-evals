# Agentic-Migrations Recipe Coverage Matrix

This matrix maps each migration category the framework is asked about
to (a) whether a worked recipe exists today, (b) whether the canonical
sandbox image fits the category, (c) whether a committed fixture
exercises the recipe end-to-end through the funnel, and (d) any gaps
or roadmap pointers. The purpose is one-glance visibility of what is
*graded today* versus *roadmap* — so customers and teammates do not
have to grep `configs/recipes/` to find out.

For the funnel itself see [`oracle_funnel.md`](oracle_funnel.md); for
the CLI quickstart see [`usage.md`](usage.md); for the v0.3 PRD scope
see [`PRD.md`](PRD.md).

## Matrix

| Category | Recipe | Sandbox image | Fixture | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| **Version upgrade — Java LTS (8 → 17)** | [`configs/recipes/java8_17.yaml`](../configs/recipes/java8_17.yaml) | `maven:3.9-eclipse-temurin-17` | — | graded today (T1+T2) | Fixture not yet committed under `tests/fixtures/changeset_examples/`; smoke runs against the synthetic Java generator and the [`configs/java8_17_smoke.yaml`](../configs/java8_17_smoke.yaml) cassette repos. |
| **Lateral library — Go import-path rewrite** | [`configs/recipes/go_import_rewrite.yaml`](../configs/recipes/go_import_rewrite.yaml) | `golang:1.22` | [`tests/fixtures/changeset_examples/go_import_rewrite/ghodss_to_sigs/`](../tests/fixtures/changeset_examples/go_import_rewrite/ghodss_to_sigs) | graded today (T0–T2 + quality oracles) | Calibrated against `tests/fixtures/calibration/go_import_rewrite/`; ground-truth diff and sed baseline pinned in the recipe under `quality:`. |
| **Lateral library — Dockerfile base-image bump** | [`configs/recipes/dockerfile_base_image_bump.yaml`](../configs/recipes/dockerfile_base_image_bump.yaml) | `docker:24-dind-alpine` (docker-in-docker) | [`tests/fixtures/changeset_examples/dockerfile_base_image_bump/alpine_to_debian/`](../tests/fixtures/changeset_examples/dockerfile_base_image_bump/alpine_to_debian) | graded today (T0+T1) | T2 (tests) intentionally not supported — canonical invocation is `--stages diff,compile`. The recipe's `test_cmd` is a fail-loud sentinel. |
| **Version upgrade — Node LTS** | — | — | — | roadmap | Tracked in bead `migration-evals-iai` (`node_lts_upgrade` recipe). Sandbox image candidate: `node:<lts>` (Debian-based). |
| **Version upgrade — Go toolchain** | — | — | — | roadmap | Tracked in bead `migration-evals-ge5` (`go_version_upgrade` recipe). Distinct from `go_import_rewrite`; this is `go.mod` `go` directive + module-graph compatibility. |
| **CVE / vulnerability fix** | — | — | — | decision pending | Whether CVE remediation belongs as a first-class recipe (versus being expressed through one of the existing version-upgrade or lateral-library recipes) is in flight under bead `migration-evals-dpm`. ADR not yet landed; do not assume direction. |

### Status legend

- **graded today** — Recipe + sandbox image are committed; the smoke /
  test path exercises the relevant tiers without API keys or external
  network.
- **roadmap** — Category is in the v0.3 PRD scope and a bead is filed,
  but no recipe is committed on `main` yet.
- **decision pending** — Architecture call is being made in a tracked
  bead; the matrix will be updated once the ADR lands.
- **—** in *Recipe / Sandbox image / Fixture* columns means *not yet
  committed*. **N/A** would mean *deliberately not applicable* — none of
  today's rows are N/A.

## Adding a new recipe

The minimum to graduate a category from *roadmap* to *graded today*:

1. Add `configs/recipes/<migration_id>.yaml` with `migration_id`,
   `model_cutoff_date`, `recipe.{dockerfile,build_cmd,test_cmd}`, and
   `stamps:` — copy [`configs/recipes/go_import_rewrite.yaml`](../configs/recipes/go_import_rewrite.yaml)
   for the fullest worked shape (it includes per-recipe quality oracles
   and calibration), or [`configs/recipes/java8_17.yaml`](../configs/recipes/java8_17.yaml)
   for the minimal shape.
2. Add a fixture at
   `tests/fixtures/changeset_examples/<migration_id>/<scenario>/` with
   `repo_state/`, `patch.diff`, `meta.json`, and a per-tier `README.md`
   explaining what each tier should catch.
3. Wire the fixture into the `test_canonical_example_passes_tier0`
   `parametrize` block in [`tests/test_run_eval.py`](../tests/test_run_eval.py)
   so the fixture is exercised on every CI run.
4. Update this matrix and the link in [`README.md`](../README.md).
