"""Pre/post-cutoff score split for contamination detection (PRD M7).

Each trial result records when the underlying repository was created. A
"contaminated" model - one whose training corpus included the repo -
tends to score much higher on pre-cutoff repos than on post-cutoff ones,
because it has effectively memorized the answer.

:func:`split_scores` buckets a list of result dicts by ``repo_created_at``
against the model's ``model_cutoff_date`` and reports the pass-rate in
each bucket plus the gap. When the gap exceeds 5 percentage points (in
absolute value) the ``warning_flag`` is raised.

The gap alone says nothing about whether it is noise or signal - a 5pp
gap on 5 trials per bucket is meaningless, on 500 it is damning. The
report therefore also carries a two-proportion z-test ``p_value`` (and a
``significant`` flag at alpha=0.05): treat ``warning_flag`` as the
conservative tripwire and ``p_value`` as the statistical justification.
Empty buckets report ``None`` scores so "no data" is distinguishable
from "0% pass rate".
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from migration_evals.dates import parse_iso_date

WARNING_THRESHOLD_PP = 5.0
SIGNIFICANCE_ALPHA = 0.05


@dataclass(frozen=True)
class ContaminationReport:
    """Aggregate pre/post-cutoff pass rates for a set of trials.

    ``score_pre`` / ``score_post`` / ``gap_pp`` are ``None`` when the
    corresponding bucket(s) are empty. ``p_value`` is the two-sided
    two-proportion z-test p-value for H0 "pre and post pass rates are
    equal"; it is ``None`` when either bucket is empty or the pooled
    rate is degenerate (all passes or all failures).
    """

    score_pre: float | None
    score_post: float | None
    gap_pp: float | None
    warning_flag: bool
    n_pre: int
    n_post: int
    p_value: float | None
    significant: bool | None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict."""
        return {
            "score_pre": self.score_pre,
            "score_post": self.score_post,
            "gap_pp": self.gap_pp,
            "warning_flag": self.warning_flag,
            "n_pre": self.n_pre,
            "n_post": self.n_post,
            "p_value": self.p_value,
            "significant": self.significant,
        }


def _is_pass(result: Mapping[str, Any]) -> bool:
    """Treat ``success=True`` as a pass; anything else is a miss."""
    value = result.get("success")
    if isinstance(value, bool):
        return value
    return False


def _two_proportion_p_value(
    passes_a: int, total_a: int, passes_b: int, total_b: int
) -> float | None:
    """Two-sided two-proportion z-test p-value (pooled variance).

    Returns ``None`` when either sample is empty or the pooled rate is
    degenerate (0 or 1), where the z statistic is undefined.
    """
    if total_a == 0 or total_b == 0:
        return None
    pooled = (passes_a + passes_b) / (total_a + total_b)
    variance = pooled * (1.0 - pooled) * (1.0 / total_a + 1.0 / total_b)
    if variance <= 0.0:
        return None
    z = ((passes_a / total_a) - (passes_b / total_b)) / math.sqrt(variance)
    return math.erfc(abs(z) / math.sqrt(2.0))


def split_scores(
    results: Sequence[Mapping[str, Any]],
    model_cutoff_date: date,
) -> ContaminationReport:
    """Bucket results by ``repo_created_at`` vs ``model_cutoff_date``.

    A repo is *pre-cutoff* iff its ``repo_created_at`` is strictly before
    ``model_cutoff_date`` - those repos were likely in the training data.
    Results missing or with an unparseable ``repo_created_at`` are skipped
    (they are counted in neither bucket), which is the conservative
    choice: unknown provenance cannot contribute to a contamination
    warning.
    """
    if not isinstance(model_cutoff_date, date):
        raise TypeError(
            f"model_cutoff_date must be a datetime.date, got {type(model_cutoff_date).__name__}"
        )

    pre_total = 0
    pre_passes = 0
    post_total = 0
    post_passes = 0

    for row in results:
        if not isinstance(row, Mapping):
            continue
        created = parse_iso_date(row.get("repo_created_at"))
        if created is None:
            continue
        passed = _is_pass(row)
        if created < model_cutoff_date:
            pre_total += 1
            if passed:
                pre_passes += 1
        else:
            post_total += 1
            if passed:
                post_passes += 1

    score_pre = round(pre_passes / pre_total, 6) if pre_total > 0 else None
    score_post = round(post_passes / post_total, 6) if post_total > 0 else None
    gap_pp: float | None = None
    warning_flag = False
    if score_pre is not None and score_post is not None:
        gap = round((score_pre - score_post) * 100.0, 6)
        gap_pp = gap
        warning_flag = abs(gap) > WARNING_THRESHOLD_PP
    p_value = _two_proportion_p_value(pre_passes, pre_total, post_passes, post_total)
    significant = (p_value < SIGNIFICANCE_ALPHA) if p_value is not None else None

    return ContaminationReport(
        score_pre=score_pre,
        score_post=score_post,
        gap_pp=gap_pp,
        warning_flag=warning_flag,
        n_pre=pre_total,
        n_post=post_total,
        p_value=round(p_value, 6) if p_value is not None else None,
        significant=significant,
    )


__all__ = [
    "ContaminationReport",
    "SIGNIFICANCE_ALPHA",
    "WARNING_THRESHOLD_PP",
    "split_scores",
]
