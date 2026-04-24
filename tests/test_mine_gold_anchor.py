"""Unit tests for scripts/mine_gold_anchor.py.

These cover the pure-functional surface (recipe loading, verdict
classification, balancing) so the script's logic stays testable without a
network round-trip to the GitHub API. Network-dependent helpers
(_search_prs, _hydrate_pr, _find_revert_after) are integration-tested
manually via the `--dry-run` path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = _REPO_ROOT / "scripts" / "mine_gold_anchor.py"


def _load_module():
    """Import scripts/mine_gold_anchor.py as a module under a stable name."""
    spec = importlib.util.spec_from_file_location("mine_gold_anchor", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["mine_gold_anchor"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mga():
    return _load_module()


@pytest.fixture()
def now() -> datetime:
    return datetime(2026, 4, 24, tzinfo=timezone.utc)


# -- classify ---------------------------------------------------------------


def test_classify_returns_accept_for_old_merged_pr(mga, now, monkeypatch):
    monkeypatch.setattr(mga, "_find_revert_after", lambda *a, **k: False)
    pr = mga.CandidatePR(
        repo_full_name="org/repo",
        pr_number=1,
        state="closed",
        merged=True,
        merge_commit_sha="abc1234",
        closed_at=(now - timedelta(days=60)).isoformat(),
        url="https://github.com/org/repo/pull/1",
    )
    verdict = mga.classify(pr, min_days_survived=30, revert_keywords=["revert"], now=now)
    assert verdict is not None
    label, note = verdict
    assert label == "accept"
    assert "60d without revert" in note
    assert "https://github.com/org/repo/pull/1" in note


def test_classify_returns_reject_for_closed_unmerged(mga, now):
    pr = mga.CandidatePR(
        repo_full_name="org/repo",
        pr_number=2,
        state="closed",
        merged=False,
        merge_commit_sha=None,
        closed_at=(now - timedelta(days=10)).isoformat(),
        url="https://github.com/org/repo/pull/2",
    )
    verdict = mga.classify(pr, min_days_survived=30, revert_keywords=["revert"], now=now)
    assert verdict is not None
    assert verdict[0] == "reject"
    assert "closed unmerged" in verdict[1]


def test_classify_returns_none_for_recent_merge(mga, now):
    pr = mga.CandidatePR(
        repo_full_name="org/repo",
        pr_number=3,
        state="closed",
        merged=True,
        merge_commit_sha="def5678",
        closed_at=(now - timedelta(days=5)).isoformat(),
        url="https://github.com/org/repo/pull/3",
    )
    verdict = mga.classify(pr, min_days_survived=30, revert_keywords=["revert"], now=now)
    assert verdict is None  # too recent — defer until next harvest


def test_classify_returns_reject_when_revert_observed(mga, now, monkeypatch):
    monkeypatch.setattr(mga, "_find_revert_after", lambda *a, **k: True)
    pr = mga.CandidatePR(
        repo_full_name="org/repo",
        pr_number=4,
        state="closed",
        merged=True,
        merge_commit_sha="cafe1234",
        closed_at=(now - timedelta(days=45)).isoformat(),
        url="https://github.com/org/repo/pull/4",
    )
    verdict = mga.classify(pr, min_days_survived=30, revert_keywords=["revert"], now=now)
    assert verdict is not None
    assert verdict[0] == "reject"
    assert "reverted" in verdict[1]


# -- recipe loading ---------------------------------------------------------


def test_load_recipe_built_in(mga):
    args = type("Args", (), {"recipe": None, "migration": "java8_17"})()
    recipe = mga.load_recipe(args)
    assert recipe["migration_id"] == "java8_17"
    assert recipe["search_queries"]


def test_load_recipe_unknown_migration_exits(mga):
    args = type("Args", (), {"recipe": None, "migration": "cobol_to_rust"})()
    with pytest.raises(SystemExit) as exc_info:
        mga.load_recipe(args)
    assert "unknown" in str(exc_info.value).lower()


def test_load_recipe_custom_file(mga, tmp_path):
    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_text(
        json.dumps(
            {
                "migration_id": "spring_boot_2_3",
                "search_queries": [{"q": "is:pr is:merged 'spring boot 3'", "limit": 50}],
                "min_days_survived": 30,
            }
        )
    )
    args = type("Args", (), {"recipe": str(recipe_path), "migration": "java8_17"})()
    recipe = mga.load_recipe(args)
    assert recipe["migration_id"] == "spring_boot_2_3"


def test_load_recipe_missing_required_key_exits(mga, tmp_path):
    recipe_path = tmp_path / "bad.json"
    recipe_path.write_text(json.dumps({"migration_id": "x"}))  # no search_queries
    args = type("Args", (), {"recipe": str(recipe_path), "migration": "java8_17"})()
    with pytest.raises(SystemExit) as exc_info:
        mga.load_recipe(args)
    assert "search_queries" in str(exc_info.value)


# -- write_output validates against the JSON schema -------------------------


def test_write_output_produces_schema_valid_json(mga, tmp_path, now):
    entries = [
        {
            "repo_url": "https://github.com/org/repo",
            "commit_sha": "abc1234567890",
            "human_verdict": "accept",
            "reviewer_notes": "merged @ 2026-01-01 survived 90d | source=https://github.com/org/repo/pull/1",
            "labeled_at": now.isoformat(),
        },
        {
            "repo_url": "https://github.com/org/repo2",
            "commit_sha": "",
            "human_verdict": "reject",
            "reviewer_notes": "closed unmerged @ 2026-02-01 | source=https://github.com/org/repo2/pull/2",
            "labeled_at": now.isoformat(),
        },
    ]
    out_path = tmp_path / "gold.json"
    mga.write_output(entries, out_path)

    schema_path = _REPO_ROOT / "schemas" / "gold_anchor_entry.schema.json"
    schema = json.loads(schema_path.read_text())
    try:
        import jsonschema
    except ImportError:
        pytest.skip("jsonschema not installed")

    written = json.loads(out_path.read_text())
    assert isinstance(written, list)
    for entry in written:
        jsonschema.validate(entry, schema)


# -- balance preserves target_count and ordering ---------------------------


def test_balance_truncates_to_target_count(mga):
    accepts = [{"id": f"a{i}"} for i in range(40)]
    rejects = [{"id": f"r{i}"} for i in range(40)]
    out = mga._balance(accepts, rejects, target_count=10)
    assert len(out) == 10
    # Accepts come first by construction.
    assert all(entry["id"].startswith("a") for entry in out)


def test_balance_returns_all_when_under_target(mga):
    accepts = [{"id": "a1"}]
    rejects = [{"id": "r1"}]
    out = mga._balance(accepts, rejects, target_count=10)
    assert len(out) == 2


# -- parse_pr_url -----------------------------------------------------------


def test_parse_pr_url_basic(mga):
    repo, num = mga.parse_pr_url("https://github.com/foo/bar/pull/42")
    assert repo == "foo/bar"
    assert num == 42


def test_parse_pr_url_handles_trailing_slash_and_query(mga):
    repo, num = mga.parse_pr_url("https://github.com/foo/bar/pull/42/?ref=x#diff")
    assert repo == "foo/bar"
    assert num == 42


def test_parse_pr_url_rejects_non_pr_url(mga):
    with pytest.raises(ValueError):
        mga.parse_pr_url("https://github.com/foo/bar/issues/42")


def test_parse_pr_url_rejects_non_url(mga):
    with pytest.raises(ValueError):
        mga.parse_pr_url("not a url at all")


# -- load_changeset_urls ----------------------------------------------------


def test_load_changeset_urls_skips_blanks_comments_header(mga, tmp_path):
    csv = tmp_path / "urls.csv"
    csv.write_text(
        "pr_url\n"
        "# this is a comment\n"
        "\n"
        "https://github.com/a/b/pull/1\n"
        "https://github.com/c/d/pull/2,extra,metadata,fields\n"
    )
    urls = mga.load_changeset_urls(csv)
    assert urls == [
        "https://github.com/a/b/pull/1",
        "https://github.com/c/d/pull/2",
    ]


def test_load_changeset_urls_returns_empty_for_empty_file(mga, tmp_path):
    csv = tmp_path / "empty.csv"
    csv.write_text("")
    assert mga.load_changeset_urls(csv) == []


# -- harvest_from_changesets ------------------------------------------------


def test_harvest_from_changesets_classifies_via_hydrate(mga, monkeypatch, now):
    """End-to-end harvest with mocked gh hydrate + revert check."""
    closed_old = (now - timedelta(days=60)).isoformat()
    closed_recent = (now - timedelta(days=5)).isoformat()

    hydrate_responses = {
        ("orgA/repo", 1): {
            "merged": True,
            "mergeCommit": {"oid": "abc1234"},
            "closedAt": closed_old,
            "url": "https://github.com/orgA/repo/pull/1",
        },
        ("orgA/repo", 2): {
            "merged": False,
            "mergeCommit": None,
            "closedAt": closed_old,
            "url": "https://github.com/orgA/repo/pull/2",
        },
        ("orgB/repo", 3): {
            "merged": True,
            "mergeCommit": {"oid": "def5678"},
            "closedAt": closed_recent,
            "url": "https://github.com/orgB/repo/pull/3",
        },
    }

    def fake_hydrate(repo, number):
        return hydrate_responses[(repo, number)]

    monkeypatch.setattr(mga, "_hydrate_pr", fake_hydrate)
    monkeypatch.setattr(mga, "_find_revert_after", lambda *a, **k: False)

    urls = [
        "https://github.com/orgA/repo/pull/1",  # accept (merged old, no revert)
        "https://github.com/orgA/repo/pull/2",  # reject (closed unmerged)
        "https://github.com/orgB/repo/pull/3",  # skip (merged but too recent)
    ]
    entries, stats = mga.harvest_from_changesets(
        urls,
        target_count=50,
        min_days_survived=30,
        revert_keywords=["revert"],
        now=now,
    )
    verdicts = [e["human_verdict"] for e in entries]
    assert verdicts == ["accept", "reject"]
    assert stats["queried"] == 3
    assert stats["skipped_too_recent"] == 1
    assert stats["unparseable_urls"] == 0


def test_harvest_from_changesets_dedupes_and_skips_unparseable(mga, monkeypatch, now):
    monkeypatch.setattr(
        mga,
        "_hydrate_pr",
        lambda repo, number: {
            "merged": True,
            "mergeCommit": {"oid": "abc1234"},
            "closedAt": (now - timedelta(days=60)).isoformat(),
            "url": f"https://github.com/{repo}/pull/{number}",
        },
    )
    monkeypatch.setattr(mga, "_find_revert_after", lambda *a, **k: False)

    urls = [
        "https://github.com/foo/bar/pull/1",
        "https://github.com/foo/bar/pull/1",  # duplicate
        "not a url",
        "https://github.com/foo/bar/issues/2",  # not a PR url
    ]
    entries, stats = mga.harvest_from_changesets(
        urls,
        target_count=50,
        min_days_survived=30,
        revert_keywords=["revert"],
        now=now,
    )
    assert len(entries) == 1
    assert stats["queried"] == 1
    assert stats["unparseable_urls"] == 2


def test_harvest_from_changesets_stops_at_target_count(mga, monkeypatch, now):
    monkeypatch.setattr(
        mga,
        "_hydrate_pr",
        lambda repo, number: {
            "merged": True,
            "mergeCommit": {"oid": f"sha{number:04x}"},
            "closedAt": (now - timedelta(days=60)).isoformat(),
            "url": f"https://github.com/{repo}/pull/{number}",
        },
    )
    monkeypatch.setattr(mga, "_find_revert_after", lambda *a, **k: False)

    urls = [f"https://github.com/foo/bar/pull/{i}" for i in range(1, 21)]
    entries, _stats = mga.harvest_from_changesets(
        urls,
        target_count=5,
        min_days_survived=30,
        revert_keywords=["revert"],
        now=now,
    )
    assert len(entries) == 5
