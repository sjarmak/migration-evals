# go_import_rewrite calibration corpus

Hand-vetted fixtures used by `scripts/calibrate.py` to compute per-tier FPR / FNR for the `go_import_rewrite` recipe.

Each subdirectory under `known_good/` and `known_bad/` is one fixture, with:

- `repo/`        a small Go module the funnel runs against
- `patch.diff`   the unified diff under test
- `label.json`   the hand-authored expected outcome

Known-good fixtures are valid import rewrites that should pass every tier. Known-bad fixtures are seeded with a single class of failure and are labelled with the tier expected to reject them. The current corpus targets `diff_valid` (tier 0); tier-1 / tier-2 fixtures are tracked in beads.
