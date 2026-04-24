"""Pre/post-cutoff score split for contamination detection (PRD M7).

Each trial result records when the underlying repository was created. A
"contaminated" model — one whose training corpus included the repo —
tends to score much higher on pre-cutoff repos than on post-cutoff ones,
because it has effectively memorized the answer.

:func:`split_scores` buckets a list of result dicts by ``repo_created_at``
against the model's ``model_cutoff_date`` and reports the pass-rate in
each bucket plus the gap. When the gap exceeds 5 percentage points (in
absolute value) the ``warning_flag`` is raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence

WARNING_THRESHOLD_PP = 5.0


@dataclass(frozen=True)
class ContaminationReport:
    """Aggregate pre/post-cutoff pass rates for a set of trials."""

    score_pre: float
    score_post: float
    gap_pp: float
    warning_flag: bool
    n_pre: int
    n_post: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict."""
        return {
            "score_pre": self.score_pre,
            "score_post": self.score_post,
            "gap_pp": self.gap_pp,
            "warning_flag": self.warning_flag,
            "n_pre": self.n_pre,
            "n_post": self.n_post,
        }


def _parse_created_at(raw: Any) -> date | None:
    """Return a :class:`date` or ``None`` if the value is missing/unparseable."""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if not isinstance(raw, str) or not raw:
        return None
    # Accept "YYYY-MM-DD" or full ISO timestamps.
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _is_pass(result: Mapping[str, Any]) -> bool:
    """Treat ``success=True`` as a pass; anything else is a miss."""
    value = result.get("success")
    if isinstance(value, bool):
        return value
    return False


def split_scores(
    results: Sequence[Mapping[str, Any]],
    model_cutoff_date: date,
) -> ContaminationReport:
    """Bucket results by ``repo_created_at`` vs ``model_cutoff_date``.

    A repo is *pre-cutoff* iff its ``repo_created_at`` is strictly before
    ``model_cutoff_date`` — those repos were likely in the training data.
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
        created = _parse_created_at(row.get("repo_created_at"))
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

    score_pre = (pre_passes / pre_total) if pre_total > 0 else 0.0
    score_post = (post_passes / post_total) if post_total > 0 else 0.0
    gap_pp = round((score_pre - score_post) * 100.0, 6)
    warning_flag = abs(gap_pp) > WARNING_THRESHOLD_PP

    return ContaminationReport(
        score_pre=round(score_pre, 6),
        score_post=round(score_post, 6),
        gap_pp=gap_pp,
        warning_flag=warning_flag,
        n_pre=pre_total,
        n_post=post_total,
    )


__all__ = ["ContaminationReport", "WARNING_THRESHOLD_PP", "split_scores"]
