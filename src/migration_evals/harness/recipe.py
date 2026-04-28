"""Recipe dataclass for LLM-synthesized build harnesses (PRD M2).

A :class:`Recipe` captures the minimum information required to build and test
a migrated repository inside a container: a Dockerfile, a build command, a
test command, and provenance metadata describing which model + prompt
produced the recipe. Instances are immutable (``frozen=True``) so callers can
safely share them across cache layers and drift detectors without worrying
about in-place mutation.

``harness_provenance`` is the append-only audit surface. It is carried with
the recipe through every cache/lookup path and must include at minimum the
model identifier, the prompt version, and an ISO-8601 UTC timestamp of the
synthesis call. Downstream callers add optional keys (e.g. cassette IDs),
but the three required keys are validated on deserialization.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

_REQUIRED_PROVENANCE_KEYS = ("model", "prompt_version", "timestamp")


@dataclass(frozen=True)
class Recipe:
    """Build/test harness recipe emitted by the LLM synthesizer."""

    dockerfile: str
    build_cmd: str
    test_cmd: str
    harness_provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing = [k for k in _REQUIRED_PROVENANCE_KEYS if k not in self.harness_provenance]
        if missing:
            raise ValueError(
                f"harness_provenance missing required keys: {missing}. "
                f"Got keys: {sorted(self.harness_provenance)}"
            )

    def to_json(self) -> str:
        """Serialize to a deterministic JSON string (sorted keys)."""
        return json.dumps(asdict(self), sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, payload: str) -> Recipe:
        """Parse a JSON string produced by :meth:`to_json`.

        Extra keys are ignored so that forward-compatible fields added by
        future work units do not break older readers.
        """
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise ValueError(f"Recipe JSON must decode to object, got {type(raw).__name__}")
        try:
            return cls(
                dockerfile=raw["dockerfile"],
                build_cmd=raw["build_cmd"],
                test_cmd=raw["test_cmd"],
                harness_provenance=dict(raw["harness_provenance"]),
            )
        except KeyError as exc:
            raise ValueError(f"Recipe JSON missing required field: {exc.args[0]!r}") from exc


__all__ = ["Recipe"]
