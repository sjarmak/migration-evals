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

> **The shipped `meta.json` is a template, not a runnable record.**
> `commit_sha` is `0000…0000` and `repo_url` points at an `example.com`
> placeholder. Both must be replaced with a real, clone-able remote
> whose state matches `repo_state/` before `run_eval.py` can drive
> this through the funnel against a live git remote. The shipped
> tests work around this by building a seeded git remote from
> `repo_state/` in-process.

To run this through the funnel as if it were a real agent changeset:

```bash
# 1. Build a clone-able remote from repo_state/. One way:
git init --bare /tmp/canonical-go-remote.git
git -C /tmp/canonical-go-remote.git config user.email a@b.c
git -C /tmp/canonical-go-remote.git config user.name t
git clone /tmp/canonical-go-remote.git /tmp/canonical-go-seed
cp -r tests/fixtures/changeset_examples/go_import_rewrite/ghodss_to_sigs/repo_state/. \
      /tmp/canonical-go-seed/
git -C /tmp/canonical-go-seed add -A
git -C /tmp/canonical-go-seed commit -m init
git -C /tmp/canonical-go-seed push origin HEAD:main
SHA=$(git -C /tmp/canonical-go-seed rev-parse HEAD)

# 2. Stage the changeset, REWRITING the placeholders.
mkdir -p /tmp/staged/canonical-go-1
cp tests/fixtures/changeset_examples/go_import_rewrite/ghodss_to_sigs/patch.diff \
   /tmp/staged/canonical-go-1/
jq --arg url file:///tmp/canonical-go-remote.git --arg sha "$SHA" \
   '.repo_url = $url | .commit_sha = $sha' \
   tests/fixtures/changeset_examples/go_import_rewrite/ghodss_to_sigs/meta.json \
   > /tmp/staged/canonical-go-1/meta.json

# 3. Drive the funnel.
python scripts/run_eval.py \
    --migration go_import_rewrite \
    --provider filesystem --root /tmp/staged \
    --eval-root /tmp/eval \
    --output-root runs/analysis/mig_go_import_rewrite/canonical/poc \
    --variant poc \
    --stages diff \
    canonical-go-1
```

`tests/test_run_eval.py::test_canonical_example_passes_tier0[go_import_rewrite]`
runs the same flow against an in-process seeded remote without the
manual rewrite — see `tests/conftest.py::seeded_go_import_remote`.
