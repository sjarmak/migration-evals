# Go import rewrite — `ghodss/yaml` → `sigs.k8s.io/yaml`

Canonical example of a single-rule mechanical Go import-path rewrite,
the textbook shape for an agent-driven batch change. The agent finds
every reference to `github.com/ghodss/yaml` (a deprecated YAML library)
and rewrites it to the recommended `sigs.k8s.io/yaml` replacement.

## Files

| File | Purpose |
| --- | --- |
| `repo_state/` | Pre-patch project state. A two-file Go module that imports `github.com/ghodss/yaml`. |
| `patch.diff` | Unified diff produced by the agent. Rewrites the import in `go.mod` and `main.go`. |
| `meta.json` | `ChangesetProvider` metadata. The committed `commit_sha` is a placeholder; tests build a seeded git remote from `repo_state/` and substitute the real SHA at runtime. |

## What the funnel catches

| Tier | Verdict on this example | Failure mode this tier exists to catch |
| --- | --- | --- |
| 0 — `diff_valid` | **passes** | Malformed unified diff; line offsets that no longer match `repo_state/`. |
| 1 — `compile_only` (`go build ./...`) | **passes** if `go` toolchain is on PATH | Transitive imports through intermediate packages that still reference the old path; vendored copies that no longer match. |
| 2 — `tests` (`go test ./...`) | **passes** if `go` toolchain is on PATH | API drift between `ghodss/yaml` and `sigs.k8s.io/yaml`: the two packages share an interface but have made differing decisions about marshalling edge cases. |

The shipped tests only exercise tier 0 (no Go toolchain assumed in CI).
A workstation with Go installed can run the higher tiers via
`scripts/run_eval.py --stages diff,compile,tests`.

## Reusing this example

To run this through the funnel as if it were a real agent changeset,
stage it into the filesystem-provider layout and invoke `run_eval`:

```bash
# Stage one synthetic instance pointing at this fixture.
mkdir -p /tmp/staged/canonical-go-1
cp tests/fixtures/changeset_examples/go_import_rewrite/ghodss_to_sigs/{meta.json,patch.diff} \
   /tmp/staged/canonical-go-1/
# (Replace meta.json's repo_url + commit_sha with a clone-able remote
# whose state matches repo_state/.)

python scripts/run_eval.py \
    --migration go_import_rewrite \
    --provider filesystem --root /tmp/staged \
    --eval-root /tmp/eval \
    --output-root runs/analysis/mig_go_import_rewrite/canonical/poc \
    --variant poc \
    --stages diff \
    canonical-go-1
```

`tests/test_run_eval.py::test_canonical_go_import_rewrite_passes_tier0`
exercises this flow against an in-process seeded remote.
