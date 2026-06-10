"""Tests for the shared budget-guard helpers (beads migration_evals-tc2, -cmy).

The three judge adapters previously carried near-identical private
copies of these helpers; these tests pin the now-single implementation,
plus the rate-table staleness warning added alongside the dedupe.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from migration_evals.cost_utils import (  # noqa: E402
    RATES_MAX_AGE_DAYS,
    estimate_input_tokens,
    flatten_system,
    rates_staleness_warning,
    worst_case_cost,
)

# -- flatten_system ------------------------------------------------------------


def test_flatten_system_handles_none_string_and_blocks() -> None:
    assert flatten_system(None) == ""
    assert flatten_system("plain") == "plain"
    blocks = [
        {"type": "text", "text": "rubric", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "appendix"},
    ]
    assert flatten_system(blocks) == "rubric\n\nappendix"


def test_flatten_system_drops_non_text_blocks() -> None:
    assert flatten_system([{"type": "text"}, "stray", {"type": "text", "text": "x"}]) == "x"


# -- estimate_input_tokens -------------------------------------------------------


def test_estimate_counts_system_and_message_content() -> None:
    messages = [{"role": "user", "content": "a" * 400}]
    assert estimate_input_tokens(messages, "b" * 400) == 200


def test_estimate_accepts_block_system_and_block_content() -> None:
    messages = [{"role": "user", "content": [{"type": "text", "text": "a" * 40}]}]
    system = [{"type": "text", "text": "b" * 40}]
    assert estimate_input_tokens(messages, system) == 20


def test_estimate_floors_at_one_token() -> None:
    assert estimate_input_tokens([], None) == 1


# -- worst_case_cost -------------------------------------------------------------


def test_worst_case_cost_known_model() -> None:
    rates = {"m": {"input": 1.0, "output": 10.0}}
    cost = worst_case_cost(model="m", in_tokens_est=1_000_000, max_tokens=100_000, cost_rates=rates)
    assert cost == 1.0 + 1.0


def test_worst_case_cost_unknown_model_returns_none() -> None:
    assert worst_case_cost(model="x", in_tokens_est=1, max_tokens=1, cost_rates={}) is None


# -- rates_staleness_warning ------------------------------------------------------


def test_fresh_rates_produce_no_warning() -> None:
    today = date(2026, 6, 1)
    assert rates_staleness_warning("2026-04-01", label="t", today=today) is None


def test_stale_rates_produce_warning_with_age() -> None:
    today = date(2026, 6, 1)
    warning = rates_staleness_warning("2025-06-01", label="anthropic rates", today=today)
    assert warning is not None
    assert "anthropic rates" in warning
    assert "365 days old" in warning
    assert str(RATES_MAX_AGE_DAYS) in warning


def test_boundary_age_is_still_fresh() -> None:
    today = date(2026, 6, 1)
    as_of = date.fromordinal(today.toordinal() - RATES_MAX_AGE_DAYS)
    assert rates_staleness_warning(as_of, label="t", today=today) is None


def test_unparseable_date_is_reported_not_treated_as_fresh() -> None:
    warning = rates_staleness_warning("Q2 2026", label="t", today=date(2026, 6, 1))
    assert warning is not None
    assert "unparseable" in warning


def test_default_rate_tables_carry_parseable_dates() -> None:
    from migration_evals.adapters_anthropic import DEFAULT_COST_RATES_AS_OF
    from migration_evals.adapters_openai import DEFAULT_OPENAI_COST_RATES_AS_OF

    for raw in (DEFAULT_COST_RATES_AS_OF, DEFAULT_OPENAI_COST_RATES_AS_OF):
        date.fromisoformat(raw)
