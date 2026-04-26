#!/usr/bin/env python3
"""End-to-end eval driver: changesets -> funnel -> result.json (vj9.5).

Wires three pieces together for a single migration:

    1. ChangesetProvider           (scripts/pull_changesets.stage_instance)
       Pulls each instance into <eval-root>/<id>/{repo, repo/patch.diff,
       meta.json}.
    2. <eval-root>/<id>/repo/meta.json synthesis
       Combines the migration's recipe template (configs/recipes/<mig>.yaml)
       with the changeset provenance (repo_url, commit_sha, workflow_id,
       agent_runner, agent_model) so the funnel runner can build a
       Recipe(dockerfile, build_cmd, test_cmd) and stamp the result.json
       payload with the right migration_id / agent_model.
    3. runner.run_from_config()
       Iterates the staged repos through the tiered-oracle funnel and
       writes one result.json per trial under --output-root.

The driver is offline-capable via Tier-0 only (``--stages diff``); the
real-data path uses ``adapters.sandbox_provider: docker`` (configured
via the recipe template or the YAML overlay) and at minimum
``--stages diff,compile,tests``. Tier 3 (LLM judge) is out of scope
for the POC and intentionally not wired here.

The acceptance gate for this driver — committing a calibrated
``docs/poc_tier1_report.md`` from N>=10 real workflow instance outputs
— is documented in ``docs/tier1_skip.md``. The driver itself ships
ready and is exercised by ``tests/test_run_eval.py`` against a local
file:// remote.

Usage
-----
    # Smoke against pre-staged changesets, Tier-0 only
    python scripts/run_eval.py \\
        --migration java8_17 \\
        --provider filesystem --root /tmp/staged \\
        --eval-root /tmp/eval \\
        --output-root runs/analysis/mig_java8_17/claude-sonnet-4-6/poc \\
        --variant poc \\
        --stages diff \\
        inst-1 inst-2

    # Real run with Docker sandbox and tiers 0..2
    python scripts/run_eval.py \\
        --migration java8_17 \\
        --provider filesystem --root /tmp/staged \\
        --eval-root /tmp/eval \\
        --output-root runs/analysis/mig_java8_17/claude-sonnet-4-6/poc \\
        --variant poc \\
        --stages diff,compile,tests \\
        --sandbox-provider docker \\
        inst-1 inst-2 inst-3

Reports are produced separately by the existing CLI:

    python -m migration_evals.cli report \\
        --run runs/analysis/mig_java8_17/claude-sonnet-4-6/poc \\
        --out docs/poc_tier1_report.md

Exit codes
----------
0   All instances pulled, funnel ran, result.json emitted for each.
1   Wrong CLI usage (unknown migration, missing recipe, etc.).
2   At least one instance failed to pull (its result.json is absent),
    OR the funnel runner exited non-zero. Per-instance funnel failures
    that the funnel handles internally (failure_class=agent_error) do
    not change the exit code — they are recorded in result.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from migration_evals.changesets import get_provider  # noqa: E402
from migration_evals.runner import STAGE_TO_TIER, run_from_config  # noqa: E402

import pull_changesets  # noqa: E402


def load_recipe_template(path: Path) -> dict[str, Any]:
    """Load a per-migration recipe template (configs/recipes/<mig>.yaml).

    Required keys: ``migration_id``, ``recipe.dockerfile``,
    ``recipe.build_cmd``, ``recipe.test_cmd``, ``stamps.oracle_spec``,
    ``stamps.recipe_spec``, ``stamps.hypotheses``. ``model_cutoff_date``
    is optional.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"recipe template at {path} must be a YAML mapping")
    for key in ("migration_id", "recipe", "stamps"):
        if key not in raw:
            raise ValueError(f"recipe template at {path} missing required key {key!r}")
    for key in ("dockerfile", "build_cmd", "test_cmd"):
        if key not in raw["recipe"]:
            raise ValueError(f"recipe template at {path} missing recipe.{key}")
    for key in ("oracle_spec", "recipe_spec", "hypotheses"):
        if key not in raw["stamps"]:
            raise ValueError(f"recipe template at {path} missing stamps.{key}")
    return dict(raw)


