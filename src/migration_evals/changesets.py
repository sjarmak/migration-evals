"""Agent-changeset provider interface.

The tiered-oracle funnel evaluates *agent-produced diffs*. Pulling those
diffs out of whatever the agent pipeline uses for artifact storage is
the job of a :class:`ChangesetProvider`: given an instance id, return a
:class:`Changeset` carrying the base commit, the unified diff, and
provenance (agent runner, agent model, workflow id).

Two implementations ship in-repo:

* :class:`FilesystemChangesetProvider` reads from a local directory tree
  laid out as ``<root>/<instance_id>/{meta.json, patch.diff}``. Used by
  fixtures, tests, and pre-staged corpora.
* :class:`HTTPChangesetProvider` reads the same layout from an HTTP
  artifact server (``GET <base_url>/<instance_id>/meta.json`` and
  ``GET <base_url>/<instance_id>/patch.diff``). Reference template for
  teams whose artifacts live behind an HTTP endpoint; copy-modify for
  authenticated or non-public stores.

``meta.json`` must contain the keys listed in
:data:`_REQUIRED_META_KEYS`. The format is intentionally minimal so any
backend (S3-compatible object store, blob storage, HTTP artifact server)
can stage a directory on disk and hand the path to this provider.

Production backends that pull from non-filesystem storage implement the
:class:`ChangesetProvider` Protocol alongside the consuming pipeline and
register a factory via :func:`register_provider` before calling
:func:`get_provider`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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


def _build_changeset(
    instance_id: str, meta_text: str, patch_diff: str, *, source: str
) -> Changeset:
    """Parse meta-bytes, validate the required keys, and return a Changeset.

    Shared between the filesystem and HTTP providers so a third backend
    only has to bring `meta_text` and `patch_diff` to the table.
    `source` is woven into error messages to point operators at the
    failing locator (filesystem path or URL).
    """
    try:
        meta = json.loads(meta_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"meta.json for {instance_id!r} at {source} is not valid JSON: {exc}"
        ) from exc
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
        patch_diff=patch_diff,
        workflow_id=str(meta["workflow_id"]),
        agent_runner=str(meta["agent_runner"]),
        agent_model=str(meta["agent_model"]),
    )


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
        return _build_changeset(
            instance_id,
            meta_text=meta_path.read_text(encoding="utf-8"),
            patch_diff=patch_path.read_text(encoding="utf-8"),
            source=str(meta_path),
        )


class HTTPChangesetProvider:
    """Reference HTTP ``ChangesetProvider``.

    Fetches changesets from an HTTP artifact server using the same
    layout as :class:`FilesystemChangesetProvider`::

        GET <base_url>/<instance_id>/meta.json   -> JSON blob
        GET <base_url>/<instance_id>/patch.diff  -> unified diff

    The route shape mirrors the filesystem provider so a corpus staged
    on disk can be served verbatim by ``python -m http.server`` for a
    smoke run, then replaced by a real artifact server in production.

    Authentication is intentionally out of scope for the reference
    implementation: pass static request headers via ``headers=``, or
    fork this class and override :meth:`_get_text` to plug in a session
    library (``requests``, ``httpx``) with cookies, OAuth tokens, etc.

    Standard library only — no new dependencies.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._headers = dict(headers or {})

    def fetch(self, instance_id: str) -> Changeset:
        validate_instance_id(instance_id)
        meta_url = f"{self._base_url}/{instance_id}/meta.json"
        patch_url = f"{self._base_url}/{instance_id}/patch.diff"
        return _build_changeset(
            instance_id,
            meta_text=self._get_text(meta_url, what="meta.json"),
            patch_diff=self._get_text(patch_url, what="patch.diff"),
            source=meta_url,
        )

    def _get_text(self, url: str, *, what: str) -> str:
        req = Request(url, headers=self._headers)
        try:
            with urlopen(req, timeout=self._timeout_s) as resp:  # noqa: S310
                return resp.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(
                    f"{what} not found at {url} (HTTP 404)"
                ) from exc
            raise
        except URLError as exc:
            raise ConnectionError(
                f"could not fetch {what} at {url}: {exc.reason}"
            ) from exc


# Provider registry. Built-ins are registered at module import; external
# pipelines plug in their own factories via register_provider() before
# calling get_provider().
_PROVIDER_FACTORIES: dict[
    str, Callable[[Mapping[str, Any]], ChangesetProvider]
] = {}


def register_provider(
    name: str,
    factory: Callable[[Mapping[str, Any]], ChangesetProvider],
) -> None:
    """Register a ``ChangesetProvider`` factory under ``name``.

    The factory takes a config mapping (the same one ``--config`` parses
    on the CLI) and returns a provider instance. Registering the same
    name twice replaces the prior factory.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("provider name must be a non-empty string")
    _PROVIDER_FACTORIES[name] = factory


def unregister_provider(name: str) -> None:
    """Remove a previously-registered ``ChangesetProvider`` factory.

    No-op when ``name`` is not registered. Intended for tests that
    register a fixture provider and want to leave the registry clean
    for the next test, and for hot-reload scenarios.
    """
    _PROVIDER_FACTORIES.pop(name, None)


def _filesystem_factory(config: Mapping[str, Any]) -> ChangesetProvider:
    if "root" not in config:
        raise KeyError("filesystem provider requires config key 'root'")
    return FilesystemChangesetProvider(config["root"])


def _http_factory(config: Mapping[str, Any]) -> ChangesetProvider:
    if "base_url" not in config:
        raise KeyError("http provider requires config key 'base_url'")
    timeout_s = float(config.get("timeout_s", 30.0))
    headers = config.get("headers")
    if headers is not None and not isinstance(headers, Mapping):
        raise TypeError("http provider 'headers' must be a mapping if supplied")
    return HTTPChangesetProvider(
        config["base_url"],
        timeout_s=timeout_s,
        headers=headers,
    )


register_provider("filesystem", _filesystem_factory)
register_provider("http", _http_factory)


def get_provider(name: str, config: Mapping[str, Any]) -> ChangesetProvider:
    """Return a provider instance for ``name`` with ``config``.

    Raises :class:`ValueError` for an unknown provider; the underlying
    factory may raise :class:`KeyError` (missing required config key)
    or :class:`TypeError` (wrong-typed config value).
    """
    factory = _PROVIDER_FACTORIES.get(name)
    if factory is None:
        known = ", ".join(sorted(_PROVIDER_FACTORIES))
        raise ValueError(
            f"unknown provider {name!r}; known providers: {known}"
        )
    return factory(config)
