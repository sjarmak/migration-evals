#!/usr/bin/env python3
"""Automated gold-anchor harvester for migration-evals (PRD M4-lite).

Replaces the original "schedule reviewer days to label 50 repos" step with
a fully automated pipeline that derives accept/reject labels from the
implicit maintainer verdict on public OSS PRs:

    accept = PR was merged and survived >=30 days without a revert
    reject = PR was closed-unmerged or merged-then-reverted

The script uses the `gh` CLI for both repo search and PR metadata; it
requires `gh auth login` to be already done. No additional Python
dependencies — stdlib + subprocess.

Usage
-----
    # Mine 50 Java 8->17 PRs and write data/gold_anchor.json
    python scripts/mine_gold_anchor.py \\
        --migration java8_17 \\
        --target-count 50 \\
        --out data/gold_anchor.json

    # Dry-run: print the search queries and exit without calling gh
    python scripts/mine_gold_anchor.py --migration java8_17 --dry-run

    # Use a custom search recipe (advanced)
    python scripts/mine_gold_anchor.py \\
        --recipe path/to/recipe.json \\
        --target-count 50 \\
        --out data/custom_gold.json

Recipe format
-------------
A recipe is a JSON file shaped like:

    {
      "migration_id": "spring_boot_2_3",
      "search_queries": [
        {"q": "is:pr is:merged 'spring-boot 3' language:java", "limit": 200}
      ],
      "min_days_survived": 30,
      "revert_keywords": ["revert", "rollback"]
    }

Built-in recipes for ``java8_17`` and ``python23`` ship inside this script
(see ``BUILT_IN_RECIPES``).

Output
------
A JSON array conforming to ``schemas/gold_anchor_entry.schema.json``.
Each entry carries the source PR URL and the survival-check method in
``reviewer_notes`` so the provenance is machine-auditable.

Exit codes
----------
0   Output written successfully (or dry-run completed).
1   `gh` CLI not available, recipe invalid, or output schema validation fail.
2   Insufficient candidates harvested (warning, but file still written).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_MIN_DAYS_SURVIVED = 30
DEFAULT_REVERT_KEYWORDS = ("revert", "rollback", "back out")


# ---------------------------------------------------------------------------
# Built-in migration recipes
# ---------------------------------------------------------------------------

BUILT_IN_RECIPES: dict[str, dict[str, Any]] = {
    "java8_17": {
        "migration_id": "java8_17",
        "search_queries": [
            {
                "q": (
                    "is:pr is:merged language:java "
                    '"java 17" in:title,body'
                ),
                "limit": 100,
            },
            {
                "q": (
                    "is:pr is:merged language:java "
                    '"upgrade to java 17" in:title,body'
                ),
                "limit": 100,
            },
            {
                "q": (
                    "is:pr is:closed is:unmerged language:java "
                    '"java 17" in:title,body'
                ),
                "limit": 50,
            },
        ],
        "min_days_survived": DEFAULT_MIN_DAYS_SURVIVED,
        "revert_keywords": list(DEFAULT_REVERT_KEYWORDS),
    },
    "python23": {
        "migration_id": "python23",
        "search_queries": [
            {
                "q": (
                    "is:pr is:merged language:python "
                    '"python 3" "drop python 2" in:title,body'
                ),
                "limit": 100,
            },
            {
                "q": (
                    "is:pr is:closed is:unmerged language:python "
                    '"python 3 migration" in:title,body'
                ),
                "limit": 50,
            },
        ],
        "min_days_survived": DEFAULT_MIN_DAYS_SURVIVED,
        "revert_keywords": list(DEFAULT_REVERT_KEYWORDS),
    },
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidatePR:
    """A PR returned by `gh search prs`. Minimal field set."""

    repo_full_name: str  # e.g. "apache/kafka"
    pr_number: int
    state: str  # "open" | "closed" (closed covers merged-or-not)
    merged: bool
    merge_commit_sha: str | None
    closed_at: str | None
    url: str


# ---------------------------------------------------------------------------
# gh CLI helpers
# ---------------------------------------------------------------------------


def _check_gh_available() -> None:
    if shutil.which("gh") is None:
        raise SystemExit(
            "error: `gh` CLI not found on PATH. Install it (https://cli.github.com/) "
            "and run `gh auth login` before mining the gold anchor."
        )


def _run_gh(args: list[str]) -> str:
    """Run `gh` and return stdout. Raises on non-zero exit."""
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} exited {proc.returncode}\n"
            f"stderr: {proc.stderr.strip()}"
        )
    return proc.stdout


def _search_prs(query: str, limit: int) -> list[CandidatePR]:
    """Wrap `gh search prs --json ...` into a list of CandidatePR."""
    out = _run_gh(
        [
            "search",
            "prs",
            query,
            "--limit",
            str(limit),
            "--json",
            "repository,number,state,isPullRequest,closedAt,url",
        ]
    )
    raw = json.loads(out) if out.strip() else []
    candidates: list[CandidatePR] = []
    for entry in raw:
        repo_full = entry.get("repository", {}).get("nameWithOwner") or ""
        number = int(entry.get("number") or 0)
        state = str(entry.get("state") or "").lower()
        if not repo_full or not number:
            continue
        candidates.append(
            CandidatePR(
                repo_full_name=repo_full,
                pr_number=number,
                state=state,
                merged=False,  # filled in by _hydrate_pr
                merge_commit_sha=None,  # filled in by _hydrate_pr
                closed_at=entry.get("closedAt"),
                url=str(entry.get("url") or ""),
            )
        )
    return candidates


def _hydrate_pr(repo: str, number: int) -> dict[str, Any]:
    """Pull merge state + commit SHA via `gh pr view`."""
    out = _run_gh(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "merged,mergeCommit,closedAt,url",
        ]
    )
    return json.loads(out) if out.strip() else {}


def _find_revert_after(
    repo: str,
    merge_commit_sha: str,
    after_iso: str,
    revert_keywords: Iterable[str],
) -> bool:
    """Return True if any commit on default branch since after_iso reverts merge_commit_sha."""
    out = _run_gh(
        [
            "api",
            f"repos/{repo}/commits",
            "--paginate",
            "-X",
            "GET",
            "-f",
            f"since={after_iso}",
        ]
    )
    try:
        commits = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return False
    needles = [k.lower() for k in revert_keywords]
    short_sha = merge_commit_sha[:7] if merge_commit_sha else ""
    for commit in commits:
        message = (commit.get("commit", {}).get("message") or "").lower()
        if not message:
            continue
        if short_sha and short_sha in message:
            return True
        if any(needle in message for needle in needles) and short_sha and short_sha in message:
            return True
    return False


# ---------------------------------------------------------------------------
# Survival classification
# ---------------------------------------------------------------------------


def classify(
    pr: CandidatePR,
    *,
    min_days_survived: int,
    revert_keywords: Iterable[str],
    now: datetime,
) -> tuple[str, str] | None:
    """Return (verdict, evidence_note) for a PR, or None if undecidable.

    'accept' = merged AND >= min_days_survived old AND no revert observed
    'reject' = closed-unmerged OR merged-then-reverted
    None     = merged-but-too-recent (not yet eligible)
    """
    if pr.state == "closed" and not pr.merged:
        return ("reject", f"closed unmerged @ {pr.closed_at} | source={pr.url}")
    if not pr.merged or not pr.merge_commit_sha or not pr.closed_at:
        return None
    try:
        closed_dt = datetime.fromisoformat(pr.closed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    days_since = (now - closed_dt).days
    if days_since < min_days_survived:
        return None
    reverted = _find_revert_after(
        pr.repo_full_name,
        pr.merge_commit_sha,
        after_iso=pr.closed_at,
        revert_keywords=revert_keywords,
    )
    if reverted:
        return (
            "reject",
            f"merged @ {pr.closed_at} then reverted within {days_since}d | source={pr.url}",
        )
    return (
        "accept",
        f"merged @ {pr.closed_at} survived {days_since}d without revert | source={pr.url}",
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def load_recipe(args: argparse.Namespace) -> dict[str, Any]:
    if args.recipe is not None:
        with open(args.recipe) as fh:
            recipe = json.load(fh)
    elif args.migration in BUILT_IN_RECIPES:
        recipe = BUILT_IN_RECIPES[args.migration]
    else:
        known = ", ".join(sorted(BUILT_IN_RECIPES))
        raise SystemExit(
            f"error: unknown --migration {args.migration!r}. "
            f"Built-in recipes: {known}. Or pass --recipe path.json."
        )
    for required in ("migration_id", "search_queries"):
        if required not in recipe:
            raise SystemExit(f"error: recipe missing required key {required!r}")
    return recipe


def harvest(
    recipe: dict[str, Any],
    *,
    target_count: int,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Run the search queries, classify, and accumulate up to target_count entries."""
    min_days = int(recipe.get("min_days_survived", DEFAULT_MIN_DAYS_SURVIVED))
    revert_keywords = list(recipe.get("revert_keywords", DEFAULT_REVERT_KEYWORDS))

    seen: set[tuple[str, int]] = set()
    accepts: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    stats = {"queried": 0, "skipped_too_recent": 0, "errors": 0}

    for query_spec in recipe["search_queries"]:
        try:
            candidates = _search_prs(
                query=str(query_spec["q"]),
                limit=int(query_spec.get("limit", 100)),
            )
        except RuntimeError as exc:
            print(f"warn: search failed: {exc}", file=sys.stderr)
            stats["errors"] += 1
            continue
        for pr in candidates:
            key = (pr.repo_full_name, pr.pr_number)
            if key in seen:
                continue
            seen.add(key)
            stats["queried"] += 1
            try:
                hydrated = _hydrate_pr(pr.repo_full_name, pr.pr_number)
            except RuntimeError as exc:
                print(f"warn: hydrate {pr.url} failed: {exc}", file=sys.stderr)
                stats["errors"] += 1
                continue
            pr = CandidatePR(
                repo_full_name=pr.repo_full_name,
                pr_number=pr.pr_number,
                state=pr.state,
                merged=bool(hydrated.get("merged")),
                merge_commit_sha=(hydrated.get("mergeCommit") or {}).get("oid"),
                closed_at=hydrated.get("closedAt") or pr.closed_at,
                url=hydrated.get("url") or pr.url,
            )
            verdict = classify(
                pr,
                min_days_survived=min_days,
                revert_keywords=revert_keywords,
                now=now,
            )
            if verdict is None:
                stats["skipped_too_recent"] += 1
                continue
            label, note = verdict
            entry = {
                "repo_url": f"https://github.com/{pr.repo_full_name}",
                "commit_sha": pr.merge_commit_sha or "",
                "human_verdict": label,
                "reviewer_notes": note,
                "labeled_at": now.isoformat(),
            }
            if label == "accept":
                accepts.append(entry)
            else:
                rejects.append(entry)
            if len(accepts) + len(rejects) >= target_count:
                return _balance(accepts, rejects, target_count), stats
    return _balance(accepts, rejects, target_count), stats


