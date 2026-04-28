"""CLI entry point for the migration eval framework.

Subcommands:
    run         Launch a migration eval run from a config file or fixture dir.
    report      Summarize results across one or more runs (stub).
    regression  Diff two runs to surface newly-failing tasks.
    harness     Synthesize or validate LLM-inferred build harnesses (stub).
    probe       Run a falsification probe against a target ecosystem (stub).

The ``run`` subcommand supports a ``--stage`` filter (one of
``compile``, ``tests``, ``judge``, ``daikon``, ``all``) and operates
over a directory of fixture repos for the cassette-replay path. Real
remote-execution integration is wired in via the adapter layer; the CLI
itself never imports a vendor SDK.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

SUBCOMMANDS = ("run", "report", "regression", "harness", "probe")

STAGE_CHOICES = ("compile", "tests", "judge", "daikon", "all")
DEFAULT_STAGE = "all"

# These constants are placeholders that match the real spec SHAs once they
# are wired through. Result.json carries them verbatim so downstream tools
# can detect spec drift.
DEFAULT_ORACLE_SPEC_SHA = "oracle-spec-v0"
DEFAULT_RECIPE_SPEC_SHA = "recipe-spec-v0"
DEFAULT_PRE_REG_SHA = "pre-reg-v0"


def _stub(name: str) -> int:
    """Placeholder handler for a subcommand whose real logic has not landed."""
    print(
        f"[migration_eval] '{name}' is a scaffold stub - "
        "downstream work units will wire up real behaviour.",
        file=sys.stderr,
    )
    return 0


def _add_run(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "run",
        help="Launch a migration eval run.",
        description=(
            "Launch a migration eval run. With --repos, iterates fixture "
            "repos and writes per-repo result.json files using the tiered "
            "oracle funnel."
        ),
    )
    p.add_argument(
        "config_positional",
        nargs="?",
        default=None,
        help="Path to run config YAML (positional form).",
    )
    p.add_argument(
        "--config",
        dest="config",
        default=None,
        help="Path to run config YAML (same as positional).",
    )
    p.add_argument(
        "--stage",
        choices=STAGE_CHOICES,
        default=DEFAULT_STAGE,
        help="Restrict the funnel to a single tier (default: all).",
    )
    p.add_argument(
        "--repos",
        default=None,
        help="Directory of repo subdirectories to evaluate.",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Output directory for per-repo result.json files.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of repos to evaluate.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the run without executing it.",
    )


def _add_report(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "report",
        help="Summarize results across one or more runs.",
        description=(
            "Aggregate a directory of result.json files into a funnel "
            "markdown report with tier counts, contamination split, optional "
            "gold-anchor correlation, spec stamps, and failure-class counts."
        ),
    )
    p.add_argument(
        "--run",
        dest="run_dir",
        default=None,
        help="Run directory containing per-trial result.json files.",
    )
    p.add_argument(
        "--out",
        dest="out",
        default=None,
        help="Output markdown path.",
    )
    p.add_argument(
        "--gold",
        dest="gold_path",
        default=None,
        help="Optional path to a gold-anchor JSON file.",
    )
    p.add_argument(
        "--cutoff",
        dest="cutoff",
        default=None,
        help="Override model cutoff date (YYYY-MM-DD).",
    )


def _add_regression(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "regression",
        help="Diff two runs to surface newly-failing tasks.",
        description=(
            "Produce a regression diff between two directories of result.json "
            "files. Emits a markdown report with one row per newly-failing task."
        ),
    )
    p.add_argument("--from", dest="from_ref", default=None, help="Baseline directory.")
    p.add_argument("--to", dest="to_ref", default=None, help="Candidate directory.")
    p.add_argument("--out", dest="out", default=None, help="Output markdown report path.")


def _add_harness(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "harness",
        help="Synthesize or validate LLM-inferred build harnesses.",
        description="Manage harness recipes. (scaffold stub)",
    )
    p.add_argument("action", nargs="?", default=None, choices=[None, "synth", "validate"])
    p.add_argument("--repo", default=None, help="Target repository path or URL.")


def _add_probe(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "probe",
        help="Run a falsification probe against a target ecosystem.",
        description=(
            "Run a falsification probe. The Python 2→3 mode (PRD M9) "
            "stress-tests the M2/M3/M5 interfaces against a Python ecosystem "
            "and writes a findings JSON enumerating schema inadequacies. "
            "Other ecosystems still return a structured stub envelope."
        ),
    )
    p.add_argument(
        "--ecosystem",
        default="python23",
        help="Ecosystem identifier. Default: python23 (the M9 probe).",
    )
    p.add_argument(
        "--count",
        type=int,
        default=20,
        help="Number of synthetic repos to generate when no fixtures supplied.",
    )
    p.add_argument(
        "--fixture-repo-root",
        default=None,
        help="Optional path to pre-existing fixture repos; skips generation.",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Top-level RNG seed for synthetic generation.",
    )
    p.add_argument(
        "--out",
        default=None,
        help=(
            "Output directory (python23 mode) or JSON path (legacy stub). "
            "Required for the python23 ecosystem."
        ),
    )


def _add_iterator_report(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "iterator-report",
        help="Per-batch aggregate of trials grouped by iterator_id.",
        description=(
            "Walk a run directory, group result.json files by iterator_id, "
            "and emit a markdown report with per-batch completion rate, "
            "failure-class breakdown, p50/p95 latency, and total cost."
        ),
    )
    p.add_argument(
        "--run",
        dest="run_dir",
        required=True,
        help="Run directory containing per-trial result.json files.",
    )
    p.add_argument(
        "--out",
        dest="out",
        required=True,
        help="Output markdown path.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m migration_evals.cli",
        description=(
            "Migration eval framework CLI. Subcommands: run, report, "
            "iterator-report, regression, harness, probe."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{run,report,iterator-report,regression,harness,probe}",
    )
    _add_run(subparsers)
    _add_report(subparsers)
    _add_iterator_report(subparsers)
    _add_regression(subparsers)
    _add_harness(subparsers)
    _add_probe(subparsers)
    return parser


# ---------------------------------------------------------------------------
# Replay-cassette adapter wiring (used when --repos points at fixture dir)
# ---------------------------------------------------------------------------


class _CassetteSandboxAdapter:
    """Cassette-backed sandbox stand-in for fixture/replay runs.

    Reads pre-recorded ``(exit_code, stdout, stderr)`` envelopes from a
    JSON file under ``$MIGRATION_EVAL_FAKE_SANDBOX_CASSETTE_DIR``. The
    file name matches the repo directory name. Default behavior when the
    cassette is absent is to return a successful exit envelope.
    """

    def __init__(self, repo_name: str, cassette_dir: Path | None) -> None:
        self._repo_name = repo_name
        self._cassette_dir = cassette_dir
        self._records: dict[str, Mapping[str, Any]] = {}
        if cassette_dir is not None:
            cassette_path = cassette_dir / f"{repo_name}.json"
            if cassette_path.is_file():
                try:
                    self._records = dict(json.loads(cassette_path.read_text()))
                except (OSError, ValueError):
                    self._records = {}
        self._sandbox_counter = 0

    def create_sandbox(self, *, image: str, env: Any = None, cassette: Any = None) -> str:
        self._sandbox_counter += 1
        return f"sandbox-{self._repo_name}-{self._sandbox_counter}"

    def exec(
        self, sandbox_id: str, *, command: str, timeout_s: int = 600, cassette: Any = None
    ) -> Mapping[str, Any]:
        record = self._records.get(command)
        if record is None:
            # Default: pretend the command succeeded. This keeps fixture
            # scaffolding minimal - only failing cases need cassette
            # entries.
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        return dict(record)

    def destroy_sandbox(self, sandbox_id: str) -> None:  # pragma: no cover
        return None


class _CassetteAnthropicAdapter:
    """Cassette-backed Anthropic stand-in for the judge tier.

    Loads a recorded response envelope from
    ``<judge_cassette_dir>/<repo_name>.json``. Falls back to a hard-coded
    PASS envelope when no cassette is present so the funnel never blocks.
    """

    def __init__(self, repo_name: str, cassette_dir: Path | None) -> None:
        self._repo_name = repo_name
        self._cassette_dir = cassette_dir
        self.last_request: dict[str, Any] = {}
        self.call_count = 0

    def messages_create(
        self,
        *,
        model: str,
        messages: Iterable[Mapping[str, Any]],
        system: Any = None,
        max_tokens: int = 1024,
        cassette: Any = None,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        self.call_count += 1
        # Capture so a real implementation can audit the cache_control block.
        self.last_request = {
            "model": model,
            "messages": list(messages),
            "system": system,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if self._cassette_dir is not None:
            cassette_path = self._cassette_dir / f"{self._repo_name}.json"
            if cassette_path.is_file():
                try:
                    return json.loads(cassette_path.read_text())
                except (OSError, ValueError):
                    pass
        return {"content": [{"type": "text", "text": "PASS judge defaulted to pass"}]}


def _load_repo_meta(repo_dir: Path) -> dict[str, Any]:
    meta_path = repo_dir / "meta.json"
    if not meta_path.is_file():
        return {}
    try:
        return json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return {}


def _build_recipe_from_meta(meta: Mapping[str, Any]):
    """Construct a :class:`Recipe` from a fixture repo's ``meta.json``."""
    from migration_evals.harness.recipe import Recipe

    dockerfile = (
        meta.get("dockerfile") or "FROM maven:3.9-eclipse-temurin-17\nWORKDIR /src\nCOPY . .\n"
    )
    build_cmd = meta.get("build_cmd") or "mvn -B -e compile"
    test_cmd = meta.get("test_cmd") or "mvn -B -e test"
    provenance = meta.get("harness_provenance") or {
        "model": "claude-haiku-4-5",
        "prompt_version": "v1",
        "timestamp": "2026-04-24T00:00:00Z",
    }
    return Recipe(
        dockerfile=dockerfile,
        build_cmd=build_cmd,
        test_cmd=test_cmd,
        harness_provenance=provenance,
    )


