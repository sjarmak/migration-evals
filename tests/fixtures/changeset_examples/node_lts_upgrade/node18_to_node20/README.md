# Node LTS upgrade — Node 18 → Node 20

Canonical example of a Node.js LTS-version upgrade batch change. The
agent bumps `engines.node` in `package.json` and rewrites a deprecated
`url.parse()` call to the WHATWG `URL` constructor, the textbook shape
of a Node-LTS-bump touching both project metadata and source.

## Files

| File | Purpose |
| --- | --- |
| `repo_state/` | Pre-patch project state. A two-file Node module: `package.json` declaring `engines.node: ">=18"` plus `index.js` calling the legacy `url.parse()` API. |
| `patch.diff` | Unified diff produced by the agent. Bumps `engines.node` to `">=20"` and replaces `url.parse(input).host` with `new URL(input).host`. |
| `meta.json` | `ChangesetProvider` metadata. The committed `commit_sha` is a placeholder; tests build a seeded git remote from `repo_state/` and substitute the real SHA at runtime. |

## What the funnel catches

| Tier | Verdict on this example | Failure mode this tier exists to catch |
| --- | --- | --- |
| 0 — `diff_valid` | **passes** | Malformed unified diff; line offsets that no longer match `repo_state/`. |
| 1 — `compile_only` (`npm ci`) | **passes** if Node 20+ is on PATH | Lockfile drift; `engines` enforcement under `--engine-strict`; native-addon ABI mismatches with the new Node ABI; packages dropped from the new Node's bundled deps. |
| 2 — `tests` (`npm test`) | **passes** if Node 20+ is on PATH | Runtime breakage from APIs the new LTS removed or changed semantics on (`url.parse`, `Buffer()` constructor, removed `crypto` algorithms, fetch/streams shifts, timer-promise behaviour). |

The shipped tests only exercise tier 0 (no Node toolchain assumed in
CI). A workstation with Node 20+ installed can run the higher tiers via
`scripts/run_eval.py --stages diff,compile,tests`.

## Reusing this example

> **The shipped `meta.json` is a template, not a runnable record.**
> `commit_sha` is `0000…0000` and `repo_url` points at an `example.com`
> placeholder. Both must be replaced with a real, clone-able remote
> whose state matches `repo_state/` before `run_eval.py` can drive
> this through the funnel against a live git remote. The shipped
> tests work around this by building a seeded git remote from
> `repo_state/` in-process.

`tests/test_run_eval.py::test_canonical_example_passes_tier0[node_lts_upgrade]`
runs the flow against an in-process seeded remote without the manual
rewrite — see `tests/conftest.py::seeded_node_lts_upgrade_remote`.
