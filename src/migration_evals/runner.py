"""Config-driven runner for the migration eval framework.

Consumes a YAML configuration, iterates the declared repos through the
tiered oracle funnel, and writes one ``result.json`` per trial under
``output_root/<repo_name>_<seed>/result.json``.

Design notes
------------
* Adapter instantiation mirrors the cassette-replay pattern used by
  ``migration_evals.cli`` so the smoke path never touches the
  network.
* Each emitted payload is stamped via :func:`pre_reg.stamp_result` so the
  three SHA fields (oracle / recipe / pre-reg) are always present.
* When the funnel already assigned a ``failure_class`` (because a tier
  short-circuited), we propagate it. Otherwise - and only when
  ``success=False`` - we fall back to ``failure_class.classify`` against
  the newly-written trial directory. Success cases always get ``null``.
* Every payload validates against ``schemas/mig_result.schema.json`` by
  construction; the CLI smoke test enforces this at runtime too.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from migration_evals.adapters_anthropic import build_anthropic_adapter
from migration_evals.adapters_docker import build_sandbox_adapter
from migration_evals.cli import (
    _build_recipe_from_meta,
    _load_repo_meta,
)
from migration_evals.failure_class import classify as classify_failure
from migration_evals.funnel import run_funnel
from migration_evals.quality_spec import QualitySpec
from migration_evals.pre_reg import stamp_result
from migration_evals.types import FailureClass

LOG = logging.getLogger(__name__)

# Tier -> CLI stage name, used when config enumerates stages.
STAGE_TO_TIER = {
    "diff": "diff_valid",
    "compile": "compile_only",
    "tests": "tests",
    "judge": "judge",
    "daikon": "daikon",
    "ast": "ast_conformance",
}


@dataclass(frozen=True)
class RepoEntry:
    """A single repo to evaluate as part of a config-driven run."""

    path: Path
    seed: int


def _parse_repo_entries(raw: Sequence[Mapping[str, Any]]) -> list[RepoEntry]:
    entries: list[RepoEntry] = []
    for idx, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise ValueError(f"repos[{idx}] must be a mapping; got {type(row).__name__}")
        if "path" not in row:
            raise ValueError(f"repos[{idx}] missing required field 'path'")
        if "seed" not in row:
            raise ValueError(f"repos[{idx}] missing required field 'seed'")
        entries.append(RepoEntry(path=Path(str(row["path"])), seed=int(row["seed"])))
    return entries


def _resolve_stages_for_config(stages_cfg: Optional[Sequence[str]]) -> Optional[tuple[str, ...]]:
    """Convert the config's ``stages`` list to funnel tier names.

    Returns ``None`` when the config omits the key (run every tier the
    funnel allows).
    """
    if not stages_cfg:
        return None
    tiers: list[str] = []
    for stage in stages_cfg:
        tier = STAGE_TO_TIER.get(str(stage))
        if tier is None:
            raise ValueError(
                f"unknown stage {stage!r} in config; expected one of "
                f"{sorted(STAGE_TO_TIER)}"
            )
        tiers.append(tier)
    return tuple(tiers)


def _build_payload(
    *,
    repo_entry: RepoEntry,
    repo_meta: Mapping[str, Any],
    funnel_result: Any,
    migration_id: str,
    agent_model: str,
    agent_runner: Optional[str],
    iterator_id: Optional[str],
    started_at: Optional[str],
    finished_at: Optional[str],
    variant: str,
    model_cutoff_date: Optional[date],
) -> dict[str, Any]:
    """Compose the base result payload (pre-stamp)."""
    success = bool(funnel_result.final_verdict.passed)
    score = 1.0 if success else 0.0
    repo_created_at = repo_meta.get("repo_created_at")
    pre_score: Optional[float] = None
    post_score: Optional[float] = None
    # Per-trial pre/post fields: the repo is pre-cutoff iff its created
    # date is strictly before the model cutoff. Both fields are populated
    # (one as the trial's score, the other as ``null``) so the aggregate
    # report can bucket without re-parsing dates.
    created_date = _parse_date(repo_created_at)
    if model_cutoff_date is not None and created_date is not None:
        if created_date < model_cutoff_date:
            pre_score = score
        else:
            post_score = score
    else:
        # No cutoff or no date -> default both to the trial score so the
        # result.json still satisfies the required-number schema fields.
        pre_score = score
        post_score = score

    task_id = f"{migration_id}::{repo_entry.path.name}"
    payload: dict[str, Any] = {
        "task_id": task_id,
        "agent_model": agent_model,
        "agent_runner": agent_runner,
        "iterator_id": iterator_id,
        "migration_id": migration_id,
        "variant": variant,
        "seed": repo_entry.seed,
        "repo_path": str(repo_entry.path),
        "repo_created_at": repo_created_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "success": success,
        "failure_class": funnel_result.failure_class,
        "oracle_tier": funnel_result.final_verdict.tier,
        "score_pre_cutoff": pre_score if pre_score is not None else 0.0,
        "score_post_cutoff": post_score if post_score is not None else 0.0,
        # Spec-SHA placeholders; stamp_result() overwrites them.
        "oracle_spec_sha": "",
        "recipe_spec_sha": "",
        "pre_reg_sha": "",
        "funnel": funnel_result.to_dict(),
    }
    return payload


def _parse_date(raw: Any) -> Optional[date]:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _coerce_cutoff(raw: Any) -> Optional[date]:
    return _parse_date(raw)


def _finalize_failure_class(
    payload: Mapping[str, Any],
    trial_dir: Path,
) -> Optional[str]:
    """Pick the final failure_class value for the trial.

    * Success trials => always ``None``.
    * Funnel-assigned class wins when present.
    * Otherwise fall back to :func:`failure_class.classify` against the
      newly-written trial directory.
    """
    if bool(payload.get("success")):
        return None
    existing = payload.get("failure_class")
    if isinstance(existing, str) and existing:
        return existing
    classified = classify_failure(trial_dir)
    if isinstance(classified, FailureClass):
        return classified.value
    return FailureClass.AGENT_ERROR.value


def run_from_config(config_path: Path) -> int:
    """Execute a config-driven migration eval run.

    Returns an exit code. Writes one ``result.json`` per repo under
    ``output_root``. A ``summary.json`` is written at ``output_root`` with
    the three stamps + run metadata to make the report step trivially
    quick.
    """
    config_path = Path(config_path)
    if not config_path.is_file():
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        return 2

    raw_cfg = yaml.safe_load(config_path.read_text())
    if not isinstance(raw_cfg, Mapping):
        print(
            f"error: config at {config_path} must decode to a mapping; "
            f"got {type(raw_cfg).__name__}",
            file=sys.stderr,
        )
        return 2

    try:
        migration_id = str(raw_cfg["migration_id"])
        agent_model = str(raw_cfg["agent_model"])
        variant = str(raw_cfg["variant"])
        output_root = Path(str(raw_cfg["output_root"]))
        repos_raw = raw_cfg["repos"]
    except KeyError as exc:
        print(f"error: config missing required key: {exc.args[0]!r}", file=sys.stderr)
        return 2

    agent_runner_raw = raw_cfg.get("agent_runner")
    agent_runner: Optional[str] = (
        str(agent_runner_raw) if agent_runner_raw else None
    )
    iterator_id_raw = raw_cfg.get("iterator_id")
    iterator_id: Optional[str] = (
        str(iterator_id_raw) if iterator_id_raw else None
    )

    if not isinstance(repos_raw, Sequence) or not repos_raw:
        print("error: config 'repos' must be a non-empty list", file=sys.stderr)
        return 2

    repo_entries = _parse_repo_entries(list(repos_raw))
    stages = _resolve_stages_for_config(raw_cfg.get("stages"))
    cutoff = _coerce_cutoff(raw_cfg.get("model_cutoff_date"))

    adapters_cfg = raw_cfg.get("adapters") or {}
    sandbox_cassette_dir = _as_path(adapters_cfg.get("sandbox_cassette_dir"))
    anthropic_cassette_dir = _as_path(adapters_cfg.get("anthropic_cassette_dir"))
    quality_cfg = raw_cfg.get("quality") or {}
    quality_spec = QualitySpec.from_dict(quality_cfg)

    stamps_cfg = raw_cfg.get("stamps") or {}
    oracle_spec = _as_path(stamps_cfg.get("oracle_spec"))
    recipe_spec = _as_path(stamps_cfg.get("recipe_spec"))
    hypotheses = _as_path(stamps_cfg.get("hypotheses"))
    prompt_spec = _as_path(stamps_cfg.get("prompt_spec"))
    if oracle_spec is None or recipe_spec is None or hypotheses is None:
        print(
            "error: config 'stamps' must include oracle_spec, recipe_spec, "
            "and hypotheses paths",
            file=sys.stderr,
        )
        return 2

    # Recipe templates may declare a top-level `sandbox_policy:` block so
    # per-recipe defaults live alongside the recipe rather than being
    # duplicated in every smoke YAML. The smoke config still wins per
    # key (shallow merge) when both sources set the same flag.
    recipe_template_policy = _load_recipe_template_sandbox_policy(recipe_spec)
    smoke_policy = adapters_cfg.get("sandbox_policy")
    merged_policy = _merge_sandbox_policy(recipe_template_policy, smoke_policy)
    if merged_policy is not None:
        adapters_cfg = {**adapters_cfg, "sandbox_policy": merged_policy}

    output_root.mkdir(parents=True, exist_ok=True)
    written = 0
    for repo_entry in repo_entries:
        if not repo_entry.path.is_dir():
            print(
                f"error: repo path does not exist: {repo_entry.path}",
                file=sys.stderr,
            )
            return 2
        meta = _load_repo_meta(repo_entry.path)
        recipe = _build_recipe_from_meta(meta)
        adapters = {
            "sandbox": build_sandbox_adapter(
                repo_path=repo_entry.path,
                adapters_cfg=adapters_cfg,
                cassette_dir=sandbox_cassette_dir,
            ),
            "anthropic": build_anthropic_adapter(
                repo_path=repo_entry.path,
                adapters_cfg=adapters_cfg,
                cassette_dir=anthropic_cassette_dir,
            ),
            "enable_daikon": False,
            "quality_spec": quality_spec,
        }
        trial_started_at = datetime.now(tz=timezone.utc).isoformat()
        funnel_result = run_funnel(
            repo_entry.path,
            recipe,
            adapters,
            is_synthetic=bool(meta.get("is_synthetic", False)),
            stages=stages,
        )
        trial_finished_at = datetime.now(tz=timezone.utc).isoformat()
        base_payload = _build_payload(
            repo_entry=repo_entry,
            repo_meta=meta,
            funnel_result=funnel_result,
            migration_id=migration_id,
            agent_model=agent_model,
            agent_runner=agent_runner,
            iterator_id=iterator_id,
            started_at=trial_started_at,
            finished_at=trial_finished_at,
            variant=variant,
            model_cutoff_date=cutoff,
        )
        stamped = stamp_result(
            base_payload, oracle_spec, recipe_spec, hypotheses, prompt_spec
        )

        trial_dir = output_root / f"{repo_entry.path.name}_{repo_entry.seed}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        result_path = trial_dir / "result.json"
        result_path.write_text(json.dumps(stamped, indent=2, sort_keys=True) + "\n")

        # After writing, re-resolve failure_class using the newly-available
        # trial artifacts. This is a no-op when the funnel already assigned
        # one (the common case); it exists so future trial writers that emit
        # status.txt / logs still get a correct classification.
        final_class = _finalize_failure_class(stamped, trial_dir)
        if final_class != stamped.get("failure_class"):
            stamped["failure_class"] = final_class
            result_path.write_text(json.dumps(stamped, indent=2, sort_keys=True) + "\n")
        written += 1

    summary_stamps = {
        "oracle_spec_sha": _sha_of(oracle_spec),
        "recipe_spec_sha": _sha_of(recipe_spec),
        "pre_reg_sha": _sha_of(hypotheses),
    }
    if prompt_spec is not None:
        summary_stamps["prompt_sha"] = _sha_of(prompt_spec)
    summary = {
        "migration_id": migration_id,
        "agent_model": agent_model,
        "agent_runner": agent_runner,
        "iterator_id": iterator_id,
        "variant": variant,
        "output_root": str(output_root),
        "n_trials": written,
        "model_cutoff_date": raw_cfg.get("model_cutoff_date"),
        "stamps": summary_stamps,
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(
        f"run: wrote {written} result.json files under {output_root}",
        file=sys.stderr,
    )
    return 0


def _as_path(raw: Any) -> Optional[Path]:
    if raw is None:
        return None
    if isinstance(raw, Path):
        return raw
    if isinstance(raw, str) and raw:
        return Path(raw)
    return None


def _load_recipe_template_sandbox_policy(
    recipe_spec: Optional[Path],
) -> Mapping[str, Any]:
    """Return the recipe template's top-level ``sandbox_policy`` block.

    Returns ``{}`` for any failure mode (missing path, unreadable file,
    invalid YAML, non-mapping block) so the caller can treat "no
    template policy" and "broken template policy" identically — the
    recipe template is consulted as a soft-default source, not a
    correctness gate.
    """
    if recipe_spec is None or not recipe_spec.is_file():
        return {}
    try:
        data = yaml.safe_load(recipe_spec.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    if not isinstance(data, Mapping):
        return {}
    block = data.get("sandbox_policy")
    if not isinstance(block, Mapping):
        return {}
    return dict(block)


def _merge_sandbox_policy(
    recipe_policy: Mapping[str, Any],
    smoke_policy: Optional[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    """Shallow-merge a recipe-template policy with a smoke-YAML policy.

    Smoke wins per key — recipe values fill in keys the smoke config
    omits, but the smoke config can override or zero-out any recipe
    default. Returns ``None`` when neither source provides any keys, so
    the caller can leave ``adapters_cfg`` untouched in the common case.
    """
    merged: dict[str, Any] = dict(recipe_policy)
    if smoke_policy:
        for key, value in smoke_policy.items():
            merged[key] = value
    return merged if merged else None


def _sha_of(path: Path) -> str:
    from migration_evals.pre_reg import compute_spec_sha

    return compute_spec_sha(path)


__all__ = ["RepoEntry", "run_from_config"]
