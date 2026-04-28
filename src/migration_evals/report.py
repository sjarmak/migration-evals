"""Funnel report generator for the migration eval framework.

Aggregates a directory of per-trial ``result.json`` files into a single
markdown report. Sections (in order):

1. Funnel table - per-tier n_entered/n_passed/n_failed/cumulative_pass_rate.
2. Contamination split - score_pre_cutoff, score_post_cutoff, gap_pp, warning.
3. Gold-anchor correlation - point + 95% CI + eval_broken (optional).
4. Spec stamps - oracle_spec_sha / recipe_spec_sha / pre_reg_sha.
5. Failure-class breakdown - count per class across failed trials.

Rendering strategy
------------------
The rendering path is a hand-rolled :func:`format_report` that uses
f-strings. Jinja2 is listed as available at dev time but we deliberately
avoid depending on it at runtime - see docs/migration_eval/usage.md. The
companion ``templates/report.md.j2`` file is a reference-only artifact
that documents the target structure.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from migration_evals.contamination import split_scores
from migration_evals.gold_anchor import CorrelationReport, correlate, load_gold_set
from migration_evals.stats import (
    bootstrap_proportion_ci,
    wilson_interval,
)

# All five tiers in fixed order so the report always has the same shape.
_TIER_ORDER: tuple[str, ...] = (
    "compile_only",
    "tests",
    "ast_conformance",
    "judge",
    "daikon",
)

_DEFAULT_CUTOFF = date(2025, 1, 1)


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def _iter_results(run_dir: Path) -> Iterable[dict[str, Any]]:
    """Yield every parseable ``result.json`` payload under ``run_dir``."""
    for result_path in sorted(run_dir.rglob("result.json")):
        try:
            yield json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue


def _load_summary(run_dir: Path) -> dict[str, Any]:
    summary_path = run_dir / "summary.json"
    if not summary_path.is_file():
        return {}
    try:
        return json.loads(summary_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _coerce_cutoff(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _funnel_counts(
    results: Sequence[Mapping[str, Any]],
    *,
    bootstrap_seed: int = 42,
    n_bootstrap: int = 10_000,
) -> list[dict[str, Any]]:
    """Compute per-tier funnel counts with 95% confidence intervals.

    For each tier:

    * ``n_entered`` = number of trials whose ``per_tier_verdict`` includes
      the tier (i.e. the cascade reached it).
    * ``n_passed`` = number of trials whose verdict for that tier was
      ``passed=True``.
    * ``n_failed`` = n_entered - n_passed.
    * ``cumulative_pass_rate`` = n_passed / total_trials - gives the
      fraction of trials that made it past the tier.
    * ``rate_ci_low`` / ``rate_ci_high`` — Wilson 95% CI on the
      conditional pass rate (n_passed / n_entered) for trials that
      reached this tier. ``(0, 0)`` when n_entered = 0.
    * ``cumulative_ci_low`` / ``cumulative_ci_high`` — percentile
      bootstrap 95% CI on the cumulative pass-through rate, resampling
      trials with replacement. ``(0, 0)`` when total = 0. Seed and
      ``n_bootstrap`` are exposed so callers (and tests) get
      deterministic numbers.
    """
    total = len(results)
    rows: list[dict[str, Any]] = []
    for tier in _TIER_ORDER:
        n_entered = 0
        n_passed = 0
        cumulative_flags: list[bool] = []
        for row in results:
            verdicts = (row.get("funnel") or {}).get("per_tier_verdict") or []
            tier_passed = False
            tier_seen = False
            for verdict in verdicts:
                if not isinstance(verdict, Mapping):
                    continue
                if verdict.get("tier") != tier:
                    continue
                tier_seen = True
                n_entered += 1
                if bool(verdict.get("passed")):
                    n_passed += 1
                    tier_passed = True
                break
            cumulative_flags.append(tier_passed if tier_seen else False)
        n_failed = n_entered - n_passed
        cumulative = (n_passed / total) if total > 0 else 0.0
        rate_ci_low, rate_ci_high = wilson_interval(n_passed, n_entered)
        cum_ci_low, cum_ci_high = bootstrap_proportion_ci(
            cumulative_flags,
            n_bootstrap=n_bootstrap,
            seed=bootstrap_seed,
        )
        rows.append(
            {
                "tier_name": tier,
                "n_entered": n_entered,
                "n_passed": n_passed,
                "n_failed": n_failed,
                "cumulative_pass_rate": round(cumulative, 6),
                "rate_ci_low": round(rate_ci_low, 6),
                "rate_ci_high": round(rate_ci_high, 6),
                "cumulative_ci_low": round(cum_ci_low, 6),
                "cumulative_ci_high": round(cum_ci_high, 6),
            }
        )
    return rows


_QUALITY_ORACLE_ORDER: tuple[str, ...] = (
    "diff_minimality",
    "idempotency",
    "baseline_comparison",
    "touched_paths",
    "cve_disappears",
)


def _quality_aggregate(
    results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate the dsm batch-change quality oracles per tier.

    For each oracle in fixed order:

    - ``n_observed``: trials whose ``funnel.quality_verdicts`` includes
      this oracle (always equals ``n_trials`` once the oracle is wired
      into a config, but degrades gracefully on legacy result.json files
      that pre-date the dsm field).
    - ``n_passed``: trials where the oracle reported ``passed=True``.
    - ``n_skipped``: trials where the oracle returned a ``skipped=True``
      detail (no ground truth / no baseline_pattern / etc.). Skipped
      trials count toward ``n_observed`` and ``n_passed`` because the
      verdict is informational.
    - ``mean_diff_size_ratio`` / ``mean_over_edit_pct`` /
      ``mean_files_overlap``: only emitted for ``diff_minimality`` and
      only over the trials where the metric is non-null.
    - ``baseline_passed_rate``: only for ``baseline_comparison``;
      fraction of trials where ``baseline_passed=True`` (i.e. the
      baseline tool would have produced the same migration).
    - ``cve_disappears_rate``: only for ``cve_disappears``; fraction of
      *non-skipped* trials where the named CVE was absent from trivy
      output (1.0 means every observed trial cleared the CVE; computed
      over ``n_observed - n_skipped`` so trials that lacked trivy on
      PATH or hit a parse-failure skip do not dilute the rate).
    """
    rows: list[dict[str, Any]] = []
    for tier in _QUALITY_ORACLE_ORDER:
        n_observed = 0
        n_passed = 0
        n_skipped = 0
        ratios: list[float] = []
        over_edits: list[float] = []
        overlaps: list[float] = []
        baseline_passes = 0
        cve_absent_count = 0
        for row in results:
            verdicts = (row.get("funnel") or {}).get("quality_verdicts") or []
            for verdict in verdicts:
                if not isinstance(verdict, Mapping):
                    continue
                if verdict.get("tier") != tier:
                    continue
                n_observed += 1
                if bool(verdict.get("passed")):
                    n_passed += 1
                details = verdict.get("details") or {}
                if details.get("skipped") is True:
                    n_skipped += 1
                if tier == "diff_minimality":
                    ratio = details.get("diff_size_ratio")
                    if isinstance(ratio, (int, float)):
                        ratios.append(float(ratio))
                    over = details.get("over_edit_pct")
                    if isinstance(over, (int, float)):
                        over_edits.append(float(over))
                    overlap = details.get("touched_files_overlap")
                    if isinstance(overlap, (int, float)):
                        overlaps.append(float(overlap))
                if tier == "baseline_comparison":
                    if details.get("baseline_passed") is True:
                        baseline_passes += 1
                if tier == "cve_disappears":
                    if details.get("skipped") is not True and details.get("cve_present") is False:
                        cve_absent_count += 1
                break
        row_out: dict[str, Any] = {
            "tier_name": tier,
            "n_observed": n_observed,
            "n_passed": n_passed,
            "n_skipped": n_skipped,
            "pass_rate": (round(n_passed / n_observed, 6) if n_observed > 0 else None),
        }
        if tier == "diff_minimality":
            row_out["mean_diff_size_ratio"] = (
                round(sum(ratios) / len(ratios), 6) if ratios else None
            )
            row_out["mean_over_edit_pct"] = (
                round(sum(over_edits) / len(over_edits), 6) if over_edits else None
            )
            row_out["mean_touched_files_overlap"] = (
                round(sum(overlaps) / len(overlaps), 6) if overlaps else None
            )
        if tier == "baseline_comparison":
            # Denominator is n_observed (includes skipped trials),
            # deliberately asymmetric with cve_disappears_rate which uses
            # n_observed - n_skipped. Skipped baseline trials are rare in
            # practice (only when the recipe lacks a baseline_pattern),
            # so dividing by n_observed keeps the rate comparable across
            # corpora. cve_disappears skips are common (any workstation
            # without trivy on PATH), so subtracting them preserves
            # interpretability.
            row_out["baseline_passed_rate"] = (
                round(baseline_passes / n_observed, 6) if n_observed > 0 else None
            )
        if tier == "cve_disappears":
            scanned = n_observed - n_skipped
            row_out["cve_disappears_rate"] = (
                round(cve_absent_count / scanned, 6) if scanned > 0 else None
            )
        rows.append(row_out)
    return rows


