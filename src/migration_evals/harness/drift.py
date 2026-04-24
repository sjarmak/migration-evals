"""Weekly drift detector for synthesized harness recipes (PRD D5).

``revalidate()`` walks ``<harness_root>/<hash>/recipe.json`` entries, flags
any whose ``cached_at`` timestamp is older than ``ttl_days``, and evicts
them via :func:`migration_evals.harness.cache.evict`. The eviction
path writes a JSON line to ``_audit.log`` so a historical record survives
the directory removal.

A full version of this unit will additionally run the recipe's Dockerfile
build to catch silent drift (e.g. base-image tags that stop pulling) — that
rebuild step is represented here by :func:`_rebuild_ok`, which returns
``True`` for now. Callers depending on the rebuild signal should override
``rebuild_ok`` via the ``rebuild_check`` parameter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from migration_evals.harness import cache as cache_mod
from migration_evals.harness.recipe import Recipe


@dataclass(frozen=True)
class DriftReport:
    """Summary of a :func:`revalidate` pass."""

    stale_hashes: list[str] = field(default_factory=list)
    evicted: list[str] = field(default_factory=list)
    timestamp: str = ""


def _rebuild_ok(recipe: Recipe) -> bool:  # noqa: ARG001 — stub for future work
    """Placeholder: a future unit will actually rebuild the image here."""
    return True


def _iter_cache_entries(harness_root: Path) -> list[str]:
    if not harness_root.is_dir():
        return []
    hashes: list[str] = []
    for child in sorted(harness_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_"):
            continue
        if (child / cache_mod.RECIPE_FILE_NAME).is_file():
            hashes.append(child.name)
    return hashes


def revalidate(
    harness_root: Path,
    ttl_days: int = 7,
    *,
    rebuild_check: Optional[Callable[[Recipe], bool]] = None,
) -> DriftReport:
    """Flag and evict cache entries older than ``ttl_days``.

    ``rebuild_check`` is reserved for the future rebuild-based drift check;
    when provided and returning ``False`` the entry is also evicted with
    reason ``rebuild_failed``. Stale entries use reason ``ttl_expired``.
    """
    if ttl_days < 0:
        raise ValueError(f"ttl_days must be non-negative, got {ttl_days}")

    check = rebuild_check or _rebuild_ok
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ttl_days)

    stale: list[str] = []
    evicted: list[str] = []

    for entry_hash in _iter_cache_entries(harness_root):
        stamp = cache_mod.cached_at(entry_hash, harness_root)
        if stamp is None:
            # Corrupt entry — evict defensively.
            cache_mod.evict(entry_hash, harness_root, reason="missing_timestamp")
            stale.append(entry_hash)
            evicted.append(entry_hash)
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)

        if stamp < cutoff:
            stale.append(entry_hash)
            cache_mod.evict(entry_hash, harness_root, reason="ttl_expired")
            evicted.append(entry_hash)
            continue

        recipe = cache_mod.lookup(entry_hash, harness_root)
        if recipe is not None and not check(recipe):
            cache_mod.evict(entry_hash, harness_root, reason="rebuild_failed")
            evicted.append(entry_hash)

    return DriftReport(
        stale_hashes=stale,
        evicted=evicted,
        timestamp=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


__all__ = ["DriftReport", "revalidate"]
