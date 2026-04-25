"""Agent-changeset provider interface.

The tiered-oracle funnel evaluates *agent-produced diffs*. Pulling those
diffs out of whatever the agent pipeline uses for artifact storage is
the job of a :class:`ChangesetProvider`: given an instance id, return a
:class:`Changeset` carrying the base commit, the unified diff, and
provenance (agent runner, agent model, workflow id).

Only the :class:`FilesystemChangesetProvider` implementation ships
in-repo. It reads from a local directory laid out as::

    <root>/<instance_id>/meta.json
    <root>/<instance_id>/patch.diff

``meta.json`` must contain the keys listed in
:data:`_REQUIRED_META_KEYS`. The format is intentionally minimal so any
backend (S3-compatible object store, blob storage, HTTP artifact server)
can stage a directory on disk and hand the path to this provider.

Production backends that pull from non-filesystem storage implement
:class:`ChangesetProvider` alongside the consuming pipeline; extend
:func:`get_provider` to wire them in.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

_REQUIRED_META_KEYS: tuple[str, ...] = (
    "repo_url",
    "commit_sha",
    "workflow_id",
    "agent_runner",
    "agent_model",
)

# Instance ids become path components and arrive from external pipelines;
# constrain to a conservative alphanumeric + .-_ alphabet to defeat
# traversal (`../`) and absolute-path inputs.
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def validate_instance_id(instance_id: str) -> None:
    """Raise ValueError if ``instance_id`` is unsafe to use as a path component."""
    if not isinstance(instance_id, str) or not _INSTANCE_ID_RE.fullmatch(instance_id):
        raise ValueError(
            f"unsafe instance_id {instance_id!r}: must match {_INSTANCE_ID_RE.pattern}"
        )


def validate_commit_sha(commit_sha: str) -> None:
    """Raise ValueError if ``commit_sha`` is not a 40-char lowercase hex SHA-1."""
    if not isinstance(commit_sha, str) or not _SHA1_RE.fullmatch(commit_sha):
        raise ValueError(
            f"commit_sha must be a 40-char lowercase hex SHA-1; got {commit_sha!r}"
        )


@dataclass(frozen=True)
class Changeset:
    """An agent-produced diff plus the provenance needed to reproduce it."""

    instance_id: str
    repo_url: str
    commit_sha: str
    patch_diff: str
    workflow_id: str
    agent_runner: str
    agent_model: str


@runtime_checkable
class ChangesetProvider(Protocol):
    """Minimum surface for fetching an agent changeset by instance id."""

    def fetch(self, instance_id: str) -> Changeset:
        """Return the changeset for ``instance_id`` or raise on missing."""
        ...


class FilesystemChangesetProvider:
    """Read changesets from a local directory tree.

    Expected layout::

        <root>/<instance_id>/meta.json
        <root>/<instance_id>/patch.diff
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def fetch(self, instance_id: str) -> Changeset:
        validate_instance_id(instance_id)
        inst_dir = self._root / instance_id
        if not inst_dir.is_dir():
            raise FileNotFoundError(
                f"no changeset directory for instance {instance_id!r} under {self._root}"
            )
        meta_path = inst_dir / "meta.json"
        patch_path = inst_dir / "patch.diff"
        if not meta_path.is_file():
            raise FileNotFoundError(f"missing meta.json for {instance_id!r} at {meta_path}")
        if not patch_path.is_file():
            raise FileNotFoundError(f"missing patch.diff for {instance_id!r} at {patch_path}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        for key in _REQUIRED_META_KEYS:
            if key not in meta:
                raise KeyError(
                    f"meta.json for {instance_id!r} is missing required key {key!r}"
                )
        commit_sha = str(meta["commit_sha"])
        validate_commit_sha(commit_sha)
        return Changeset(
            instance_id=instance_id,
            repo_url=str(meta["repo_url"]),
            commit_sha=commit_sha,
            patch_diff=patch_path.read_text(encoding="utf-8"),
            workflow_id=str(meta["workflow_id"]),
            agent_runner=str(meta["agent_runner"]),
            agent_model=str(meta["agent_model"]),
        )


_KNOWN_PROVIDERS: tuple[str, ...] = ("filesystem",)


def get_provider(name: str, config: Mapping[str, Any]) -> ChangesetProvider:
    """Return a provider instance for ``name`` with ``config``.

    Raises :class:`ValueError` for an unknown provider; raises
    :class:`KeyError` if the named provider's required config keys are
    missing.
    """
    if name == "filesystem":
        if "root" not in config:
            raise KeyError("filesystem provider requires config key 'root'")
        return FilesystemChangesetProvider(config["root"])
    raise ValueError(
        f"unknown provider {name!r}; known providers: {', '.join(_KNOWN_PROVIDERS)}"
    )