def _coerce_seconds(started: Any, finished: Any) -> float | None:
    if not started or not finished:
        return None
    try:
        from datetime import datetime as _dt

        s = _dt.fromisoformat(str(started).replace("Z", "+00:00"))
        f = _dt.fromisoformat(str(finished).replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (f - s).total_seconds()
    return delta if delta >= 0 else None


def _percentile(values: Sequence[float], pct: float) -> float | None:
    if not values:
        return None
    sorted_v = sorted(values)
    if pct <= 0.0:
        return sorted_v[0]
    if pct >= 100.0:
        return sorted_v[-1]
    n = len(sorted_v)
    rank = (pct / 100.0) * (n - 1)
    lo = int(rank)
    hi = lo + 1 if lo + 1 < n else lo
    if lo == hi:
        return sorted_v[lo]
    frac = rank - lo
    return sorted_v[lo] * (1 - frac) + sorted_v[hi] * frac


def _trial_total_cost_usd(payload: Mapping[str, Any]) -> float | None:
    funnel = payload.get("funnel")
    if not isinstance(funnel, Mapping):
        return None
    cost = funnel.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        return float(cost)
    return None


def _trial_total_tokens(payload: Mapping[str, Any]) -> int | None:
    """Best-effort: sum input + output tokens across per-tier verdicts.

    Tokens land in result.json only when the underlying agent adapter
    surfaces them (today: claude_code via the AnthropicAdapter usage
    block). Returns ``None`` when no per-tier verdict carries a usage
    payload, so the report can render a placeholder rather than 0.
    """
    funnel = payload.get("funnel")
    if not isinstance(funnel, Mapping):
        return None
    total = 0
    saw_any = False
    verdicts = funnel.get("per_tier_verdict") or []
    for verdict in verdicts:
        if not isinstance(verdict, Mapping):
            continue
        details = verdict.get("details") or {}
        usage = details.get("usage") if isinstance(details, Mapping) else None
        if not isinstance(usage, Mapping):
            continue
        in_tok = usage.get("input_tokens")
        out_tok = usage.get("output_tokens")
        if isinstance(in_tok, (int, float)):
            total += int(in_tok)
            saw_any = True
        if isinstance(out_tok, (int, float)):
            total += int(out_tok)
            saw_any = True
    return total if saw_any else None


def _cost_aggregate(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate cost / latency / token metrics for the funnel report.

    Keys (each may be ``None`` when the underlying signal is absent):

    * ``n_total`` / ``n_success`` — denominators for the per-success
      ratios. ``n_success`` is trials with ``success=True``.
    * ``total_cost_usd`` — sum across every trial that records a cost.
    * ``dollars_per_success`` — ``total_cost_usd / n_success`` when both
      are positive.
    * ``p50_latency_s`` / ``p95_latency_s`` — wall-clock per-trial,
      computed from ``finished_at - started_at``.
    * ``total_tokens`` / ``tokens_per_success`` — best-effort sum of
      ``input_tokens + output_tokens`` aggregated across per-tier
      verdicts; only populated when at least one verdict carried usage.
    """
    n_total = len(results)
    successes = [bool(r.get("success")) for r in results]
    n_success = sum(1 for s in successes if s)

    costs = [c for c in (_trial_total_cost_usd(r) for r in results) if c is not None]
    total_cost = round(sum(costs), 6) if costs else None
    dollars_per_success = (
        round(total_cost / n_success, 6) if (total_cost is not None and n_success > 0) else None
    )

    latencies = [
        d
        for d in (_coerce_seconds(r.get("started_at"), r.get("finished_at")) for r in results)
        if d is not None
    ]
    p50_lat = _percentile(latencies, 50.0)
    p95_lat = _percentile(latencies, 95.0)
    p50_lat = round(p50_lat, 3) if p50_lat is not None else None
    p95_lat = round(p95_lat, 3) if p95_lat is not None else None

    tokens = [t for t in (_trial_total_tokens(r) for r in results) if t is not None]
    total_tokens = sum(tokens) if tokens else None
    tokens_per_success = (
        round(total_tokens / n_success, 3) if (total_tokens is not None and n_success > 0) else None
    )

    return {
        "n_total": n_total,
        "n_success": n_success,
        "total_cost_usd": total_cost,
        "dollars_per_success": dollars_per_success,
        "p50_latency_s": p50_lat,
        "p95_latency_s": p95_lat,
        "total_tokens": total_tokens,
        "tokens_per_success": tokens_per_success,
    }


def _efficiency_aggregate(
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Tries-per-success when at least one trial has an iterator_id.

    Iterator runs fan a single migration intent across many repos; the
    efficiency question for those runs is how many attempts the harness
    burned per successful migration. Returns ``None`` for runs whose
    trials all lack ``iterator_id`` so the renderer can omit the
    section entirely (single-shot smoke runs aren't iteration runs).
    """
    iter_trials = [r for r in results if r.get("iterator_id")]
    if not iter_trials:
        return None
    n_total = len(iter_trials)
    n_success = sum(1 for r in iter_trials if bool(r.get("success")))
    tries_per_success = round(n_total / n_success, 4) if n_success > 0 else None

    by_iter: dict[str, dict[str, int]] = {}
    for r in iter_trials:
        key = str(r.get("iterator_id"))
        bucket = by_iter.setdefault(key, {"n": 0, "ok": 0})
        bucket["n"] += 1
        if bool(r.get("success")):
            bucket["ok"] += 1
    per_iterator = [
        {
            "iterator_id": key,
            "n_total": bucket["n"],
            "n_success": bucket["ok"],
            "tries_per_success": (
                round(bucket["n"] / bucket["ok"], 4) if bucket["ok"] > 0 else None
            ),
        }
        for key, bucket in sorted(by_iter.items())
    ]
    return {
        "n_total": n_total,
        "n_success": n_success,
        "tries_per_success": tries_per_success,
        "per_iterator": per_iterator,
    }


def _failure_class_counts(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in results:
        if bool(row.get("success")):
            continue
        cls = row.get("failure_class")
        if cls is None:
            key = "unclassified"
        else:
            key = str(cls)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _stamp_block(
    results: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
) -> dict[str, str]:
    """Return the three spec stamps from summary.json first, else first result."""
    stamps_raw = summary.get("stamps")
    if isinstance(stamps_raw, Mapping):
        return {
            "oracle_spec_sha": str(stamps_raw.get("oracle_spec_sha", "")),
            "recipe_spec_sha": str(stamps_raw.get("recipe_spec_sha", "")),
            "pre_reg_sha": str(stamps_raw.get("pre_reg_sha", "")),
        }
    if results:
        first = results[0]
        return {
            "oracle_spec_sha": str(first.get("oracle_spec_sha", "")),
            "recipe_spec_sha": str(first.get("recipe_spec_sha", "")),
            "pre_reg_sha": str(first.get("pre_reg_sha", "")),
        }
    return {"oracle_spec_sha": "", "recipe_spec_sha": "", "pre_reg_sha": ""}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_funnel_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render the funnel table with Wilson + bootstrap 95% CIs.

    The two CIs answer different questions and both matter:

    * The **rate CI** (Wilson, conditional on entering the tier) tells
      you how confidently you can publish the per-tier pass rate as a
      property of the oracle.
    * The **cumulative CI** (bootstrap, over all trials) tells you how
      confidently you can publish the end-to-end pass-through.

    Empty tiers render the CI as ``-`` instead of ``[0.000, 0.000]`` so
    a reader doesn't mistake "no signal" for "we measured zero".
    """
    lines = [
        "| tier_name | n_entered | n_passed | n_failed | "
        "rate_ci_95 | cumulative_pass_rate | cumulative_ci_95 |",
        "|-----------|-----------|----------|----------|"
        "------------|----------------------|------------------|",
    ]
    for row in rows:
        rate_lo = row.get("rate_ci_low")
        rate_hi = row.get("rate_ci_high")
        if row["n_entered"] > 0 and rate_lo is not None and rate_hi is not None:
            rate_ci = f"[{rate_lo:.3f}, {rate_hi:.3f}]"
        else:
            rate_ci = "-"
        cum_lo = row.get("cumulative_ci_low")
        cum_hi = row.get("cumulative_ci_high")
        if cum_lo is not None and cum_hi is not None:
            cum_ci = f"[{cum_lo:.3f}, {cum_hi:.3f}]"
        else:
            cum_ci = "-"
        lines.append(
            f"| {row['tier_name']} | {row['n_entered']} | {row['n_passed']} | "
            f"{row['n_failed']} | {rate_ci} | "
            f"{row['cumulative_pass_rate']:.4f} | {cum_ci} |"
        )
    return "\n".join(lines)


def _format_cost_section(cost: Mapping[str, Any]) -> str:
    """Render the cost-normalisation block.

    Numbers are dashed-out when missing — a per-success ratio of
    ``$0.00`` would silently lie about cost when ``n_success == 0`` or
    cost data is absent on every trial.
    """
    n_total = cost.get("n_total", 0)
    n_success = cost.get("n_success", 0)
    total_cost = cost.get("total_cost_usd")
    dollars_each = cost.get("dollars_per_success")
    p50 = cost.get("p50_latency_s")
    p95 = cost.get("p95_latency_s")
    total_tokens = cost.get("total_tokens")
    tokens_each = cost.get("tokens_per_success")

    def _fmt(v: Any, fmt: str) -> str:
        if v is None:
            return "-"
        return fmt.format(v)

    return (
        f"- n_total: {n_total}\n"
        f"- n_success: {n_success}\n"
        f"- total_cost_usd: {_fmt(total_cost, '${:.4f}')}\n"
        f"- dollars_per_success: {_fmt(dollars_each, '${:.4f}')}\n"
        f"- p50_latency_s: {_fmt(p50, '{:.2f}')}\n"
        f"- p95_latency_s: {_fmt(p95, '{:.2f}')}\n"
        f"- total_tokens: {_fmt(total_tokens, '{}')}\n"
        f"- tokens_per_success: {_fmt(tokens_each, '{:.1f}')}"
    )


def _format_efficiency_section(eff: Mapping[str, Any] | None) -> str:
    if eff is None:
        return "- (no trials carry an `iterator_id`; section omitted)"
    n_total = eff["n_total"]
    n_success = eff["n_success"]
    overall_tps = eff["tries_per_success"]
    per_iter = eff.get("per_iterator") or []
    overall = f"{overall_tps:.4f}" if overall_tps is not None else "-"
    lines = [
        f"- iterator_trials: {n_total}",
        f"- iterator_successes: {n_success}",
        f"- overall_tries_per_success: {overall}",
    ]
    if per_iter:
        lines.append("")
        lines.append("| iterator_id | n_total | n_success | tries_per_success |")
        lines.append("|-------------|---------|-----------|--------------------|")
        for entry in per_iter:
            tps = entry["tries_per_success"]
            tps_str = f"{tps:.4f}" if tps is not None else "-"
            lines.append(
                f"| {entry['iterator_id']} | {entry['n_total']} | "
                f"{entry['n_success']} | {tps_str} |"
            )
    return "\n".join(lines)


def _format_contamination(contam: Mapping[str, Any]) -> str:
    flag = "YES" if contam.get("warning_flag") else "no"
    return (
        f"- score_pre_cutoff: {contam.get('score_pre', 0.0):.4f} "
        f"(n={contam.get('n_pre', 0)})\n"
        f"- score_post_cutoff: {contam.get('score_post', 0.0):.4f} "
        f"(n={contam.get('n_post', 0)})\n"
        f"- gap_pp: {contam.get('gap_pp', 0.0):.4f}\n"
        f"- warning_flag: {flag}"
    )


def _format_gold_section(report: CorrelationReport | None) -> str | None:
    if report is None:
        return None
    broken = "YES" if report.eval_broken else "no"
    details = report.details or {}
    n_pairs = details.get("n_pairs", 0)
    return (
        f"- point: {report.point:.4f}\n"
        f"- ci_low: {report.ci_low:.4f}\n"
        f"- ci_high: {report.ci_high:.4f}\n"
        f"- eval_broken: {broken}\n"
        f"- n_pairs: {n_pairs}"
    )


def _format_stamps(stamps: Mapping[str, str]) -> str:
    return (
        f"- oracle_spec_sha: `{stamps.get('oracle_spec_sha', '')}`\n"
        f"- recipe_spec_sha: `{stamps.get('recipe_spec_sha', '')}`\n"
        f"- pre_reg_sha: `{stamps.get('pre_reg_sha', '')}`"
    )


def _format_quality_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render the dsm 'Batch-change quality' section.

    The columns are deliberately wide enough to surface both the headline
    pass-rate and the underlying numerator, so a reader can tell whether
    a 0% pass-rate means "0 of 200 trials passed" or "0 of 0 observed".
    """
    if not rows or all(r["n_observed"] == 0 for r in rows):
        return "- (no quality verdicts emitted; recipe declares no `quality:` block)"
    lines: list[str] = []
    for row in rows:
        n_obs = row["n_observed"]
        if n_obs == 0:
            lines.append(f"- **{row['tier_name']}**: not observed")
            continue
        skipped_note = f" (skipped: {row['n_skipped']})" if row["n_skipped"] else ""
        pass_rate = row.get("pass_rate")
        pass_str = f"{pass_rate:.4f}" if pass_rate is not None else "n/a"
        lines.append(
            f"- **{row['tier_name']}**: pass {row['n_passed']}/{n_obs} "
            f"({pass_str}){skipped_note}"
        )
        if row["tier_name"] == "diff_minimality":
            mean_ratio = row.get("mean_diff_size_ratio")
            mean_over = row.get("mean_over_edit_pct")
            mean_overlap = row.get("mean_touched_files_overlap")
            if mean_ratio is not None:
                lines.append(f"  - mean diff_size_ratio: {mean_ratio:.4f}")
            if mean_over is not None:
                lines.append(f"  - mean over_edit_pct: {mean_over:.4f}")
            if mean_overlap is not None:
                lines.append(f"  - mean touched_files_overlap: {mean_overlap:.4f}")
        if row["tier_name"] == "baseline_comparison":
            baseline_rate = row.get("baseline_passed_rate")
            if baseline_rate is not None:
                lines.append(
                    f"  - baseline_passed rate: {baseline_rate:.4f} "
                    "(1.0 means baseline ≡ agent on every trial)"
                )
        if row["tier_name"] == "cve_disappears":
            cve_rate = row.get("cve_disappears_rate")
            if cve_rate is not None:
                # cve_disappears_rate is computed in _quality_aggregate as
                # cve_absent_count / (n_observed - n_skipped); only the
                # display "over N non-skipped trial(s)" text re-derives the
                # denominator for human readability. _quality_aggregate is
                # the source of truth — if its rate is non-None, scanned
                # is guaranteed > 0.
                scanned = row["n_observed"] - row["n_skipped"]
                lines.append(
                    f"  - cve_disappears rate: {cve_rate:.4f} "
                    f"(over {scanned} non-skipped trial(s); 1.0 means the "
                    "named CVE was absent from every scanned trial)"
                )
    return "\n".join(lines)


def _format_failure_classes(counts: Mapping[str, int]) -> str:
    if not counts:
        return "- (no failures)"
    lines = ["| failure_class | count |", "|---------------|-------|"]
    for cls, count in counts.items():
        lines.append(f"| {cls} | {count} |")
    return "\n".join(lines)


def format_report(data: Mapping[str, Any]) -> str:
    """Render the aggregated report as markdown using plain f-strings.

    ``data`` is the dict produced by :func:`build_report_data`. The gold
    section is omitted entirely when ``data['gold_anchor']`` is ``None``.
    """
    summary = data.get("summary") or {}
    migration_id = summary.get("migration_id", "unknown")
    agent_model = summary.get("agent_model", "unknown")
    variant = summary.get("variant", "unknown")
    n_trials = summary.get("n_trials", data.get("n_trials", 0))

    funnel_md = _format_funnel_table(data["funnel"])
    contamination_md = _format_contamination(data["contamination"])
    gold_md = _format_gold_section(data.get("gold_anchor"))
    stamps_md = _format_stamps(data["stamps"])
    failure_md = _format_failure_classes(data["failure_classes"])
    quality_md = _format_quality_table(data.get("quality") or [])
    cost_md = _format_cost_section(data.get("cost") or {})
    efficiency_md = _format_efficiency_section(data.get("efficiency"))

    parts = [
        "# Migration Eval Funnel Report",
        "",
        f"- migration_id: `{migration_id}`",
        f"- agent_model: `{agent_model}`",
        f"- variant: `{variant}`",
        f"- n_trials: {n_trials}",
        "",
        "## 1. Funnel",
        "",
        funnel_md,
        "",
        "## 2. Contamination Split",
        "",
        contamination_md,
        "",
    ]
    base_idx = 3
    if gold_md is not None:
        parts.extend(
            [
                f"## {base_idx}. Gold Anchor Correlation",
                "",
                gold_md,
                "",
            ]
        )
        base_idx += 1
    parts.extend(
        [
            f"## {base_idx}. Spec Stamps",
            "",
            stamps_md,
            "",
            f"## {base_idx + 1}. Failure Class Breakdown",
            "",
            failure_md,
            "",
            f"## {base_idx + 2}. Batch-change quality",
            "",
            quality_md,
            "",
            f"## {base_idx + 3}. Cost",
            "",
            cost_md,
            "",
            f"## {base_idx + 4}. Iteration efficiency",
            "",
            efficiency_md,
            "",
        ]
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_report_data(
    run_dir: Path,
    *,
    model_cutoff_date: date | None = None,
    gold_path: Path | None = None,
) -> dict[str, Any]:
    """Aggregate ``run_dir`` into the dict consumed by :func:`format_report`."""
    run_dir = Path(run_dir)
    results = list(_iter_results(run_dir))
    summary = _load_summary(run_dir)

    cutoff = (
        model_cutoff_date or _coerce_cutoff(summary.get("model_cutoff_date")) or _DEFAULT_CUTOFF
    )

    funnel_rows = _funnel_counts(results)
    contamination = split_scores(results, cutoff).to_dict()
    failure_classes = _failure_class_counts(results)
    stamps = _stamp_block(results, summary)

    gold_report: CorrelationReport | None = None
    if gold_path is not None:
        gold = load_gold_set(Path(gold_path))
        gold_report = correlate(results, gold)

    return {
        "summary": summary,
        "n_trials": len(results),
        "funnel": funnel_rows,
        "contamination": contamination,
        "gold_anchor": gold_report,
        "stamps": stamps,
        "failure_classes": failure_classes,
        "quality": _quality_aggregate(results),
        "cost": _cost_aggregate(results),
        "efficiency": _efficiency_aggregate(results),
    }


def generate_report(
    run_dir: Path,
    out_path: Path,
    *,
    model_cutoff_date: date | None = None,
    gold_path: Path | None = None,
) -> int:
    """Aggregate ``run_dir`` and write a markdown report to ``out_path``."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        print(f"error: run directory does not exist: {run_dir}", file=sys.stderr)
        return 2

    data = build_report_data(
        run_dir,
        model_cutoff_date=model_cutoff_date,
        gold_path=gold_path,
    )
    markdown = format_report(data)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown)
    print(f"report: wrote {out_path}", file=sys.stderr)
    return 0


__all__ = [
    "build_report_data",
    "format_report",
    "generate_report",
]
