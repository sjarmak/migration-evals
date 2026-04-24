"""Funnel report generator for the migration eval framework.

Aggregates a directory of per-trial ``result.json`` files into a single
markdown report. Sections (in order):

1. Funnel table — per-tier n_entered/n_passed/n_failed/cumulative_pass_rate.
2. Contamination split — score_pre_cutoff, score_post_cutoff, gap_pp, warning.
3. Gold-anchor correlation — point + 95% CI + eval_broken (optional).
4. Spec stamps — oracle_spec_sha / recipe_spec_sha / pre_reg_sha.
5. Failure-class breakdown — count per class across failed trials.

Rendering strategy
------------------
The rendering path is a hand-rolled :func:`format_report` that uses
f-strings. Jinja2 is listed as available at dev time but we deliberately
avoid depending on it at runtime — see docs/migration_eval/usage.md. The
companion ``templates/report.md.j2`` file is a reference-only artifact
that documents the target structure.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from migration_evals.contamination import split_scores
from migration_evals.gold_anchor import CorrelationReport, correlate, load_gold_set

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


def _coerce_cutoff(raw: Any) -> Optional[date]:
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


def _funnel_counts(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Compute per-tier funnel counts.

    For each tier:

    * ``n_entered`` = number of trials whose ``per_tier_verdict`` includes
      the tier (i.e. the cascade reached it).
    * ``n_passed`` = number of trials whose verdict for that tier was
      ``passed=True``.
    * ``n_failed`` = n_entered - n_passed.
    * ``cumulative_pass_rate`` = n_passed / total_trials — gives the
      fraction of trials that made it past the tier.
    """
    total = len(results)
    rows: list[dict[str, Any]] = []
    for tier in _TIER_ORDER:
        n_entered = 0
        n_passed = 0
        for row in results:
            verdicts = (row.get("funnel") or {}).get("per_tier_verdict") or []
            for verdict in verdicts:
                if not isinstance(verdict, Mapping):
                    continue
                if verdict.get("tier") != tier:
                    continue
                n_entered += 1
                if bool(verdict.get("passed")):
                    n_passed += 1
                break
        n_failed = n_entered - n_passed
        cumulative = (n_passed / total) if total > 0 else 0.0
        rows.append(
            {
                "tier_name": tier,
                "n_entered": n_entered,
                "n_passed": n_passed,
                "n_failed": n_failed,
                "cumulative_pass_rate": round(cumulative, 6),
            }
        )
    return rows


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
    lines = [
        "| tier_name | n_entered | n_passed | n_failed | cumulative_pass_rate |",
        "|-----------|-----------|----------|----------|----------------------|",
    ]
    for row in rows:
        lines.append(
            f"| {row['tier_name']} | {row['n_entered']} | {row['n_passed']} | "
            f"{row['n_failed']} | {row['cumulative_pass_rate']:.4f} |"
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


def _format_gold_section(report: Optional[CorrelationReport]) -> Optional[str]:
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
    if gold_md is not None:
        parts.extend(
            [
                "## 3. Gold Anchor Correlation",
                "",
                gold_md,
                "",
                "## 4. Spec Stamps",
                "",
                stamps_md,
                "",
                "## 5. Failure Class Breakdown",
                "",
                failure_md,
                "",
            ]
        )
    else:
        parts.extend(
            [
                "## 3. Spec Stamps",
                "",
                stamps_md,
                "",
                "## 4. Failure Class Breakdown",
                "",
                failure_md,
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
    model_cutoff_date: Optional[date] = None,
    gold_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Aggregate ``run_dir`` into the dict consumed by :func:`format_report`."""
    run_dir = Path(run_dir)
    results = list(_iter_results(run_dir))
    summary = _load_summary(run_dir)

    cutoff = (
        model_cutoff_date
        or _coerce_cutoff(summary.get("model_cutoff_date"))
        or _DEFAULT_CUTOFF
    )

    funnel_rows = _funnel_counts(results)
    contamination = split_scores(results, cutoff).to_dict()
    failure_classes = _failure_class_counts(results)
    stamps = _stamp_block(results, summary)

    gold_report: Optional[CorrelationReport] = None
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
    }


def generate_report(
    run_dir: Path,
    out_path: Path,
    *,
    model_cutoff_date: Optional[date] = None,
    gold_path: Optional[Path] = None,
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