def _resolve_stages(stage: str) -> tuple[str, ...] | None:
    from migration_evals.funnel import STAGE_ALIASES

    if stage == "all":
        return None
    return STAGE_ALIASES.get(stage)


def _result_payload(
    *,
    repo_dir: Path,
    meta: Mapping[str, Any],
    funnel_result: Any,
    stage: str,
) -> dict[str, Any]:
    """Compose the result.json payload for a single repo."""
    success = bool(funnel_result.final_verdict.passed)
    score = 1.0 if success else 0.0
    return {
        "task_id": str(meta.get("task_id") or repo_dir.name),
        "agent_model": str(meta.get("agent_model") or "claude-sonnet-4-6"),
        "migration_id": str(meta.get("migration_id") or "java8_17"),
        "success": success,
        "failure_class": funnel_result.failure_class,
        "oracle_tier": funnel_result.final_verdict.tier,
        "oracle_spec_sha": str(meta.get("oracle_spec_sha") or DEFAULT_ORACLE_SPEC_SHA),
        "recipe_spec_sha": str(meta.get("recipe_spec_sha") or DEFAULT_RECIPE_SPEC_SHA),
        "pre_reg_sha": str(meta.get("pre_reg_sha") or DEFAULT_PRE_REG_SHA),
        "score_pre_cutoff": score,
        "score_post_cutoff": score,
        "repo_created_at": meta.get("repo_created_at"),
        "stage_filter": stage,
        "funnel": funnel_result.to_dict(),
    }


