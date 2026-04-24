"""Iterator-batch reporting for fan-out workflow runs.

Production code-migration workflows typically fan out across hundreds-
to-thousands of repos as a single "iterator" instance. The natural unit
of analysis at that point is the *batch*, not the individual trial:

* Did the iterator complete (every sub-trial reached a terminal state)?
* What fraction of trials succeeded?
* What is the failure-class breakdown?
* What is the p50 / p95 wall-clock latency per trial?
* What did the batch cost in total (sum of per-tier oracle costs)?

This module reads a directory of ``result.json`` files emitted by the
runner, groups them by ``iterator_id`` (treating the absence of an
iterator id as a single implicit batch named ``"<unbatched>"``), and
emits a markdown report.

Public surface:

* :func:`load_results(run_dir)` -> ``list[dict]``
    Walk a run directory and return all parsed ``result.json`` payloads.
* :func:`build_iterator_reports(results)` -> ``list[IteratorReport]``
    Group results by ``iterator_id`` and compute per-batch metrics.
* :func:`format_report(reports)` -> ``str``
    Render the reports as a markdown document.
* :func:`generate_report(run_dir, out_path)` -> ``int``
    End-to-end CLI entry: load + group + render + write. Returns 0/2.

The module uses stdlib only; no numpy / pandas dependency.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

UNBATCHED_KEY = "<unbatched>"


@dataclass(frozen=True)
class IteratorReport:
    """Per-batch aggregate. All counts are non-negative integers.

    Latency / cost statistics are ``None`` when no trial in the batch
    carried the necessary data.
    """

    iterator_id: str
    n_total: int
    n_completed: int
    n_failed: int
    completion_rate: float
    failure_class_breakdown: Mapping[str, int]
    oracle_tier_breakdown: Mapping[str, int]
    total_cost_usd: Optional[float]
    p50_cost_usd: Optional[float]
    p95_cost_usd: Optional[float]
    p50_duration_s: Optional[float]
    p95_duration_s: Optional[float]
    agent_model: Optional[str]
    agent_runner: Optional[str]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_results(run_dir: Path) -> list[dict]:
    """Walk ``run_dir`` recursively and return parsed result.json payloads."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run dir does not exist: {run_dir}")
    results: list[dict] = []
    for path in sorted(run_dir.rglob("result.json")):
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            results.append(payload)
    return results


# ---------------------------------------------------------------------------
# Grouping + aggregation
# ---------------------------------------------------------------------------


