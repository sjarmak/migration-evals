"""LLM-backed build-harness synthesizer (PRD M2).

This module is deliberately thin: it collects a repo's manifest + CI + README
context, asks a Haiku-class model (via the :class:`AnthropicAdapter`
Protocol) to emit a Dockerfile + build/test recipe, and persists the result
under the content-hashed cache. All provider calls flow through the
adapter - the ``harness/`` package never imports the vendor SDK directly,
which is verified by a dedicated test.

Replay determinism is delegated to the adapter: tests wire up a
``FakeAnthropicCassette`` that records pre-canned responses and tracks a
``call_count`` for cache-hit assertions. The synthesizer never reaches the
cassette on a cache hit, which is the core property we rely on for the
"zero adapter calls on second invocation" acceptance criterion.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from migration_evals.harness import cache as cache_mod
from migration_evals.harness.recipe import Recipe

PROMPT_VERSION = "v1"
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_HARNESS_ROOT = Path("runs/analysis/_harnesses")
_MAX_FILE_BYTES = 32_000  # clip oversized READMEs / CI files before prompting.


class HarnessSynthesisError(RuntimeError):
    """Raised when the LLM response cannot be parsed into a recipe."""


_SYSTEM_PROMPT = (
    "You are a build-harness synthesizer. Given a repository's manifest, CI "
    "config, and README, emit ONLY a JSON object with three string keys: "
    '"dockerfile", "build_cmd", "test_cmd". Do not wrap in markdown. '
    "Do not include prose. The Dockerfile string must include a base image "
    "line and any required package installation."
)


def _read_clipped(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text.encode("utf-8")) > _MAX_FILE_BYTES:
        text = text[:_MAX_FILE_BYTES] + "\n# ... [truncated] ...\n"
    return text


def _gather_context(repo_path: Path) -> list[tuple[str, str]]:
    """Return ``[(relative_path, contents), ...]`` for relevant files."""
    collected: list[tuple[str, str]] = []
    for name in cache_mod.MANIFEST_FILENAMES:
        body = _read_clipped(repo_path / name)
        if body is not None:
            collected.append((name, body))
    workflows = repo_path / ".github" / "workflows"
    if workflows.is_dir():
        for ci_file in sorted(workflows.iterdir()):
            if ci_file.suffix in (".yml", ".yaml") and ci_file.is_file():
                body = _read_clipped(ci_file)
                if body is not None:
                    collected.append((f".github/workflows/{ci_file.name}", body))
    readme = repo_path / "README.md"
    body = _read_clipped(readme)
    if body is not None:
        collected.append(("README.md", body))
    return collected


def _compose_user_message(ctx: list[tuple[str, str]]) -> str:
    parts = ["Synthesize a build harness for this repo.\n"]
    for rel, content in ctx:
        parts.append(f"=== {rel} ===\n{content}\n")
    parts.append(
        "\nRespond with a single JSON object: "
        '{"dockerfile": "...", "build_cmd": "...", "test_cmd": "..."}'
    )
    return "".join(parts)


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop opening fence (with optional language tag).
        newline_idx = stripped.find("\n")
        if newline_idx == -1:
            return stripped
        stripped = stripped[newline_idx + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
    return stripped.strip()


def _extract_text(response: Mapping[str, Any]) -> str:
    if "error" in response:
        raise HarnessSynthesisError(f"adapter returned error envelope: {response['error']!r}")
    content = response.get("content")
    if not isinstance(content, list) or not content:
        raise HarnessSynthesisError("response has no content blocks")
    first = content[0]
    if not isinstance(first, Mapping):
        raise HarnessSynthesisError("response content[0] is not an object")
    text = first.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HarnessSynthesisError("response content[0].text is empty or non-string")
    return text


def _parse_recipe_payload(text: str) -> dict[str, str]:
    cleaned = _strip_code_fences(text)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise HarnessSynthesisError(f"recipe JSON could not be parsed: {exc}") from exc
    if not isinstance(obj, dict):
        raise HarnessSynthesisError(f"recipe JSON must decode to object, got {type(obj).__name__}")
    for key in ("dockerfile", "build_cmd", "test_cmd"):
        if key not in obj or not isinstance(obj[key], str) or not obj[key].strip():
            raise HarnessSynthesisError(f"recipe JSON missing or empty field: {key!r}")
    return {k: obj[k] for k in ("dockerfile", "build_cmd", "test_cmd")}


def synthesize_recipe(
    repo_path: Path,
    anthropic_adapter: Any,
    *,
    root: Path = DEFAULT_HARNESS_ROOT,
    model: str = DEFAULT_MODEL,
    prompt_version: str = PROMPT_VERSION,
) -> Recipe:
    """Return a :class:`Recipe` for ``repo_path``, using the cache when possible.

    On cache hit, ``anthropic_adapter.messages_create`` is NOT called.
    On cache miss, the adapter is called exactly once, the response is
    parsed, persisted, and returned. Failure modes (unparseable JSON,
    error envelopes, empty content) raise :class:`HarnessSynthesisError`
    so the caller can quarantine the repo.
    """
    repo_path = Path(repo_path)
    root = Path(root)

    key = cache_mod.content_hash(repo_path)
    cached = cache_mod.lookup(key, root)
    if cached is not None:
        return cached

    ctx = _gather_context(repo_path)
    user_message = _compose_user_message(ctx)
    response = anthropic_adapter.messages_create(
        model=model,
        messages=[{"role": "user", "content": user_message}],
        system=_SYSTEM_PROMPT,
        max_tokens=2048,
    )
    text = _extract_text(response)
    fields = _parse_recipe_payload(text)

    provenance = {
        "model": model,
        "prompt_version": prompt_version,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    recipe = Recipe(
        dockerfile=fields["dockerfile"],
        build_cmd=fields["build_cmd"],
        test_cmd=fields["test_cmd"],
        harness_provenance=provenance,
    )
    cache_mod.store(key, recipe, root)
    return recipe


__all__ = [
    "HarnessSynthesisError",
    "PROMPT_VERSION",
    "DEFAULT_MODEL",
    "DEFAULT_HARNESS_ROOT",
    "synthesize_recipe",
]