def _handle_run(args: argparse.Namespace) -> int:
    """Real ``run`` handler: iterate repos and cascade them through the funnel.

    Two invocation modes:

    * ``--config path.yaml`` (or positional ``config``) - load a YAML
      configuration and execute the full funnel via
      :func:`migration_evals.runner.run_from_config`.
    * ``--repos dir --out dir`` - legacy fixture-driven path used by
      ``tests/migration_eval/test_funnel.py``. Kept intact so the funnel
      integration test continues to pass.
    """
    config_path = args.config or getattr(args, "config_positional", None)
    if config_path:
        from migration_evals.runner import run_from_config

        return run_from_config(Path(config_path))

    if args.repos is None:
        print(
            "error: 'run' requires either a config (positional or --config) "
            "or --repos/--out for the legacy fixture path",
            file=sys.stderr,
        )
        return 2

    repos_dir = Path(args.repos)
    if not repos_dir.is_dir():
        print(f"error: --repos directory does not exist: {repos_dir}", file=sys.stderr)
        return 2

    if args.out is None:
        print("error: --out is required when --repos is provided", file=sys.stderr)
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    stages = _resolve_stages(args.stage)
    sandbox_cassette_dir_env = os.environ.get("MIGRATION_EVAL_FAKE_SANDBOX_CASSETTE_DIR")
    sandbox_cassette_dir = Path(sandbox_cassette_dir_env) if sandbox_cassette_dir_env else None
    judge_cassette_dir_env = os.environ.get("MIGRATION_EVAL_FAKE_JUDGE_CASSETTE_DIR")
    judge_cassette_dir = Path(judge_cassette_dir_env) if judge_cassette_dir_env else None

    repo_dirs = sorted(p for p in repos_dir.iterdir() if p.is_dir())
    if args.limit is not None:
        repo_dirs = repo_dirs[: args.limit]

    if args.dry_run:
        print(f"would process {len(repo_dirs)} repos with stage={args.stage}", file=sys.stderr)
        return 0

    from migration_evals.funnel import run_funnel

    written = 0
    for repo_dir in repo_dirs:
        meta = _load_repo_meta(repo_dir)
        recipe = _build_recipe_from_meta(meta)
        adapters = {
            "sandbox": _CassetteSandboxAdapter(repo_dir.name, sandbox_cassette_dir),
            "anthropic": _CassetteAnthropicAdapter(repo_dir.name, judge_cassette_dir),
            "enable_daikon": False,
        }
        funnel_result = run_funnel(
            repo_dir,
            recipe,
            adapters,
            is_synthetic=bool(meta.get("is_synthetic", False)),
            stages=stages,
        )
        payload = _result_payload(
            repo_dir=repo_dir,
            meta=meta,
            funnel_result=funnel_result,
            stage=args.stage,
        )

        target_dir = out_dir / repo_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "result.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
        written += 1

    print(f"wrote {written} result.json files to {out_dir}", file=sys.stderr)
    return 0


