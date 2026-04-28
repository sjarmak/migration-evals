#!/usr/bin/env python3
"""Per-recipe oracle calibration driver (m1w + x8w).

Runs every fixture under
``tests/fixtures/calibration/<migration_id>/{known_good,known_bad}/``
through the tiered-oracle funnel and writes ``calibration.json`` with
per-tier FPR / FNR. The publication gate consumes the result.

Layout the driver expects (one directory per fixture):

    tests/fixtures/calibration/<migration_id>/known_good/<fixture_id>/
        label.json
        repo/                 <- staged repo the funnel runs against
            patch.diff        <- (optional) patch artifact for tier 0
            ...

Tier-0 (``diff_valid``) is local-only and runs in every invocation.
Higher tiers (``compile_only``, ``tests``, etc.) need a sandbox
adapter; pass ``--stages diff,compile`` (or wider) together with a
``--recipe`` pointing at the migration's recipe YAML so the driver
knows which build/test commands to run inside the sandbox. The driver
constructs a Docker-backed sandbox by default; ``--sandbox-factory``
exists for the unit tests that need to inject a stub.

Usage
-----
    # Tier-0 only (offline, no Docker).
    python scripts/calibrate.py \\
        --migration go_import_rewrite \\
        --fixtures tests/fixtures/calibration/go_import_rewrite \\
        --output configs/recipes/go_import_rewrite.calibration.json

    # Tier-0 + tier-1 + tier-2 (requires Docker on PATH and
    # network=none egress fixtures, see the calibration recipe).
    python scripts/calibrate.py \\
        --migration go_import_rewrite \\
        --fixtures tests/fixtures/calibration/go_import_rewrite \\
        --output configs/recipes/go_import_rewrite.calibration.json \\
        --recipe configs/recipes/go_import_rewrite.calibration.recipe.yaml \\
        --stages diff,compile,tests

Exit codes
----------
0   Calibration ran end-to-end and the JSON was written.
1   Wrong CLI usage / missing fixtures / no label files found.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from migration_evals.calibration import (  # noqa: E402
    CalibrationReport,
    FixtureLabel,
    FixtureObservation,
    compute_calibration,
    observations_from_funnel_dicts,
)
from migration_evals.funnel import STAGE_ALIASES, run_funnel  # noqa: E402
from migration_evals.harness.recipe import Recipe  # noqa: E402
from migration_evals.oracles import (  # noqa: E402
    tier1_compile,
    tier2_tests,
)

# The funnel's tier names are stable; we keep the canonical run order here
# so the calibration report's per_tier list reflects funnel order.
DEFAULT_TIER_ORDER: tuple[str, ...] = (
    "diff_valid",
    "compile_only",
    "tests",
    "ast_conformance",
    "judge",
    "daikon",
)

# Tiers that need a SandboxAdapter to produce any verdict at all.
_SANDBOX_TIERS: frozenset[str] = frozenset({tier1_compile.TIER_NAME, tier2_tests.TIER_NAME})


# ---------------------------------------------------------------------------
# Recipe loading
# ---------------------------------------------------------------------------


# Calibration runs are reproducible by construction, so the
# harness_provenance carried with calibration recipes is fixed and
# deterministic. The production runner overrides this when synthesising
# real per-trial recipes.
_CALIBRATION_PROVENANCE: dict[str, str] = {
    "model": "calibration",
    "prompt_version": "calibration-v1",
    "timestamp": "1970-01-01T00:00:00Z",
}


def _placeholder_recipe() -> Recipe:
    """Tier-0 calibration only needs a Recipe shell (build/test_cmd unused)."""
    return Recipe(
        dockerfile="FROM scratch",
        build_cmd="true",
        test_cmd="true",
        harness_provenance=dict(_CALIBRATION_PROVENANCE),
    )


def _load_recipe_from_yaml(path: Path) -> Recipe:
    """Build a :class:`Recipe` from a recipe YAML at ``path``.

    Mirrors the shape consumed by ``configs/recipes/*.yaml``: the file's
    top-level ``recipe`` key carries ``dockerfile`` / ``build_cmd`` /
    ``test_cmd``. ``harness_provenance`` falls back to
    :data:`_CALIBRATION_PROVENANCE` when the YAML omits it.
    """
    import yaml  # local import to keep top-level import light

    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, Mapping):
        raise ValueError(f"recipe YAML at {path} did not parse to a mapping")
    block = raw.get("recipe")
    if not isinstance(block, Mapping):
        raise ValueError(f"recipe YAML at {path} is missing a top-level 'recipe' block")
    try:
        return Recipe(
            dockerfile=str(block["dockerfile"]),
            build_cmd=str(block["build_cmd"]),
            test_cmd=str(block["test_cmd"]),
            harness_provenance=dict(block.get("harness_provenance", _CALIBRATION_PROVENANCE)),
        )
    except KeyError as exc:
        raise ValueError(
            f"recipe YAML at {path} missing required key {exc.args[0]!r} " "under 'recipe'"
        ) from exc


# ---------------------------------------------------------------------------
# Sandbox adapter wiring
# ---------------------------------------------------------------------------


class _ImageOverridingSandbox:
    """Adapter wrapper that pins ``image`` to a calibration-controlled value.

    The funnel's tier-1/tier-2 oracles call ``create_sandbox(image=...)``
    with a recipe-derived default (``build-sandbox:latest``). For
    calibration we want to use a known-pulled base image (e.g.
    ``golang:1.22``) without rebuilding the recipe's Dockerfile and
    without polluting the host's image namespace by retagging. Wrapping
    the underlying adapter and rewriting the ``image`` argument is the
    smallest change that keeps the funnel signature untouched.
    """

    def __init__(self, inner: Any, image: str) -> None:
        self._inner = inner
        self._image = image

    def create_sandbox(
        self,
        *,
        image: str,  # noqa: ARG002 - intentionally ignored
        env: Mapping[str, str] | None = None,
        cassette: Any | None = None,
    ) -> str:
        return self._inner.create_sandbox(image=self._image, env=env, cassette=cassette)

    def exec(
        self,
        sandbox_id: str,
        *,
        command: str,
        timeout_s: int = 600,
        cassette: Any | None = None,
    ) -> Mapping[str, Any]:
        return self._inner.exec(
            sandbox_id,
            command=command,
            timeout_s=timeout_s,
            cassette=cassette,
        )

    def destroy_sandbox(self, sandbox_id: str) -> None:
        self._inner.destroy_sandbox(sandbox_id)


def _default_sandbox_factory(repo_path: Path, *, image: str) -> Any:
    """Build the production sandbox adapter (Docker) for ``repo_path``.

    Imported lazily so unit tests that mock the factory never trigger
    Docker import-time work. Wrapping in :class:`_ImageOverridingSandbox`
    pins the image to the calibration-controlled value.
    """
    docker_module = importlib.import_module("migration_evals.adapters_docker")
    inner = docker_module.DockerSandboxAdapter(repo_path)
    return _ImageOverridingSandbox(inner, image=image)


# Security: ``--sandbox-factory`` resolves an arbitrary ``module:attr``
# spec via ``importlib.import_module`` + ``getattr``. To keep the CLI
# surface safe even when calibrate is wired into a larger pipeline that
# may pass partly-user-controlled args, we restrict the importable
# module to a small allowlist of repo-internal namespaces. The seam is a
# development/test affordance only — production runs use the default
# Docker factory and never set ``--sandbox-factory``.
_SANDBOX_FACTORY_ALLOWED_PREFIXES: tuple[str, ...] = (
    "tests.",
    "migration_evals.",
)


def _resolve_sandbox_factory(
    spec: str | None,
) -> Callable[..., Any]:
    """Translate ``--sandbox-factory`` into a callable.

    ``None`` returns the production Docker factory. A ``module:attr``
    string is resolved via :func:`importlib.import_module` and getattr;
    the attribute must be a callable with the same signature as
    :func:`_default_sandbox_factory`. This is the unit-test seam.

    Security: the module name must start with one of
    :data:`_SANDBOX_FACTORY_ALLOWED_PREFIXES`. The check happens
    *before* the import so that even attribute-side effects in
    arbitrary modules cannot run via this CLI surface.
    """
    if spec is None:
        return _default_sandbox_factory
    if ":" not in spec:
        raise ValueError(f"--sandbox-factory must be 'module:attr' (got {spec!r})")
    module_name, attr = spec.split(":", 1)
    if not any(module_name.startswith(prefix) for prefix in _SANDBOX_FACTORY_ALLOWED_PREFIXES):
        raise ValueError(
            f"--sandbox-factory {spec!r}: module {module_name!r} is not "
            f"in the allowlist {_SANDBOX_FACTORY_ALLOWED_PREFIXES!r}. "
            "This flag is a development/test seam; production runs must "
            "omit it. Never wire it to user-controlled input."
        )
    module = importlib.import_module(module_name)
    factory = getattr(module, attr)
    if not callable(factory):
        raise ValueError(f"--sandbox-factory {spec!r} resolved to non-callable")
    return factory


def _resolve_stages(raw: str | None) -> tuple[str, ...] | None:
    """Translate ``--stages`` (CLI alias list) into funnel tier names.

    ``None`` keeps the funnel default (run every enabled tier). The CLI
    accepts the same stage aliases as ``scripts/run_eval.py`` (``diff``,
    ``compile``, ``tests``, ``judge``, ``daikon``, ``all``).
    """
    if not raw:
        return None
    requested: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if token not in STAGE_ALIASES:
            raise ValueError(
                f"unknown --stages token {token!r}; " f"valid values: {sorted(STAGE_ALIASES)}"
            )
        requested.extend(STAGE_ALIASES[token])
    return tuple(dict.fromkeys(requested))


def _stages_need_sandbox(stages: tuple[str, ...] | None) -> bool:
    """True iff at least one requested stage needs a sandbox adapter.

    ``None`` (run-all) always needs a sandbox because tier-1 / tier-2
    are in the default funnel.
    """
    if stages is None:
        return True
    return any(s in _SANDBOX_TIERS for s in stages)


# ---------------------------------------------------------------------------
# Fixture iteration / per-fixture funnel
# ---------------------------------------------------------------------------


def _iter_fixtures(root: Path) -> Iterable[tuple[Path, FixtureLabel]]:
    """Yield ``(fixture_dir, FixtureLabel)`` for every committed fixture.

    Recurses into ``known_good/`` and ``known_bad/`` and treats every
    direct subdirectory as a fixture if it has a ``label.json``.
    """
    for sub in ("known_good", "known_bad"):
        bucket = root / sub
        if not bucket.is_dir():
            continue
        for fixture in sorted(bucket.iterdir()):
            label_path = fixture / "label.json"
            if not label_path.is_file():
                continue
            yield fixture, FixtureLabel.from_path(label_path)


def _effective_stages(
    label: FixtureLabel,
    stages: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    """Narrow the global ``stages`` set to those the fixture is valid for.

    Returns ``None`` (run-all) when neither the CLI nor the label
    constrain the funnel. When the label declares ``applicable_tiers``
    we intersect them with the requested ``stages``; the funnel honours
    a ``stages`` argument as an inclusion list, so a fixture that only
    applies to ``diff_valid`` will produce a single-tier verdict even
    when the run is configured for ``diff,compile,tests``.
    """
    if label.applicable_tiers is None:
        return stages
    if stages is None:
        return tuple(label.applicable_tiers)
    intersection = tuple(s for s in stages if s in label.applicable_tiers)
    return intersection


def _run_one(
    fixture_dir: Path,
    label: FixtureLabel,
    *,
    stages: tuple[str, ...] | None,
    recipe: Recipe,
    sandbox_factory: Callable[..., Any] | None,
    sandbox_image: str,
) -> FixtureObservation:
    """Run the funnel for one fixture and return its observation."""
    repo = fixture_dir / "repo"
    if not repo.is_dir():
        raise FileNotFoundError(f"calibration fixture {fixture_dir} has no repo/ subdir")
    effective = _effective_stages(label, stages)
    adapters: dict[str, Any] = {}
    if _stages_need_sandbox(effective) and sandbox_factory is not None:
        adapters["sandbox"] = sandbox_factory(repo, image=sandbox_image)
    funnel_result = run_funnel(
        repo,
        recipe,
        adapters=adapters,
        is_synthetic=False,
        stages=effective,
    )
    return observations_from_funnel_dicts(label, funnel_result.to_dict())


def calibrate(
    *,
    migration_id: str,
    fixtures_root: Path,
    stages: tuple[str, ...] | None,
    notes: str = "",
    recipe: Recipe | None = None,
    sandbox_factory: Callable[..., Any] | None = None,
    sandbox_image: str = "golang:1.22",
) -> CalibrationReport:
    """Drive the funnel over every fixture and return a CalibrationReport.

    Parameters
    ----------
    recipe
        The build/test recipe to pass to the funnel. ``None`` falls back
        to a tier-0-only placeholder; callers requesting compile/tests
        stages must provide a real recipe.
    sandbox_factory
        Per-fixture factory ``(repo_path, *, image) -> SandboxAdapter``.
        ``None`` means tier-0-only (no sandbox is constructed). When the
        requested ``stages`` need a sandbox tier, callers must pass a
        factory.
    """
    if recipe is None:
        recipe = _placeholder_recipe()
    if _stages_need_sandbox(stages) and sandbox_factory is None:
        raise ValueError(
            "calibrate: requested stages include a sandbox tier "
            "(compile_only/tests) but no sandbox_factory was supplied. "
            "Pass --recipe and use --stages diff (or fewer) to stay "
            "tier-0 only, or supply a sandbox_factory."
        )

    observations: list[FixtureObservation] = []
    fixture_count = 0
    for fixture_dir, label in _iter_fixtures(fixtures_root):
        observations.append(
            _run_one(
                fixture_dir,
                label,
                stages=stages,
                recipe=recipe,
                sandbox_factory=sandbox_factory,
                sandbox_image=sandbox_image,
            )
        )
        fixture_count += 1
    if fixture_count == 0:
        raise FileNotFoundError(f"no calibration fixtures found under {fixtures_root}")
    return compute_calibration(
        observations,
        migration_id=migration_id,
        tier_order=DEFAULT_TIER_ORDER,
        notes=notes,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the calibration corpus through the funnel and emit "
            "calibration.json with per-tier FPR / FNR."
        )
    )
    parser.add_argument(
        "--migration",
        required=True,
        help="migration_id (e.g. go_import_rewrite)",
    )
    parser.add_argument(
        "--fixtures",
        required=True,
        type=Path,
        help=(
            "Path to the per-recipe calibration root, containing "
            "known_good/ and known_bad/ subdirectories"
        ),
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Where to write calibration.json",
    )
    parser.add_argument(
        "--stages",
        default="diff",
        help=(
            "Comma-separated stage aliases (diff,compile,tests,judge,"
            "daikon,all). Default: diff (tier-0 only; offline)."
        ),
    )
    parser.add_argument(
        "--recipe",
        type=Path,
        default=None,
        help=(
            "Path to a recipe YAML (e.g. configs/recipes/<id>.yaml or "
            "the per-id calibration recipe). Required when --stages "
            "includes a sandbox tier (compile, tests)."
        ),
    )
    parser.add_argument(
        "--sandbox-image",
        default="golang:1.22",
        help=(
            "Container image used for sandbox tiers (default golang:1.22). "
            "Pinned per migration so calibration runs reproducibly."
        ),
    )
    parser.add_argument(
        "--sandbox-factory",
        default=None,
        help=(
            "Optional 'module:attr' override for the sandbox factory. "
            "DEVELOPMENT / TEST-ONLY seam: performs an arbitrary import. "
            "Module name must start with one of "
            f"{_SANDBOX_FACTORY_ALLOWED_PREFIXES}. NEVER wire this flag "
            "to untrusted or user-controlled input. Production runs use "
            "the default Docker-backed factory and must omit this flag."
        ),
    )
    parser.add_argument(
        "--notes",
        default="",
        help=(
            "Optional free-text notes embedded in calibration.json "
            "(e.g. 'tier-0 only; tier-1 calibration deferred to bd-XXX')"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        stages = _resolve_stages(args.stages)
    except ValueError as exc:
        print(f"calibrate: {exc}", file=sys.stderr)
        return 1

    fixtures = args.fixtures.resolve()
    if not fixtures.is_dir():
        print(
            f"calibrate: fixtures dir does not exist: {fixtures}",
            file=sys.stderr,
        )
        return 1

    recipe: Recipe | None = None
    if args.recipe is not None:
        try:
            recipe = _load_recipe_from_yaml(args.recipe)
        except (OSError, ValueError, ImportError) as exc:
            print(f"calibrate: {exc}", file=sys.stderr)
            return 1

    sandbox_factory: Callable[..., Any] | None = None
    if _stages_need_sandbox(stages):
        if recipe is None:
            print(
                "calibrate: --stages includes a sandbox tier "
                "(compile/tests); --recipe is required so the funnel "
                "knows which build/test commands to run.",
                file=sys.stderr,
            )
            return 1
        try:
            sandbox_factory = _resolve_sandbox_factory(args.sandbox_factory)
        except (ImportError, AttributeError, ValueError) as exc:
            print(f"calibrate: {exc}", file=sys.stderr)
            return 1

    try:
        report = calibrate(
            migration_id=args.migration,
            fixtures_root=fixtures,
            stages=stages,
            notes=args.notes,
            recipe=recipe,
            sandbox_factory=sandbox_factory,
            sandbox_image=args.sandbox_image,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"calibrate: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report.to_json() + "\n")
    print(
        f"calibrate: wrote {args.output} "
        f"(known_good={report.n_known_good}, "
        f"known_bad={report.n_known_bad})"
    )
    for tier in report.per_tier:
        if tier.n_known_good_observed == 0 and tier.n_known_bad_targeted_observed == 0:
            continue
        fpr = "n/a" if tier.fpr is None else f"{tier.fpr:.3f}"
        fnr = "n/a" if tier.fnr is None else f"{tier.fnr:.3f}"
        print(
            f"  tier={tier.tier:<16} fpr={fpr:<5} fnr={fnr:<5} "
            f"(tp={tier.tp} fp={tier.fp} tn={tier.tn} fn={tier.fn})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
