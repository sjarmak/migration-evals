"""External-dependency adapter Protocols (PRD D3).

This module **declares** the minimum interface each external dependency must
satisfy to participate in the migration eval framework. The Protocols here
are interface-only: concrete implementations live in downstream work units
(e.g. a real `AnthropicClientAdapter` wrapping the `anthropic` SDK, a fake
cassette-backed adapter for deterministic replay in CI).

The adapter layer exists to:

1. Decouple eval orchestration from any single vendor SDK so we can swap
   providers (e.g. Anthropic → a future multiplexer) without touching the
   funnel, synthetic-gen, or reporting layers.
2. Support **replay cassettes** for deterministic unit/integration tests:
   every adapter may be constructed with a `Cassette` that plays back
   pre-recorded responses instead of issuing live calls. See the `Cassette`
   Protocol below for the replay-cassette hook contract.
3. Give D3's "vendor-in-at-pinned-SHA" posture a single chokepoint so
   security/version audits only have to look in one module.

Any concrete class need only satisfy the Protocol structurally — explicit
subclassing is not required.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Protocol, runtime_checkable


@runtime_checkable
class Cassette(Protocol):
    """Replay-cassette hook used by every adapter.

    A cassette yields pre-recorded responses in deterministic order. Adapters
    that accept a cassette **must** consult it before issuing any real
    network call; in replay mode `next_response()` is authoritative.
    """

    def next_response(self) -> Mapping[str, Any]:
        """Return the next recorded response envelope."""
        ...

    def record(self, request: Mapping[str, Any], response: Mapping[str, Any]) -> None:
        """Append a request/response pair to the cassette (record mode)."""
        ...


@runtime_checkable
class AnthropicAdapter(Protocol):
    """Minimum Anthropic Messages API surface used by the migration eval.

    Concrete implementations wrap the official `anthropic` SDK; a replay
    implementation reads from a `Cassette`. The adapter is responsible for
    prompt caching headers, retry policy, and cost accounting — none of
    which are specified by this Protocol.
    """

    def messages_create(
        self,
        *,
        model: str,
        messages: Iterable[Mapping[str, Any]],
        system: Optional[str] = None,
        max_tokens: int = 1024,
        cassette: Optional[Cassette] = None,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        """Issue a single Messages API call and return the response envelope."""
        ...


@runtime_checkable
class DaytonaAdapter(Protocol):
    """Minimum Daytona sandbox surface used by the tiered-oracle funnel."""

    def create_sandbox(
        self,
        *,
        image: str,
        env: Optional[Mapping[str, str]] = None,
        cassette: Optional[Cassette] = None,
    ) -> str:
        """Create a sandbox and return its id."""
        ...

    def exec(
        self,
        sandbox_id: str,
        *,
        command: str,
        timeout_s: int = 600,
        cassette: Optional[Cassette] = None,
    ) -> Mapping[str, Any]:
        """Execute a shell command inside the sandbox and return stdout/stderr/exit."""
        ...

    def destroy_sandbox(self, sandbox_id: str) -> None:
        """Tear down the sandbox."""
        ...


@runtime_checkable
class OpenRewriteAdapter(Protocol):
    """Minimum OpenRewrite surface.

    Per PRD D3, OpenRewrite is **vendored at a pinned SHA**. The adapter
    wraps whatever invocation mechanism (subprocess, JVM embedding, etc.)
    the concrete implementation chooses.
    """

    def apply_recipe(
        self,
        *,
        repo_path: str,
        recipe: str,
        cassette: Optional[Cassette] = None,
    ) -> Mapping[str, Any]:
        """Run an OpenRewrite recipe on a checked-out repo path."""
        ...


@runtime_checkable
class CodyAdapter(Protocol):
    """Minimum Sourcegraph Cody surface used during harness inference."""

    def search_code(
        self,
        *,
        query: str,
        repo: Optional[str] = None,
        cassette: Optional[Cassette] = None,
    ) -> Iterable[Mapping[str, Any]]:
        """Run a Sourcegraph code search and yield result envelopes."""
        ...


@runtime_checkable
class GitHubAdapter(Protocol):
    """Minimum GitHub REST/GraphQL surface used for repo acquisition."""

    def get_repo(
        self,
        *,
        owner: str,
        repo: str,
        cassette: Optional[Cassette] = None,
    ) -> Mapping[str, Any]:
        """Return repository metadata."""
        ...

    def clone(
        self,
        *,
        owner: str,
        repo: str,
        dest: str,
        ref: Optional[str] = None,
        cassette: Optional[Cassette] = None,
    ) -> str:
        """Clone a repo to `dest` and return the checked-out commit SHA."""
        ...


@runtime_checkable
class DockerAdapter(Protocol):
    """Minimum Docker surface used for local (non-Daytona) harness runs."""

    def build_image(
        self,
        *,
        context_dir: str,
        dockerfile: str,
        tag: str,
        cassette: Optional[Cassette] = None,
    ) -> str:
        """Build an image from a Dockerfile and return the image id."""
        ...

    def run(
        self,
        *,
        image: str,
        command: str,
        timeout_s: int = 600,
        env: Optional[Mapping[str, str]] = None,
        cassette: Optional[Cassette] = None,
    ) -> Mapping[str, Any]:
        """Run a one-shot container and return stdout/stderr/exit."""
        ...


__all__ = [
    "Cassette",
    "AnthropicAdapter",
    "DaytonaAdapter",
    "OpenRewriteAdapter",
    "CodyAdapter",
    "GitHubAdapter",
    "DockerAdapter",
]