def synthesize_repo_meta(inst_root: Path, template: Mapping[str, Any]) -> None:
    """Write ``<inst_root>/repo/meta.json`` merging the recipe template
    with the changeset provenance at ``<inst_root>/meta.json``.

    The runner's ``_load_repo_meta`` reads this file to construct the
    funnel's per-trial Recipe and to stamp result.json with
    ``migration_id`` / ``agent_model``.
    """
    provenance = json.loads((inst_root / "meta.json").read_text(encoding="utf-8"))
    repo_meta = {
        "task_id": inst_root.name,
        "migration_id": template["migration_id"],
        "agent_model": provenance["agent_model"],
        "agent_runner": provenance["agent_runner"],
        "workflow_id": provenance["workflow_id"],
        "repo_url": provenance["repo_url"],
        "commit_sha": provenance["commit_sha"],
        "dockerfile": template["recipe"]["dockerfile"],
        "build_cmd": template["recipe"]["build_cmd"],
        "test_cmd": template["recipe"]["test_cmd"],
        "is_synthetic": False,
        "harness_provenance": {
            "model": provenance["agent_model"],
            "prompt_version": "agent-pipeline-v1",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        },
    }
    (inst_root / "repo" / "meta.json").write_text(
        json.dumps(repo_meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_runner_config(
    *,
    template: Mapping[str, Any],
    funnel_input_paths: list[Path],
    output_root: Path,
    variant: str,
    stages: list[str],
    sandbox_provider: str,
    agent_model: str,
    agent_runner: str,
) -> dict[str, Any]:
    """Build the YAML config dict consumed by ``runner.run_from_config``.

    One ``repos`` entry per funnel-input path. The runner uses
    ``path.name`` as both the trial-dir suffix and the task_id
    discriminator, so each path's basename must be the instance id.
    """
    repos = [
        {"path": str(p), "seed": idx + 1}
        for idx, p in enumerate(funnel_input_paths)
    ]
    cfg: dict[str, Any] = {
        "migration_id": template["migration_id"],
        "agent_model": agent_model,
        "agent_runner": agent_runner,
        "variant": variant,
        "output_root": str(output_root),
        "stages": stages,
        "repos": repos,
        "adapters": {
            "sandbox_provider": sandbox_provider,
        },
        "stamps": {
            "oracle_spec": template["stamps"]["oracle_spec"],
            "recipe_spec": template["stamps"]["recipe_spec"],
            "hypotheses": template["stamps"]["hypotheses"],
        },
    }
    cutoff = template.get("model_cutoff_date")
    if cutoff is not None:
        cfg["model_cutoff_date"] = cutoff
    return cfg


def emit_manifest(
    output_root: Path, template: Mapping[str, Any], repo_root: Path
) -> Path:
    """Write ``<output_root>/manifest.json`` from the recipe's stamps block.

    Spec paths are rewritten as paths relative to ``output_root`` so the
    committed manifest stays portable across machines.
    """
    stamps = template["stamps"]
    keys = ["oracle_spec", "recipe_spec", "hypotheses"]
    if "prompt_spec" in stamps:
        keys.append("prompt_spec")
    output_resolved = output_root.resolve()
    manifest: dict[str, str] = {}
    for key in keys:
        spec_abs = (repo_root / stamps[key]).resolve()
        manifest[key] = os.path.relpath(spec_abs, output_resolved)
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _parse_stages(raw: str) -> list[str]:
    """Parse a comma-separated --stages value, validate against the runner's
    canonical stage map."""
    stages = [s.strip() for s in raw.split(",") if s.strip()]
    bad = [s for s in stages if s not in STAGE_TO_TIER]
    if bad:
        raise ValueError(
            f"unknown stages {bad}; valid: {', '.join(sorted(STAGE_TO_TIER))}"
        )
    return stages


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_eval",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--migration", required=True, help="Migration id (e.g. java8_17).")
    parser.add_argument("--provider", default="filesystem", help="ChangesetProvider name.")
    parser.add_argument("--root", default=None, help="Root dir for the filesystem provider.")
    parser.add_argument(
        "--eval-root",
        default="/tmp/eval",
        help="Where pull_changesets stages each instance. Default: /tmp/eval.",
    )
    parser.add_argument(
        "--output-root",
        required=True,
        help="Where the funnel writes result.json files (one per trial).",
    )
    parser.add_argument("--variant", required=True, help="Run variant tag (e.g. 'poc', 'smoke').")
    parser.add_argument(
        "--stages",
        default="diff,compile,tests",
        help="Comma-separated funnel stages. Default: diff,compile,tests (skip judge for POC).",
    )
    parser.add_argument(
        "--sandbox-provider",
        default="cassette",
        choices=("cassette", "docker"),
        help="Sandbox backend for compile/tests tiers. Default: cassette (offline).",
    )
    parser.add_argument(
        "--instance-ids-file",
        default=None,
        help="Newline-delimited file of instance ids (# comments ok).",
    )
    parser.add_argument(
        "instance_ids",
        nargs="*",
        help="Instance ids (positional). Combined with --instance-ids-file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    recipe_path = _REPO_ROOT / "configs" / "recipes" / f"{args.migration}.yaml"
    if not recipe_path.is_file():
        print(
            f"error: no recipe template at {recipe_path}. "
            f"Add configs/recipes/{args.migration}.yaml or pick a known migration.",
            file=sys.stderr,
        )
        return 1
    try:
        template = load_recipe_template(recipe_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        stages = _parse_stages(args.stages)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    ids: list[str] = list(args.instance_ids)
    if args.instance_ids_file:
        ids.extend(pull_changesets.load_instance_ids(Path(args.instance_ids_file)))
    if not ids:
        print("error: no instance ids supplied", file=sys.stderr)
        return 1

    config: dict[str, str] = {}
    if args.root is not None:
        config["root"] = args.root
    try:
        provider = get_provider(args.provider, config)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    eval_root = Path(args.eval_root)
    output_root = Path(args.output_root)

    pulled_ok, pull_failures = _pull_all(provider, ids, eval_root)
    if not pulled_ok:
        print("error: no instances pulled successfully", file=sys.stderr)
        return 2

    for inst_root in pulled_ok:
        synthesize_repo_meta(inst_root, template)

    funnel_input_paths = _build_funnel_input_layout(pulled_ok, eval_root)

    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = emit_manifest(output_root, template, _REPO_ROOT)
    print(f"wrote manifest: {manifest_path}")
    cfg = build_runner_config(
        template=template,
        funnel_input_paths=funnel_input_paths,
        output_root=output_root,
        variant=args.variant,
        stages=stages,
        sandbox_provider=args.sandbox_provider,
        agent_model=_first_meta_field(pulled_ok, "agent_model"),
        agent_runner=_first_meta_field(pulled_ok, "agent_runner"),
    )
    cfg_path = output_root / "run_eval_config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    print(f"wrote runner config: {cfg_path}")

    runner_rc = run_from_config(cfg_path)
    if runner_rc != 0:
        print(f"error: runner exited {runner_rc}", file=sys.stderr)
        return 2
    return 2 if pull_failures else 0


def _pull_all(
    provider: Any, ids: Iterable[str], eval_root: Path
) -> tuple[list[Path], int]:
    """Stage each id via pull_changesets, partition into ok/failed."""
    pulled_ok: list[Path] = []
    failures = 0
    for iid in ids:
        r = pull_changesets.stage_instance(provider, iid, eval_root)
        if r.staged_dir is not None and (r.staged_dir / "repo").is_dir():
            pulled_ok.append(r.staged_dir)
            note = f" ({r.error})" if r.error else ""
            print(f"[pull OK] {r.instance_id}: {r.staged_dir}{note}")
        else:
            failures += 1
            print(f"[pull FAIL] {r.instance_id}: {r.error}", file=sys.stderr)
    return pulled_ok, failures


def _build_funnel_input_layout(
    pulled_ok: list[Path], eval_root: Path
) -> list[Path]:
    """Symlink each <eval-root>/<id>/repo into <eval-root>/funnel-input/<id>.

    The runner derives both trial_dir and task_id from path.name, so the
    symlink basename must be the instance id rather than the literal
    "repo" subdirectory pull_changesets writes.
    """
    funnel_input_root = eval_root / "funnel-input"
    funnel_input_root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for inst_root in pulled_ok:
        link = funnel_input_root / inst_root.name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to((inst_root / "repo").resolve())
        paths.append(link)
    return paths


def _first_meta_field(staged: Iterable[Path], key: str) -> str:
    """Return the first staged instance's value for ``meta.json[key]``.

    All instances in a single run share agent_model and agent_runner —
    those values stamp the output_root path and the result.json
    payload — so the first one is canonical for the run as a whole.
    """
    for inst_root in staged:
        meta = json.loads((inst_root / "meta.json").read_text(encoding="utf-8"))
        return str(meta[key])
    raise RuntimeError(f"no staged instances; cannot resolve meta field {key!r}")


if __name__ == "__main__":
    raise SystemExit(main())
