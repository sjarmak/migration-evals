# Harness Synthesis (PRD M2 + D5)

The migration-eval framework evaluates repos that may lack a ready-made
Dockerfile or `test.sh`. `migration_evals.harness` fills that gap by
asking a Haiku-class model to emit a build/test recipe from a repo's
manifest, CI, and README. Recipes are cached on disk under
`runs/analysis/_harnesses/<content-hash>/recipe.json` so a second eval on
the same repo costs zero tokens.

## Recipe schema

Every cached recipe is a JSON document with two top-level keys:

```json
{
  "recipe": {
    "dockerfile": "FROM maven:3.9\n...",
    "build_cmd": "mvn -B -e compile",
    "test_cmd": "mvn -B -e test",
    "harness_provenance": {
      "model": "claude-haiku-4-5",
      "prompt_version": "v1",
      "timestamp": "2026-04-24T17:05:33Z"
    }
  },
  "cached_at": "2026-04-24T17:05:33Z"
}
```

- `recipe.dockerfile` / `build_cmd` / `test_cmd` — strings consumed by the
  container runtime.
- `recipe.harness_provenance` — append-only metadata. The three keys
  `model`, `prompt_version`, and `timestamp` (ISO-8601 UTC, `Z`-suffixed)
  are required; extra keys are allowed and forwarded verbatim.
- `cached_at` — written by the cache layer, separate from provenance so
  drift-detector TTL decisions never alter the synthesis record.

## Content-hash keying

`cache.content_hash(repo_path)` SHA-256s the concatenation of manifest file
bytes in a **canonical order**:

`pom.xml`, `build.gradle`, `build.gradle.kts`, `settings.gradle`,
`settings.gradle.kts`, `setup.py`, `setup.cfg`, `pyproject.toml`,
`requirements.txt`, `package.json`, `Cargo.toml`, `go.mod`

Each contributing file is prefixed with `<filename>\0` before its bytes are
hashed, so moving identical content between filenames always changes the
hash. Files that do not exist are skipped. A repo with none of these files
raises `ValueError`; it cannot be cached because there is nothing to key
on.

## Replay cassettes and determinism

`synthesize_recipe` accepts any object satisfying the
`AnthropicAdapter` Protocol from `migration_evals.adapters`. Tests
inject a `FakeAnthropicCassette` that returns pre-recorded JSON envelopes
keyed on the repo's content hash. The cassette also tracks a `call_count`
attribute so tests can assert **zero adapter calls** after a cache hit —
this is the guarantee that makes cost-bounded re-runs safe.

## Failure handling and auto-quarantine

`synthesize_recipe` raises `HarnessSynthesisError` when any of the
following hold:

1. The adapter returns an envelope containing an `error` key.
2. The `content` list is missing, empty, or malformed.
3. The first content block's `text` is empty or non-string.
4. The parsed JSON is not a single object.
5. One of `dockerfile`, `build_cmd`, `test_cmd` is missing or empty.

When the caller catches `HarnessSynthesisError`, the expected remediation
is to **auto-quarantine** the repo: move it from the active task manifest
into `runs/analysis/_quarantine/<repo>/` with a `reason.txt` that includes
the raised message, and emit a `harness_synthesis_failed` failure class on
the next ledger write. No recipe is persisted and no audit-log entry is
written — failed syntheses leave the cache untouched so the next pipeline
run can retry cleanly once the underlying cause (e.g. a malformed
`pom.xml`) is fixed.

## Drift detection (`drift.revalidate`)

A weekly cron invokes `revalidate(harness_root, ttl_days=7)`:

- Walks every `<hash>/recipe.json` entry.
- Flags entries whose `cached_at` is older than `ttl_days` as **stale**.
- Calls `cache.evict(hash, root, reason="ttl_expired")` on each stale
  entry, which (a) deletes the cache directory and (b) appends a JSON
  line to `runs/analysis/_harnesses/_audit.log`.

The future full drift check will also rebuild the Dockerfile and compare
the image digest; for now `_rebuild_ok` is a stub that always returns
`True`. A `rebuild_check` callable can be passed to `revalidate` for tests
or experimental deployments.

## Audit log format

`runs/analysis/_harnesses/_audit.log` is append-only, one JSON object per
line:

```json
{"hash": "abc123...", "reason": "ttl_expired", "timestamp": "2026-04-24T17:05:33Z"}
```

The file is never rotated by the drift detector itself; operational
pipelines are responsible for archiving it if size becomes an issue.

## Guardrail: no direct SDK imports

`src/migration_evals/harness/` MUST NOT import the vendor Anthropic
SDK directly. All vendor calls flow through the `AnthropicAdapter`
Protocol so that swapping providers (or replaying via cassette) touches
only the adapter layer. A dedicated test asserts a recursive grep for
`import anthropic` returns zero matches inside the package.
