# go_import_rewrite calibration corpus

Hand-vetted fixtures used by `scripts/calibrate.py` to compute per-tier FPR / FNR for the `go_import_rewrite` recipe.

Each subdirectory under `known_good/` and `known_bad/` is one fixture, with:

- `repo/`        a small Go module the funnel runs against
- `patch.diff`   (optional) unified diff for tier-0 fixtures
- `label.json`   the hand-authored expected outcome

Known-good fixtures are valid import rewrites that should pass every tier they apply to. Known-bad fixtures are seeded with a single class of failure and are labelled with the tier expected to reject them.

## Tier coverage (x8w)

- `good_001..good_010` / `bad_001..bad_010` — tier-0 (`diff_valid`) only. The repos are stub Go modules whose imports do not resolve in isolation, so they declare `applicable_tiers: ["diff_valid"]` to opt out of tier-1 / tier-2 metrics.
- `good_011`, `good_012` — full-stack known-good: self-contained Go modules that compile cleanly and have a passing test suite. Inform every tier.
- `bad_011`, `bad_012` — `expected_reject_tier: compile_only`. Compile-time failures (unresolved local import, undefined symbol).
- `bad_013`, `bad_014` — `expected_reject_tier: tests`. Build cleanly but tests fail (assertion mismatch, runtime panic).

Run the full Docker-backed calibration with:

```bash
python3 scripts/calibrate.py \
  --migration go_import_rewrite \
  --fixtures tests/fixtures/calibration/go_import_rewrite \
  --output configs/recipes/go_import_rewrite.calibration.json \
  --recipe configs/recipes/go_import_rewrite.calibration.recipe.yaml \
  --stages diff,compile,tests \
  --sandbox-image golang:1.22
```
