# Dockerfile base-image bump — `alpine:3.18` → `debian:bookworm-slim`

Canonical example of a single-rule mechanical Dockerfile transformation,
the textbook shape for an agent-driven base-image bump batch change.
The agent rewrites only the `FROM` line; everything downstream stays
untouched. **This is the example designed to fail at tier 1**, on
purpose, so the funnel demonstrably catches the most common
base-image-bump failure mode.

## Files

| File | Purpose |
| --- | --- |
| `repo_state/` | Pre-patch project state. Dockerfile based on Alpine that runs `apk add curl` and a tiny shell entrypoint. |
| `patch.diff` | Unified diff produced by the agent. Rewrites only the `FROM` line. |
| `meta.json` | `ChangesetProvider` metadata. |

## Why this example fails tier 1 (and that is the point)

The patch is mechanically correct: one line rewritten, syntactically
valid, applies cleanly to the pre-patch state. **Tier 0 passes.**

`docker build .` (tier 1) fails: `apk add` does not exist on
`debian:bookworm-slim` (which uses `apt`). The image build aborts on
the `RUN apk add` step. **Tier 1 catches the migration error.**

This is the highest-frequency failure class for base-image bumps:
package-manager divergence (apk ↔ apt, yum ↔ dnf, etc.) and
distribution-default-user changes. A purely diff-validity check would
green-light this PR and ship a broken image.

## What the funnel catches

| Tier | Verdict on this example | Failure mode this tier exists to catch |
| --- | --- | --- |
| 0 — `diff_valid` | **passes** | Malformed unified diff; touched lines that no longer match `repo_state/`. |
| 1 — `compile_only` (`docker build .`) | **fails** with `failure_class=migration_error` | Package-manager divergence (apk ↔ apt); missing system packages on the new base; entrypoint changes; default-user changes. |
| 2 — `tests` | not applicable | The recipe `dockerfile_base_image_bump` intentionally caps at tier 1 (per-target test commands vary). See `configs/recipes/dockerfile_base_image_bump.yaml`. |

The shipped tests only exercise tier 0 (no Docker daemon assumed in
CI). A workstation with Docker available can run tier 1 via
`scripts/run_eval.py --stages diff,compile --sandbox-provider docker`.

## Reusing this example

> **The shipped `meta.json` is a template, not a runnable record.**
> `commit_sha` is `0000…0000` and `repo_url` points at an `example.com`
> placeholder. Both must be replaced with a real, clone-able remote
> whose state matches `repo_state/` before `run_eval.py` can drive
> this through the funnel against a live git remote. See the Go
> fixture's README for a worked rewrite snippet; the same pattern
> applies here. The shipped tests work around this by building a
> seeded git remote from `repo_state/` in-process.

`tests/test_run_eval.py::test_canonical_example_passes_tier0[dockerfile_base_image_bump]`
runs the flow against an in-process seeded remote without the manual
rewrite — see `tests/conftest.py::seeded_dockerfile_bump_remote`.