def _handle_report(args: argparse.Namespace) -> int:
    """Aggregate a run directory into a funnel markdown report."""
    from datetime import date as _date

    from migration_evals.report import generate_report

    if not args.run_dir or not args.out:
        print("error: report requires --run and --out", file=sys.stderr)
        return 2

    cutoff: Any | None = None
    if args.cutoff:
        try:
            cutoff = _date.fromisoformat(args.cutoff)
        except ValueError:
            print(
                f"error: --cutoff must be YYYY-MM-DD, got {args.cutoff!r}",
                file=sys.stderr,
            )
            return 2

    gold_path = Path(args.gold_path) if args.gold_path else None
    return generate_report(
        Path(args.run_dir),
        Path(args.out),
        model_cutoff_date=cutoff,
        gold_path=gold_path,
    )


def _handle_harness(args: argparse.Namespace) -> int:
    """Thin wrapper over :func:`migration_evals.harness.synth.synthesize_recipe`.

    This is deliberately minimal - the canonical harness synthesis code path
    lives in ``scripts/migration_eval/harness/synth.py`` and is exercised by
    the dedicated harness tests. The CLI surface exists so operators can
    inspect what a recipe looks like for a given repo without writing a
    standalone script.
    """
    if args.action not in ("synth", "validate"):
        print(
            "error: harness requires an action (synth|validate) and --repo",
            file=sys.stderr,
        )
        return 2
    if not args.repo:
        print("error: harness requires --repo", file=sys.stderr)
        return 2

    repo_path = Path(args.repo)
    if not repo_path.is_dir():
        print(f"error: --repo directory not found: {repo_path}", file=sys.stderr)
        return 2

    from migration_evals.harness.synth import synthesize_recipe

    cassette_env = os.environ.get("MIGRATION_EVAL_FAKE_HARNESS_CASSETTE_DIR")
    cassette_dir = Path(cassette_env) if cassette_env else None
    adapter = _CassetteAnthropicAdapter(repo_path.name, cassette_dir)

    if args.action == "validate":
        # Structural validation: try to build a Recipe from meta.json.
        meta = _load_repo_meta(repo_path)
        try:
            _build_recipe_from_meta(meta)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"harness validate: invalid meta.json: {exc}", file=sys.stderr)
            return 1
        print(f"harness validate: OK for {repo_path}", file=sys.stderr)
        return 0

    try:
        recipe = synthesize_recipe(repo_path, adapter)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"harness synth: synthesis failed: {exc}", file=sys.stderr)
        return 1
    print(recipe.to_json())
    return 0


