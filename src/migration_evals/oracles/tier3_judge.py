"""Tier 3 - LLM-judge single-pass verdict with prompt caching (PRD M1).

This tier asks a Claude judge to read the recipe, the repo's manifest
excerpt, and a static rubric and return a PASS / FAIL verdict. The rubric
is the same across every trial, so it lives in the ``system`` prompt with
a ``cache_control: ephemeral`` block - which on a cached provider cuts
per-call cost by the rubric's share of the prompt.

The adapter contract is ``messages_create(*, model, messages, system,
max_tokens, **kwargs)``. We pass ``system`` as the list-of-content-blocks
form so we can attach ``cache_control`` to the rubric block individually.
Tests verify the cache_control block is present by inspecting the
adapter's ``last_request`` capture.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from migration_evals.harness.recipe import Recipe
from migration_evals.oracles.verdict import OracleVerdict

TIER_NAME = "judge"
DEFAULT_COST_USD = 0.08
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS = 512

# Kept in module scope so tests can assert byte-identical cache keys.
JUDGE_RUBRIC = (
    "You are a migration-eval judge. Read the repository manifest excerpt "
    "and the synthesized build recipe. Decide whether the migration is "
    "semantically equivalent to the pre-migration state.\n\n"
    "RUBRIC:\n"
    "1. The build command must match the ecosystem (mvn/gradle/pip/etc.).\n"
    "2. The test command must exist and target the migrated code.\n"
    "3. The Dockerfile must pin a base image matching the target runtime.\n"
    "4. No TODO/placeholder markers in recipe fields.\n\n"
    "Respond with EXACTLY one of: PASS or FAIL followed by a single sentence "
    "explanation. Do not include any other text."
)

_PASS_RE = re.compile(r"^\s*PASS\b", re.IGNORECASE)


def _build_system_blocks() -> list[dict[str, Any]]:
    """Return a ``system`` payload that marks the rubric as cacheable.

    The Anthropic API accepts ``system`` as a list of content blocks;
    attaching ``cache_control`` to the block makes it a cache key the API
    will reuse across calls with identical content.
    """
    return [
        {
            "type": "text",
            "text": JUDGE_RUBRIC,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _compose_user_message(repo_path: Path, recipe: Recipe) -> str:
    parts = [
        f"Repository path: {repo_path}",
        f"Build cmd: {recipe.build_cmd}",
        f"Test cmd: {recipe.test_cmd}",
        "Dockerfile (first 40 lines):",
    ]
    head = "\n".join(recipe.dockerfile.splitlines()[:40])
    parts.append(head)
    manifest_tail: str = ""
    for name in ("pom.xml", "build.gradle", "pyproject.toml", "package.json"):
        candidate = Path(repo_path) / name
        if candidate.is_file():
            try:
                manifest_tail = candidate.read_text(encoding="utf-8", errors="replace")[-2048:]
                parts.append(f"=== {name} (tail) ===\n{manifest_tail}")
                break
            except OSError:
                continue
    parts.append("\nRespond now with PASS or FAIL + one sentence.")
    return "\n\n".join(parts)


def _extract_text(envelope: Mapping[str, Any]) -> str:
    content = envelope.get("content")
    if not isinstance(content, list) or not content:
        return ""
    first = content[0]
    if isinstance(first, Mapping):
        text = first.get("text")
        if isinstance(text, str):
            return text
    return ""


def run(
    repo_path: Path,
    harness_recipe: Recipe,
    anthropic_adapter: Any,
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    cassette: Any | None = None,
    cost_usd: float = DEFAULT_COST_USD,
) -> OracleVerdict:
    """Call the judge and return a judge-tier verdict.

    When the adapter is a
    :class:`~migration_evals.adapters_judge.DualFamilyJudgeAdapter`, the
    returned envelope carries a ``_dual_family`` block with both
    sides' raw envelopes. The verdict surfaces per-judge PASS/FAIL plus
    a disagreement flag, and the trial passes only when both judges
    agree on PASS — bias mitigation contract from bead
    migration_evals-cns.
    """
    repo_path = Path(repo_path)
    system_blocks = _build_system_blocks()
    user_msg = _compose_user_message(repo_path, harness_recipe)

    envelope = anthropic_adapter.messages_create(
        model=model,
        messages=[{"role": "user", "content": user_msg}],
        system=system_blocks,
        max_tokens=max_tokens,
        cassette=cassette,
    )
    raw_text = _extract_text(envelope)

    details: dict[str, Any] = {
        "model": model,
        "judge_text": raw_text,
        "rubric_sha_prefix": JUDGE_RUBRIC[:32],
        "cache_control_sent": True,
        "repo_path": str(repo_path),
    }

    dual = envelope.get("_dual_family") if isinstance(envelope, Mapping) else None
    if isinstance(dual, Mapping):
        anthropic_text = _extract_text(dual.get("anthropic_envelope") or {})
        other_text = _extract_text(dual.get("other_envelope") or {})
        verdict_anthropic = bool(_PASS_RE.match(anthropic_text))
        verdict_other = bool(_PASS_RE.match(other_text))
        verdicts_disagreed = verdict_anthropic != verdict_other
        # Stricter than either alone: PASS iff both agree on PASS.
        passed = verdict_anthropic and verdict_other
        details["dual_family"] = True
        details["other_model"] = dual.get("other_model")
        details["verdict_anthropic"] = verdict_anthropic
        details["verdict_other"] = verdict_other
        details["verdicts_disagreed"] = verdicts_disagreed
        details["judge_text_anthropic"] = anthropic_text
        details["judge_text_other"] = other_text
    else:
        passed = bool(_PASS_RE.match(raw_text))

    # Preserve the structured envelope in details for auditability.
    details["raw_envelope"] = json.loads(json.dumps(envelope, default=str))
    return OracleVerdict(tier=TIER_NAME, passed=passed, cost_usd=cost_usd, details=details)


__all__ = [
    "TIER_NAME",
    "DEFAULT_COST_USD",
    "DEFAULT_MODEL",
    "JUDGE_RUBRIC",
    "run",
]
