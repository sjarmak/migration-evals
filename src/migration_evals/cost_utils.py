"""Shared budget-guard helpers for the LLM judge adapters.

The Anthropic, Claude Code, and OpenAI adapters all need the same three
pieces of pre-call machinery: flattening the AnthropicAdapter ``system``
payload to plain text, a rough input-token estimate, and a worst-case
cost bound against a per-model rate table. They previously carried
near-identical private copies; a budget-guard fix had to land three
times (bead migration_evals-tc2). Post-call *actual* cost stays in each
adapter - the usage schemas are genuinely family-specific
(``input_tokens``/``cache_read_input_tokens`` vs ``prompt_tokens``/
``completion_tokens``) and unifying them would invite double-counting.

Rate-table freshness (bead migration_evals-cmy): the default rate
tables are hand-maintained approximations. Each carries a
``rates_as_of`` date; :func:`rates_staleness_warning` returns a warning
string once the table is older than :data:`RATES_MAX_AGE_DAYS`, so a
forgotten table degrades loudly instead of silently skewing the guard.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

__all__ = [
    "RATES_MAX_AGE_DAYS",
    "estimate_input_tokens",
    "flatten_system",
    "rates_staleness_warning",
    "worst_case_cost",
]

# Rate tables older than this are presumed stale - vendor price updates
# land roughly quarterly, so two missed quarters is the warning line.
RATES_MAX_AGE_DAYS = 180


def flatten_system(system: Any) -> str:
    """Collapse the AnthropicAdapter ``system`` payload into a single string.

    Accepts None, a plain string, or a list of content blocks. Block-level
    ``cache_control`` markers are intentionally dropped - non-Anthropic
    backends (and ``claude -p``) do not consume the structured form.
    """
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for blk in system:
            if isinstance(blk, Mapping):
                text = blk.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n\n".join(parts)
    return str(system)


def estimate_input_tokens(messages: Iterable[Mapping[str, Any]], system: Any) -> int:
    """Rough char/4 input-token estimate, sufficient for budget guarding.

    BPE tokenizers average ~4 chars per token on English prose; dense
    code can run 20%+ hotter, so treat this as a coarse safety net, not
    a billing predictor. ``system`` may be the raw AnthropicAdapter
    payload (string or block list) or already-flattened text.
    """
    char_count = len(flatten_system(system))
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            char_count += len(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, Mapping):
                    text = blk.get("text")
                    if isinstance(text, str):
                        char_count += len(text)
    return max(1, char_count // 4)


def worst_case_cost(
    *,
    model: str,
    in_tokens_est: int,
    max_tokens: int,
    cost_rates: Mapping[str, Mapping[str, float]],
) -> float | None:
    """Return USD upper bound for the next call, or None when unknown."""
    rates = cost_rates.get(model)
    if rates is None:
        return None
    input_cost = (in_tokens_est * rates["input"]) / 1_000_000
    output_cost = (max_tokens * rates["output"]) / 1_000_000
    return input_cost + output_cost


def rates_staleness_warning(
    rates_as_of: str | date,
    *,
    label: str,
    today: date | None = None,
    max_age_days: int = RATES_MAX_AGE_DAYS,
) -> str | None:
    """Return a warning string when a rate table is past its shelf life.

    ``rates_as_of`` accepts an ISO date string or a :class:`date`.
    Returns ``None`` when the table is fresh; an unparseable date is
    itself reported as a warning rather than treated as fresh.
    """
    if today is None:
        today = date.today()
    if isinstance(rates_as_of, date):
        as_of = rates_as_of
    else:
        try:
            as_of = date.fromisoformat(str(rates_as_of)[:10])
        except ValueError:
            return (
                f"{label}: cost rate table has unparseable rates_as_of "
                f"{rates_as_of!r}; treat the budget guard as unreliable"
            )
    age_days = (today - as_of).days
    if age_days <= max_age_days:
        return None
    return (
        f"{label}: cost rate table is {age_days} days old (as of "
        f"{as_of.isoformat()}, max {max_age_days}); refresh it or pass "
        f"explicit cost rates - the budget guard may be mispriced"
    )
