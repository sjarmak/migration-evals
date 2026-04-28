# Go toolchain upgrade — `1.22` → `1.23`

Canonical example of a Go toolchain *major* version upgrade, the
textbook shape for an agent-driven `go.mod`-version bump batch change.
The agent rewrites the `go` directive in `go.mod` and updates source
to take advantage of an API that was stabilized in the new toolchain.
This is **distinct from** `go_import_rewrite`: that recipe rewrites
import paths against a fixed toolchain; this one holds imports stable
and moves the toolchain underneath them.

## Files

| File | Purpose |
| --- | --- |
| `repo_state/` | Pre-patch project state. A two-file Go module declaring `go 1.22`, importing `slices`, and calling `slices.Sort`. |
| `patch.diff` | Unified diff produced by the agent. Bumps `go.mod` to `go 1.23` and adds a call to `slices.Repeat` (stabilized in Go 1.23). |
| `meta.json` | `ChangesetProvider` metadata. The committed `commit_sha` is a placeholder; tests build a seeded git remote from `repo_state/` and substitute the real SHA at runtime. |

## What the funnel catches

| Tier | Verdict on this example | Failure mode this tier exists to catch |
| --- | --- | --- |
| 0 — `diff_valid` | **passes** | Malformed unified diff; line offsets that no longer match `repo_state/`. |
| 1 — `compile_only` (`go build ./...`) | **passes** if the sandbox `golang:1.23` toolchain is available | `slices.Repeat` did not exist in Go 1.22, so building this patched state on a 1.22 toolchain fails to compile. The recipe pins `golang:1.23` (see `configs/recipes/go_version_upgrade.yaml`) so the bump is exercised end-to-end. |
| 2 — `tests` (`go test ./...`) | **passes** if the sandbox `golang:1.23` toolchain is available | Behavioral drift between toolchain versions: vet warnings promoted to errors, stdlib defaults that changed across releases. |

The shipped tests only exercise tier 0 (no Go toolchain assumed in CI).
A workstation with Go 1.23 installed (or Docker with the `golang:1.23`
image pulled) can run higher tiers via
`scripts/run_eval.py --stages diff,compile,tests`.

## Reusing this example

> **The shipped `meta.json` is a template, not a runnable record.**
> `commit_sha` is `0000…0000` and `repo_url` points at an `example.com`
> placeholder. Both must be replaced with a real, clone-able remote
> whose state matches `repo_state/` before `run_eval.py` can drive
> this through the funnel against a live git remote. See the
> `go_import_rewrite` README for the worked rewrite snippet; the same
> pattern applies here. The shipped tests work around this by building
> a seeded git remote from `repo_state/` in-process.

`tests/test_run_eval.py::test_canonical_example_passes_tier0[go_version_upgrade]`
runs the same flow against an in-process seeded remote without the
manual rewrite — see `tests/conftest.py::seeded_go_version_upgrade_remote`.