def _group_by_iterator(results: Sequence[Mapping]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in results:
        key = r.get("iterator_id") or UNBATCHED_KEY
        groups.setdefault(str(key), []).append(dict(r))
    return groups


def _percentile(values: Sequence[float], pct: float) -> Optional[float]:
    if not values:
        return None
    sorted_v = sorted(values)
    if pct <= 0:
        return sorted_v[0]
    if pct >= 100:
        return sorted_v[-1]
    rank = (pct / 100.0) * (len(sorted_v) - 1)
    lo, hi = int(math.floor(rank)), int(math.ceil(rank))
    if lo == hi:
        return sorted_v[lo]
    frac = rank - lo
    return sorted_v[lo] * (1 - frac) + sorted_v[hi] * frac


def _trial_cost(payload: Mapping) -> Optional[float]:
    funnel = payload.get("funnel")
    if not isinstance(funnel, Mapping):
        return None
    cost = funnel.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        return float(cost)
    return None


def _trial_duration_seconds(payload: Mapping) -> Optional[float]:
    started = payload.get("started_at")
    finished = payload.get("finished_at")
    if not started or not finished:
        return None
    try:
        s = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        f = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (f - s).total_seconds()
    return delta if delta >= 0 else None


def _build_one(iterator_id: str, trials: Sequence[Mapping]) -> IteratorReport:
    n_total = len(trials)
    n_completed = sum(1 for t in trials if bool(t.get("success")))
    n_failed = n_total - n_completed
    completion_rate = n_completed / n_total if n_total else 0.0

    failure_class_breakdown: dict[str, int] = {}
    oracle_tier_breakdown: dict[str, int] = {}
    for t in trials:
        if not bool(t.get("success")):
            cls = str(t.get("failure_class") or "unknown")
            failure_class_breakdown[cls] = failure_class_breakdown.get(cls, 0) + 1
        tier = str(t.get("oracle_tier") or "unknown")
        oracle_tier_breakdown[tier] = oracle_tier_breakdown.get(tier, 0) + 1

    costs = [c for c in (_trial_cost(t) for t in trials) if c is not None]
    total_cost = round(sum(costs), 6) if costs else None
    p50_cost = round(_percentile(costs, 50), 6) if costs else None
    p95_cost = round(_percentile(costs, 95), 6) if costs else None

    durations = [
        d for d in (_trial_duration_seconds(t) for t in trials) if d is not None
    ]
    p50_duration = round(_percentile(durations, 50), 3) if durations else None
    p95_duration = round(_percentile(durations, 95), 3) if durations else None

    # First non-empty agent_model / agent_runner wins; mismatches across
    # trials are rare in a single iterator but possible.
    agent_model = next(
        (str(t["agent_model"]) for t in trials if t.get("agent_model")), None
    )
    agent_runner = next(
        (str(t["agent_runner"]) for t in trials if t.get("agent_runner")), None
    )

    return IteratorReport(
        iterator_id=iterator_id,
        n_total=n_total,
        n_completed=n_completed,
        n_failed=n_failed,
        completion_rate=round(completion_rate, 6),
        failure_class_breakdown=dict(sorted(failure_class_breakdown.items())),
        oracle_tier_breakdown=dict(sorted(oracle_tier_breakdown.items())),
        total_cost_usd=total_cost,
        p50_cost_usd=p50_cost,
        p95_cost_usd=p95_cost,
        p50_duration_s=p50_duration,
        p95_duration_s=p95_duration,
        agent_model=agent_model,
        agent_runner=agent_runner,
    )


def build_iterator_reports(results: Sequence[Mapping]) -> list[IteratorReport]:
    """Return one :class:`IteratorReport` per iterator_id, sorted by id."""
    grouped = _group_by_iterator(results)
    reports = [_build_one(key, trials) for key, trials in grouped.items()]
    reports.sort(key=lambda r: r.iterator_id)
    return reports


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_optional(value: Optional[float], fmt: str = "{:.4f}") -> str:
    if value is None:
        return "—"
    return fmt.format(value)


def _format_breakdown(breakdown: Mapping[str, int]) -> str:
    if not breakdown:
        return "—"
    return ", ".join(f"{k}={v}" for k, v in breakdown.items())


def format_report(reports: Sequence[IteratorReport]) -> str:
    if not reports:
        return "# Iterator-Batch Report\n\nNo result.json files found.\n"
    lines: list[str] = ["# Iterator-Batch Report", ""]
    lines.append(
        "Per-batch aggregate of trials grouped by `iterator_id`. Trials "
        "without an iterator_id are reported under "
        f"`{UNBATCHED_KEY}`."
    )
    lines.append("")
    lines.append(
        "| iterator_id | total | completed | failed | completion_rate | "
        "p50 dur (s) | p95 dur (s) | total cost ($) | p95 cost ($) | "
        "agent_model | agent_runner |"
    )
    lines.append(
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |"
    )
    for r in reports:
        lines.append(
            f"| {r.iterator_id} | {r.n_total} | {r.n_completed} | {r.n_failed} | "
            f"{r.completion_rate:.3f} | "
            f"{_fmt_optional(r.p50_duration_s, '{:.2f}')} | "
            f"{_fmt_optional(r.p95_duration_s, '{:.2f}')} | "
            f"{_fmt_optional(r.total_cost_usd)} | "
            f"{_fmt_optional(r.p95_cost_usd)} | "
            f"{r.agent_model or '—'} | "
            f"{r.agent_runner or '—'} |"
        )
    lines.append("")
    lines.append("## Per-batch breakdowns")
    lines.append("")
    for r in reports:
        lines.append(f"### `{r.iterator_id}`")
        lines.append("")
        lines.append(
            f"- **Failure classes:** {_format_breakdown(r.failure_class_breakdown)}"
        )
        lines.append(
            f"- **Oracle tier (terminating):** {_format_breakdown(r.oracle_tier_breakdown)}"
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_report(run_dir: Path, out_path: Path) -> int:
    """End-to-end: load + group + render + write. Returns exit code."""
    run_dir = Path(run_dir)
    out_path = Path(out_path)
    try:
        results = load_results(run_dir)
    except FileNotFoundError as exc:
        print(f"error: {exc}", flush=True)
        return 2
    reports = build_iterator_reports(results)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(format_report(reports))
    return 0


__all__ = [
    "UNBATCHED_KEY",
    "IteratorReport",
    "load_results",
    "build_iterator_reports",
    "format_report",
    "generate_report",
]