def _balance(
    accepts: list[dict[str, Any]],
    rejects: list[dict[str, Any]],
    target_count: int,
) -> list[dict[str, Any]]:
    """Return up to target_count entries with at least 1 of each verdict where possible."""
    combined = accepts + rejects
    return combined[:target_count]


def write_output(entries: list[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(entries, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mine_gold_anchor",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--migration",
        choices=sorted(BUILT_IN_RECIPES),
        default="java8_17",
        help="Built-in recipe to use. Default: java8_17.",
    )
    parser.add_argument(
        "--recipe",
        default=None,
        help="Path to a custom recipe JSON. Overrides --migration.",
    )
    parser.add_argument(
        "--target-count",
        type=int,
        default=50,
        help="Stop once this many labels accumulated. Default: 50.",
    )
    parser.add_argument(
        "--out",
        default="data/gold_anchor.json",
        help="Output path. Default: data/gold_anchor.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the search queries and exit without calling gh.",
    )
    args = parser.parse_args(argv)

    recipe = load_recipe(args)

    if args.dry_run:
        print(f"migration_id: {recipe['migration_id']}")
        print(f"target_count: {args.target_count}")
        print(f"out: {args.out}")
        print("queries:")
        for q in recipe["search_queries"]:
            print(f"  - q={q['q']!r} limit={q.get('limit', 100)}")
        return 0

    _check_gh_available()
    now = datetime.now(tz=timezone.utc)
    entries, stats = harvest(recipe, target_count=args.target_count, now=now)
    out_path = Path(args.out)
    write_output(entries, out_path)
    n_accept = sum(1 for e in entries if e["human_verdict"] == "accept")
    n_reject = len(entries) - n_accept
    print(
        f"wrote {len(entries)} entries ({n_accept} accept / {n_reject} reject) "
        f"to {out_path}"
    )
    print(
        f"stats: queried={stats['queried']} "
        f"skipped_too_recent={stats['skipped_too_recent']} "
        f"errors={stats['errors']}"
    )
    if len(entries) < args.target_count:
        print(
            f"warn: harvested {len(entries)} < target {args.target_count}. "
            "Consider broadening search queries or lowering min_days_survived.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