def _handle_probe(args: argparse.Namespace) -> int:
    """Falsification probe handler.

    For ``--ecosystem python23`` (the M9 mode) delegates to
    :func:`migration_evals.python23_probe.run` which writes a
    ``findings.json`` enumerating M2/M3/M5 schema inadequacies. The probe
    succeeds even when it finds revision-required cases - surfacing the
    gap is the point.

    For any other ecosystem identifier, falls back to the legacy stub
    envelope so downstream wiring against the CLI surface stays stable.
    """
    if args.ecosystem == "python23":
        if not args.out:
            print(
                "error: --out is required for the python23 probe (output directory)",
                file=sys.stderr,
            )
            return 2
        from migration_evals.python23_probe import run as run_probe

        out_dir = Path(args.out)
        fixture_root = Path(args.fixture_repo_root) if args.fixture_repo_root else None
        findings = run_probe(
            count=args.count,
            out_dir=out_dir,
            fixture_repo_root=fixture_root,
            seed=args.seed,
        )
        flag = findings.get("schema_revision_required", False)
        print(
            f"probe: wrote {out_dir / 'findings.json'} " f"(schema_revision_required={flag})",
            file=sys.stderr,
        )
        if flag:
            print(
                "WARNING: ≥2 of {harness, synthetic, ledger} report mismatches. "
                "Schema interfaces MUST be revised before the first external "
                "Java number ships. See docs/migration_eval/python23_probe.md.",
                file=sys.stderr,
            )
        return 0

    payload = {
        "status": "not-implemented",
        "ecosystem": args.ecosystem,
        "note": (
            "probe harness for this ecosystem is not yet implemented; only "
            "python23 is wired today (PRD M9)."
        ),
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered)
        print(f"probe: wrote {out_path}", file=sys.stderr)
    else:
        print(rendered, end="")
    return 0


def _handle_regression(args: argparse.Namespace) -> int:
    """Regression-diff handler; delegates to migration_evals.ledger."""
    from migration_evals.ledger import run_regression

    if not args.from_ref or not args.to_ref or not args.out:
        print("error: regression requires --from, --to, and --out", file=sys.stderr)
        return 2

    return run_regression(
        Path(args.from_ref),
        Path(args.to_ref),
        Path(args.out),
    )


def _handle_iterator_report(args: argparse.Namespace) -> int:
    """iterator-report handler; delegates to migration_evals.iterator_report."""
    from migration_evals.iterator_report import generate_report

    return generate_report(Path(args.run_dir), Path(args.out))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "regression":
        return _handle_regression(args)
    if args.command == "run":
        return _handle_run(args)
    if args.command == "report":
        return _handle_report(args)
    if args.command == "iterator-report":
        return _handle_iterator_report(args)
    if args.command == "harness":
        return _handle_harness(args)
    if args.command == "probe":
        return _handle_probe(args)
    return _stub(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
