"""External-dependency adapter Protocols (PRD D3).

This module **declares** the minimum interface each external dependency must
satisfy to participate in the migration eval framework. The Protocols here
are interface-only: concrete implementations live in downstream work units
(e.g. a real `AnthropicClientAdapter` wrapping the `anthropic` SDK, a
cassette-backed stand-in for deterministic replay in CI).

The adapter layer exists to:

1. Decouple eval orchestration from any single vendor SDK so we can swap
   providers (e.g. Anthropic → a future multiplexer) without touching the
   funnel, synthetic-gen, or reporting layers.
2. Support **replay cassettes** for deterministic unit/integration tests.
   Replay is a construction-time decision, not a per-call hook: the adapter
   factories select ``provider: cassette`` and return the file-backed
   stand-ins from :mod:`migration_evals.adapters_cassette`, which satisfy
   the same Protocols as the live implementations.
3. Give D3's "vendor-in-at-pinned-SHA" posture a single chokepoint so
   security/version audits only have to look in one module.

Any concrete class need only satisfy the Protocol structurally - explicit
subclassing is not required.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AnthropicAdapter(Protocol):
    """Minimum Anthropic Messages API surface used by the migration eval.

    Concrete implementations wrap the official `anthropic` SDK, the
    Claude Code CLI, or replay from a cassette directory. The adapter is
    responsible for prompt caching headers, retry policy, and cost
    accounting - none of which are specified by this Protocol.
    """

    def messages_create(
        self,
        *,
        model: str,
        messages: Iterable[Mapping[str, Any]],
        system: str | None = None,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        """Issue a single Messages API call and return the response envelope."""
        ...


@runtime_checkable
class SandboxAdapter(Protocol):
    """Minimum sandbox surface used by the tiered-oracle funnel.

    Implementations can wrap any container runtime - Docker, a Kubernetes
    job runner, Modal-like serverless containers, a remote sandbox SaaS,
    or a local-only stand-in. The funnel only sees the three methods
    below.
    """

    def create_sandbox(
        self,
        *,
        image: str,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Create a sandbox and return its id."""
        ...

    def exec(
        self,
        sandbox_id: str,
        *,
        command: str,
        timeout_s: int = 600,
    ) -> Mapping[str, Any]:
        """Execute a shell command inside the sandbox and return stdout/stderr/exit."""
        ...

    def destroy_sandbox(self, sandbox_id: str) -> None:
        """Tear down the sandbox."""
        ...


__all__ = [
    "AnthropicAdapter",
    "SandboxAdapter",
]
