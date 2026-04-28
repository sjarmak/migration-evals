"""Tests for the LLM-inferred harness synthesizer and its cache + drift.

Covers the "harness-synthesis" work unit acceptance criteria:

- ``Recipe`` round-trip JSON fidelity.
- ``content_hash`` stability and distinction across repos.
- Cache hit path makes **zero** adapter calls (cassette call_count stays
  flat across the second ``synthesize_recipe`` call).
- Cassette-driven batch: at least 3 of 5 fixture repos succeed.
- Provenance persisted alongside the recipe.
- Drift detector flags + evicts stale entries and writes valid audit lines.
- No direct ``import anthropic`` anywhere in the harness package.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from migration_evals.harness import cache as cache_mod  # noqa: E402
from migration_evals.harness.drift import revalidate  # noqa: E402
from migration_evals.harness.recipe import Recipe  # noqa: E402
from migration_evals.harness.synth import (  # noqa: E402
    HarnessSynthesisError,
    synthesize_recipe,
)

REPO_ROOT = _REPO_ROOT
FIXTURE_REPOS = REPO_ROOT / "tests" / "fixtures" / "java_maven_repos"
CASSETTE_DIR = REPO_ROOT / "tests" / "fixtures" / "harness_cassettes"
HARNESS_PKG = REPO_ROOT / "src" / "migration_evals" / "harness"


# -- Fake cassette adapter (test-only) ---------------------------------------


class FakeAnthropicCassette:
    """Replay-cassette implementation of the AnthropicAdapter Protocol.

    Responses are loaded from ``tests/fixtures/harness_cassettes/<hash>.json``
    where ``<hash>`` is the content hash of the repo being synthesized. The
    cassette tracks ``call_count`` so tests can assert cache-hit behavior.
    """

    def __init__(self, cassette_dir: Path, repo_path: Path) -> None:
        self._cassette_dir = cassette_dir
        self._repo_path = repo_path
        self.call_count = 0

    def messages_create(
        self,
        *,
        model: str,
        messages: Iterable[Mapping[str, Any]],
        system: str | None = None,
        max_tokens: int = 1024,
        cassette: Any = None,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        self.call_count += 1
        # Force consumption of the messages iterable so the orchestration
        # path exercises its prompt composition step.
        list(messages)
        key = cache_mod.content_hash(self._repo_path)
        cassette_path = self._cassette_dir / f"{key}.json"
        if not cassette_path.is_file():
            raise AssertionError(
                f"No cassette recorded for hash {key} " f"(looked for {cassette_path})"
            )
        return json.loads(cassette_path.read_text())


# -- Recipe dataclass --------------------------------------------------------


def _make_provenance(ts: str = "2026-04-24T00:00:00Z") -> dict[str, str]:
    return {"model": "claude-haiku-4-5", "prompt_version": "v1", "timestamp": ts}


def test_recipe_roundtrip() -> None:
    r = Recipe(
        dockerfile="FROM maven:3.9\n",
        build_cmd="mvn compile",
        test_cmd="mvn test",
        harness_provenance=_make_provenance(),
    )
    restored = Recipe.from_json(r.to_json())
    assert restored == r


def test_recipe_requires_provenance_keys() -> None:
    with pytest.raises(ValueError):
        Recipe(
            dockerfile="x",
            build_cmd="y",
            test_cmd="z",
            harness_provenance={"model": "only"},
        )


def test_recipe_from_json_rejects_missing_field() -> None:
    bad = json.dumps(
        {
            "dockerfile": "FROM x",
            "build_cmd": "b",
            # test_cmd missing
            "harness_provenance": _make_provenance(),
        }
    )
    with pytest.raises(ValueError):
        Recipe.from_json(bad)


# -- content_hash ------------------------------------------------------------


def test_content_hash_stable(tmp_path: Path) -> None:
    pom = "<project><groupId>g</groupId><artifactId>a</artifactId><version>1</version></project>\n"
    (tmp_path / "pom.xml").write_text(pom)
    first = cache_mod.content_hash(tmp_path)
    second = cache_mod.content_hash(tmp_path)
    assert first == second
    assert len(first) == 64


def test_content_hash_distinguishes(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    (repo_a / "pom.xml").write_text("<project>a</project>")
    (repo_b / "pom.xml").write_text("<project>b</project>")
    assert cache_mod.content_hash(repo_a) != cache_mod.content_hash(repo_b)


def test_content_hash_empty_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        cache_mod.content_hash(tmp_path)


# -- synthesize_recipe cache hit/miss ----------------------------------------


def test_synthesize_cache_miss_then_hit(tmp_path: Path) -> None:
    repo = FIXTURE_REPOS / "repo1"
    cassette = FakeAnthropicCassette(CASSETTE_DIR, repo)

    first = synthesize_recipe(repo, cassette, root=tmp_path)
    assert cassette.call_count == 1
    assert first.dockerfile.startswith("FROM maven")
    assert first.build_cmd == "mvn -B -e compile"
    assert first.test_cmd == "mvn -B -e test"

    second = synthesize_recipe(repo, cassette, root=tmp_path)
    # Critical: second call must NOT touch the adapter.
    assert cassette.call_count == 1
    assert second == first


def test_synthesize_persists_provenance(tmp_path: Path) -> None:
    repo = FIXTURE_REPOS / "repo2"
    cassette = FakeAnthropicCassette(CASSETTE_DIR, repo)
    recipe = synthesize_recipe(repo, cassette, root=tmp_path)

    key = cache_mod.content_hash(repo)
    stored = json.loads((tmp_path / key / cache_mod.RECIPE_FILE_NAME).read_text())
    assert "cached_at" in stored
    prov = stored["recipe"]["harness_provenance"]
    assert prov["model"] == "claude-haiku-4-5"
    assert prov["prompt_version"] == "v1"
    assert prov["timestamp"].endswith("Z")
    # Recipe object carries same provenance.
    assert recipe.harness_provenance["model"] == "claude-haiku-4-5"


def test_synthesize_strips_code_fences(tmp_path: Path) -> None:
    # repo2's cassette intentionally uses ```json fences.
    repo = FIXTURE_REPOS / "repo2"
    cassette = FakeAnthropicCassette(CASSETTE_DIR, repo)
    recipe = synthesize_recipe(repo, cassette, root=tmp_path)
    assert recipe.build_cmd == "mvn -B -e package"


def test_synthesize_raises_on_error_envelope(tmp_path: Path) -> None:
    repo = FIXTURE_REPOS / "repo4"  # cassette returns {"error": "..."}
    cassette = FakeAnthropicCassette(CASSETTE_DIR, repo)
    with pytest.raises(HarnessSynthesisError):
        synthesize_recipe(repo, cassette, root=tmp_path)
    # Nothing persisted on failure.
    key = cache_mod.content_hash(repo)
    assert not (tmp_path / key / cache_mod.RECIPE_FILE_NAME).exists()


def test_synthesize_batch_3_of_5_pass(tmp_path: Path) -> None:
    successes = 0
    failures = 0
    for i in range(1, 6):
        repo = FIXTURE_REPOS / f"repo{i}"
        cassette = FakeAnthropicCassette(CASSETTE_DIR, repo)
        try:
            synthesize_recipe(repo, cassette, root=tmp_path)
            successes += 1
        except HarnessSynthesisError:
            failures += 1
    assert successes >= 3, f"expected >=3 successes; got {successes} / {successes + failures}"
    assert failures == 5 - successes


# -- drift / TTL / audit log -------------------------------------------------


def _seed_entry(root: Path, repo: Path, age_days: int) -> str:
    cassette = FakeAnthropicCassette(CASSETTE_DIR, repo)
    synthesize_recipe(repo, cassette, root=root)
    key = cache_mod.content_hash(repo)
    backdated = datetime.now(timezone.utc) - timedelta(days=age_days)
    cache_mod.set_cached_at(key, root, backdated)
    return key


def test_ttl_expiry_evicts(tmp_path: Path) -> None:
    fresh_key = _seed_entry(tmp_path, FIXTURE_REPOS / "repo1", age_days=1)
    stale_key = _seed_entry(tmp_path, FIXTURE_REPOS / "repo2", age_days=30)

    report = revalidate(tmp_path, ttl_days=7)
    assert stale_key in report.stale_hashes
    assert stale_key in report.evicted
    assert fresh_key not in report.stale_hashes
    assert fresh_key not in report.evicted
    # Evicted entry's dir is gone.
    assert not (tmp_path / stale_key).exists()
    # Fresh entry survives.
    assert (tmp_path / fresh_key / cache_mod.RECIPE_FILE_NAME).is_file()
    # Timestamp format is ISO Z.
    assert report.timestamp.endswith("Z")


def test_evict_audit_log_format(tmp_path: Path) -> None:
    stale_key = _seed_entry(tmp_path, FIXTURE_REPOS / "repo3", age_days=90)
    revalidate(tmp_path, ttl_days=7)

    audit = (tmp_path / cache_mod.AUDIT_LOG_NAME).read_text().splitlines()
    assert len(audit) >= 1
    for line in audit:
        obj = json.loads(line)
        assert set(obj.keys()) == {"hash", "reason", "timestamp"}
        assert len(obj["hash"]) == 64
        assert obj["reason"] in {"ttl_expired", "missing_timestamp", "rebuild_failed"}
        assert obj["timestamp"].endswith("Z")
    # At least one audit line references the stale key.
    hashes = [json.loads(line)["hash"] for line in audit]
    assert stale_key in hashes


def test_drift_empty_root_is_noop(tmp_path: Path) -> None:
    report = revalidate(tmp_path, ttl_days=7)
    assert report.stale_hashes == []
    assert report.evicted == []


def test_drift_rebuild_check_can_evict(tmp_path: Path) -> None:
    _seed_entry(tmp_path, FIXTURE_REPOS / "repo1", age_days=0)
    key = cache_mod.content_hash(FIXTURE_REPOS / "repo1")
    report = revalidate(tmp_path, ttl_days=7, rebuild_check=lambda r: False)
    assert key in report.evicted
    assert key not in report.stale_hashes


# -- no direct anthropic import ----------------------------------------------


def test_no_direct_anthropic_import_in_harness() -> None:
    result = subprocess.run(
        ["grep", "-R", "-n", "-E", r"^\s*(import anthropic|from anthropic )", str(HARNESS_PKG)],
        capture_output=True,
        text=True,
    )
    # grep exits 1 when no matches - that is the pass condition.
    assert result.returncode == 1, (
        f"expected zero 'import anthropic' matches in harness/. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stdout == ""
