"""Content-hashed cache for synthesized harness recipes (PRD M2).

Cache entries live under ``runs/analysis/_harnesses/<content-hash>/recipe.json``.
The content hash is a SHA-256 over the concatenated bytes of a canonical,
ordered list of manifest files present in the repo (``pom.xml``,
``build.gradle``, ``setup.py``, ``pyproject.toml``, etc.). Each file's
contents are prefixed with a ``<filename>\\0`` marker so that identical
content under different filenames does not collide.

Cache artifacts contain the serialized :class:`~migration_evals.harness.recipe.Recipe`
plus a ``cached_at`` ISO-8601 UTC timestamp used by the drift detector.
Evictions are recorded as single-line JSON entries in ``_audit.log`` inside
the cache root so we have a permanent audit trail even after the cache dir
itself is removed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Optional

from migration_evals.harness.recipe import Recipe

MANIFEST_FILENAMES: Final[tuple[str, ...]] = (
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "Cargo.toml",
    "go.mod",
)

AUDIT_LOG_NAME: Final[str] = "_audit.log"
RECIPE_FILE_NAME: Final[str] = "recipe.json"


def _utcnow_iso() -> str:
    """Return the current UTC instant in ISO-8601 format with a ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def content_hash(repo_path: Path) -> str:
    """Compute a stable SHA-256 over a repo's manifest files.

    Only filenames listed in :data:`MANIFEST_FILENAMES` contribute. Each
    contributing file's bytes are preceded by ``<filename>\\0`` so that
    moving identical bytes between filenames changes the hash. Files that do
    not exist are silently skipped.

    Raises :class:`ValueError` if no manifest files are found — an empty
    repo cannot be cached because there is nothing to key on.
    """
    h = hashlib.sha256()
    found: list[str] = []
    for name in MANIFEST_FILENAMES:
        target = repo_path / name
        if target.is_file():
            h.update(name.encode("utf-8"))
            h.update(b"\0")
            h.update(target.read_bytes())
            h.update(b"\0")
            found.append(name)
    if not found:
        raise ValueError(
            f"No manifest files found under {repo_path}; "
            f"expected at least one of {list(MANIFEST_FILENAMES)}"
        )
    return h.hexdigest()


def _entry_dir(content_hash_value: str, root: Path) -> Path:
    return root / content_hash_value


def lookup(content_hash_value: str, root: Path) -> Optional[Recipe]:
    """Return the cached recipe for a hash, or ``None`` if absent/corrupt.

    A corrupt cache entry (unreadable JSON, missing fields) is treated as a
    miss rather than raising; callers resynthesize naturally.
    """
    recipe_path = _entry_dir(content_hash_value, root) / RECIPE_FILE_NAME
    if not recipe_path.is_file():
        return None
    try:
        raw = json.loads(recipe_path.read_text())
        recipe_payload = raw["recipe"] if "recipe" in raw else raw
        return Recipe.from_json(json.dumps(recipe_payload))
    except (OSError, ValueError, KeyError):
        return None


def store(content_hash_value: str, recipe: Recipe, root: Path) -> Path:
    """Persist a recipe under ``<root>/<hash>/recipe.json`` and return the path."""
    entry = _entry_dir(content_hash_value, root)
    entry.mkdir(parents=True, exist_ok=True)
    recipe_path = entry / RECIPE_FILE_NAME
    payload = {
        "recipe": json.loads(recipe.to_json()),
        "cached_at": _utcnow_iso(),
    }
    recipe_path.write_text(json.dumps(payload, sort_keys=True, indent=2))
    return recipe_path


def cached_at(content_hash_value: str, root: Path) -> Optional[datetime]:
    """Return the ``cached_at`` timestamp of a cache entry, or ``None``."""
    recipe_path = _entry_dir(content_hash_value, root) / RECIPE_FILE_NAME
    if not recipe_path.is_file():
        return None
    try:
        raw = json.loads(recipe_path.read_text())
    except (OSError, ValueError):
        return None
    stamp = raw.get("cached_at")
    if not isinstance(stamp, str):
        return None
    # Accept both "...Z" and offset-aware ISO strings.
    try:
        if stamp.endswith("Z"):
            return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def set_cached_at(content_hash_value: str, root: Path, moment: datetime) -> None:
    """Overwrite the ``cached_at`` timestamp in place.

    Used by tests and by the drift detector's backdating helpers. Raises
    :class:`FileNotFoundError` if the entry does not exist.
    """
    recipe_path = _entry_dir(content_hash_value, root) / RECIPE_FILE_NAME
    raw = json.loads(recipe_path.read_text())
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    raw["cached_at"] = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    recipe_path.write_text(json.dumps(raw, sort_keys=True, indent=2))


def evict(content_hash_value: str, root: Path, reason: str) -> None:
    """Delete a cache entry and append an audit line.

    The audit log format is one JSON object per line with keys
    ``hash``, ``reason``, ``timestamp``. The directory is removed
    unconditionally; missing entries are tolerated so the call is idempotent.
    """
    root.mkdir(parents=True, exist_ok=True)
    entry = _entry_dir(content_hash_value, root)
    if entry.is_dir():
        shutil.rmtree(entry)
    audit_line = json.dumps(
        {
            "hash": content_hash_value,
            "reason": reason,
            "timestamp": _utcnow_iso(),
        },
        sort_keys=True,
    )
    audit_path = root / AUDIT_LOG_NAME
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(audit_line + "\n")


__all__ = [
    "MANIFEST_FILENAMES",
    "AUDIT_LOG_NAME",
    "RECIPE_FILE_NAME",
    "content_hash",
    "lookup",
    "store",
    "cached_at",
    "set_cached_at",
    "evict",
]
