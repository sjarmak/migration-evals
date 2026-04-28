"""End-to-end tests for the calibration driver and the gate's
``--require-calibration`` mode (m1w).

These tests execute ``scripts/calibrate.py`` against the committed
``go_import_rewrite`` corpus and the ``--require-calibration`` flag of the
publication gate against synthesised manifests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from migration_evals.calibration import CalibrationReport  # noqa: E402

REPO_ROOT = _REPO_ROOT
CALIBRATE_SCRIPT = REPO_ROOT / "scripts" / "calibrate.py"
GATE_MODULE = "migration_evals.publication_gate"
CALIBRATION_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "calibration" / "go_import_rewrite"
HYPOTHESES_PATH = REPO_ROOT / "docs" / "hypotheses_and_thresholds.md"
RUN_STAMPED = REPO_ROOT / "tests" / "fixtures" / "run_stamped"


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}


# ---------------------------------------------------------------------------
# scripts/calibrate.py
# ---------------------------------------------------------------------------


def test_calibrate_emits_clean_tier_zero_calibration(tmp_path: Path) -> None:
    out = tmp_path / "calibration.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(CALIBRATE_SCRIPT),
            "--migration",
            "go_import_rewrite",
            "--fixtures",
            str(CALIBRATION_FIXTURES),
            "--output",
            str(out),
            "--stages",
            "diff",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert out.is_file()
    report = CalibrationReport.from_path(out)
    assert report.migration_id == "go_import_rewrite"
    # 10 tier-0 known-good + 2 tier-1/tier-2 known-good. The latter
    # declare applicable_tiers=["compile_only", "tests"] so they do not
    # contribute to the tier-0 corpus.
    assert report.n_known_good == 12
    # 10 tier-0 known-bad + 2 compile_only + 2 tests known-bad fixtures.
    # The compile_only/tests fixtures also declare applicable_tiers
    # excluding diff_valid, so they do not run through tier-0.
    assert report.n_known_bad == 14
    diff = report.tier("diff_valid")
    # Corpus is hand-vetted for tier-0; FPR must be zero (no clean diff
    # is wrongly rejected). FNR is computed only against known-bad
    # fixtures whose expected_reject_tier == 'diff_valid' (the original
    # 10 bad_*_* fixtures), so it must also be zero.
    assert diff.fpr == 0.0
    assert diff.fnr == 0.0
    # Only the 10 tier-0 fixtures opt into diff_valid via applicable_tiers;
    # tier-1/tier-2 fixtures restrict themselves to ['compile_only','tests'].
    assert diff.tn == 10 and diff.tp == 10
    # Tiers above tier 0 weren't run; their rates are unobserved.
    assert report.tier("compile_only").fpr is None
    assert report.tier("compile_only").fnr is None


def test_calibrate_fails_on_missing_fixtures_dir(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(CALIBRATE_SCRIPT),
            "--migration",
            "go_import_rewrite",
            "--fixtures",
            str(tmp_path / "does_not_exist"),
            "--output",
            str(tmp_path / "out.json"),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_env(),
    )
    assert proc.returncode == 1
    assert "does not exist" in proc.stderr


def test_calibrate_fails_on_unknown_stage(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(CALIBRATE_SCRIPT),
            "--migration",
            "x",
            "--fixtures",
            str(CALIBRATION_FIXTURES),
            "--output",
            str(tmp_path / "out.json"),
            "--stages",
            "diff,brunch",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_env(),
    )
    assert proc.returncode == 1
    assert "unknown --stages" in proc.stderr


# ---------------------------------------------------------------------------
# scripts/calibrate.py — sandbox wiring (x8w)
# ---------------------------------------------------------------------------


CALIBRATION_RECIPE = REPO_ROOT / "configs" / "recipes" / "go_import_rewrite.calibration.recipe.yaml"
STUB_FACTORY_SPEC = "tests._calibrate_stub_sandbox:stub_factory"


def _stub_env(tmp_path: Path, *, script: dict | None = None) -> dict[str, str]:
    """Build an env dict that points the stub adapter at scratch files."""
    log_path = tmp_path / "stub_log.jsonl"
    base = _env()
    # ``tests._calibrate_stub_sandbox`` lives under the repo root; prepend
    # so importlib resolves it from there inside the calibrate subprocess.
    base_pythonpath = base.get("PYTHONPATH", "")
    pythonpath_entries = [str(REPO_ROOT)]
    if base_pythonpath:
        pythonpath_entries.append(base_pythonpath)
    env = {
        **base,
        "CALIBRATE_STUB_LOG": str(log_path),
        "PYTHONPATH": os.pathsep.join(pythonpath_entries),
    }
    if script is not None:
        script_path = tmp_path / "stub_script.json"
        script_path.write_text(json.dumps(script))
        env["CALIBRATE_STUB_SCRIPT"] = str(script_path)
    return env


def _read_stub_log(tmp_path: Path) -> list[dict]:
    log_path = tmp_path / "stub_log.jsonl"
    if not log_path.is_file():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def test_calibrate_requires_recipe_for_sandbox_stages(tmp_path: Path) -> None:
    """Asking for compile/tests without --recipe is a CLI error, not a
    silent fall-back to tier-0."""
    proc = subprocess.run(
        [
            sys.executable,
            str(CALIBRATE_SCRIPT),
            "--migration",
            "go_import_rewrite",
            "--fixtures",
            str(CALIBRATION_FIXTURES),
            "--output",
            str(tmp_path / "out.json"),
            "--stages",
            "diff,compile",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_env(),
    )
    assert proc.returncode == 1
    assert "--recipe is required" in proc.stderr


def test_calibrate_wires_sandbox_factory_for_compile_stage(
    tmp_path: Path,
) -> None:
    """When --stages includes compile, calibrate must construct a
    SandboxAdapter via the resolved factory and pass it through to the
    funnel for *every* fixture (not just the tier-1-targeted ones)."""
    out = tmp_path / "calibration.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(CALIBRATE_SCRIPT),
            "--migration",
            "go_import_rewrite",
            "--fixtures",
            str(CALIBRATION_FIXTURES),
            "--output",
            str(out),
            "--stages",
            "diff,compile",
            "--recipe",
            str(CALIBRATION_RECIPE),
            "--sandbox-factory",
            STUB_FACTORY_SPEC,
            "--sandbox-image",
            "stub-image:latest",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_stub_env(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    log = _read_stub_log(tmp_path)
    # The legacy good_001..good_010 / bad_001..bad_010 fixtures declare
    # applicable_tiers=["diff_valid"] and so opt out of tier-1; only the
    # x8w-era fixtures (good_011, good_012, bad_011..bad_014) reach
    # compile_only. 6 fixtures × 1 create_sandbox per tier-1 invocation.
    creates = [e for e in log if e["event"] == "create_sandbox"]
    assert len(creates) == 6, [e["fixture_id"] for e in creates]
    fixture_ids = {e["fixture_id"] for e in creates}
    assert fixture_ids == {
        "good_011_compiles_and_tests",
        "good_012_subpackage_compiles",
        "bad_011_compile_unresolved_local_import",
        "bad_012_compile_undefined_symbol",
        "bad_013_test_failing_assertion",
        "bad_014_test_panic",
    }
    # The wrapper must override the funnel's default image with the
    # CLI-supplied --sandbox-image. The inner factory_image carries the
    # value the test passed in.
    factory_images = {e["factory_image"] for e in creates}
    assert factory_images == {"stub-image:latest"}


def test_calibrate_sandbox_script_drives_compile_only_verdicts(
    tmp_path: Path,
) -> None:
    """The compile_only tier's verdict must come from the sandbox exec
    envelope, not from any heuristic in calibrate.py. We script
    bad_011/bad_012 to exit non-zero on `go build` and confirm the
    resulting calibration shows tp=2 fp=0 for compile_only."""
    script = {
        "bad_011_compile_unresolved_local_import": {
            "go build": {"exit_code": 1, "stderr": "build failed"},
        },
        "bad_012_compile_undefined_symbol": {
            "go build": {"exit_code": 1, "stderr": "build failed"},
        },
    }
    out = tmp_path / "calibration.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(CALIBRATE_SCRIPT),
            "--migration",
            "go_import_rewrite",
            "--fixtures",
            str(CALIBRATION_FIXTURES),
            "--output",
            str(out),
            "--stages",
            "diff,compile",
            "--recipe",
            str(CALIBRATION_RECIPE),
            "--sandbox-factory",
            STUB_FACTORY_SPEC,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_stub_env(tmp_path, script=script),
    )
    assert proc.returncode == 0, proc.stderr
    report = CalibrationReport.from_path(out)
    compile_tier = report.tier("compile_only")
    # Two bad_011/bad_012 fixtures targeted at compile_only fail the
    # build → tp=2.
    assert compile_tier.tp == 2
    assert compile_tier.fn == 0
    # bad_013/bad_014 are targeted at tests, not compile, so their
    # compile_only pass does not contribute to compile_only's
    # known-bad-targeted denominator.
    assert compile_tier.n_known_bad_targeted_observed == 2
    # Only good_011/good_012 declare applicable_tiers covering
    # compile_only (the legacy good_001..good_010 fixtures opt out),
    # so tn=2, fp=0.
    assert compile_tier.tn == 2
    assert compile_tier.fp == 0
    assert compile_tier.fpr == 0.0
    assert compile_tier.fnr == 0.0


def test_calibrate_sandbox_script_drives_tests_tier_verdicts(
    tmp_path: Path,
) -> None:
    """The tests tier's verdict must come from the sandbox exec envelope
    too. Script bad_013/bad_014 to fail go test and confirm tp=2."""
    script = {
        "bad_013_test_failing_assertion": {
            "go test": {"exit_code": 1, "stderr": "test FAIL"},
        },
        "bad_014_test_panic": {
            "go test": {"exit_code": 2, "stderr": "panic"},
        },
    }
    out = tmp_path / "calibration.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(CALIBRATE_SCRIPT),
            "--migration",
            "go_import_rewrite",
            "--fixtures",
            str(CALIBRATION_FIXTURES),
            "--output",
            str(out),
            "--stages",
            "diff,compile,tests",
            "--recipe",
            str(CALIBRATION_RECIPE),
            "--sandbox-factory",
            STUB_FACTORY_SPEC,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_stub_env(tmp_path, script=script),
    )
    assert proc.returncode == 0, proc.stderr
    report = CalibrationReport.from_path(out)
    tests_tier = report.tier("tests")
    assert tests_tier.tp == 2
    assert tests_tier.fn == 0
    assert tests_tier.n_known_bad_targeted_observed == 2
    # Only good_011/good_012 declare applicable_tiers covering tests
    # (legacy fixtures opt out), so tn=2, fp=0.
    assert tests_tier.tn == 2
    assert tests_tier.fp == 0
    assert tests_tier.fpr == 0.0
    assert tests_tier.fnr == 0.0


def test_calibrate_does_not_construct_sandbox_for_tier_zero_only(
    tmp_path: Path,
) -> None:
    """The default (--stages diff) must remain Docker-free: no factory
    resolution, no sandbox construction, no log entries."""
    out = tmp_path / "calibration.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(CALIBRATE_SCRIPT),
            "--migration",
            "go_import_rewrite",
            "--fixtures",
            str(CALIBRATION_FIXTURES),
            "--output",
            str(out),
            "--stages",
            "diff",
            # Deliberately point at a non-existent factory: tier-0 path
            # must not touch it.
            "--sandbox-factory",
            "tests._calibrate_stub_sandbox:does_not_exist",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_stub_env(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    log = _read_stub_log(tmp_path)
    assert log == []  # stub adapter never instantiated


# ---------------------------------------------------------------------------
# scripts/calibrate.py — --sandbox-factory module-prefix allowlist (o7z)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        "os:system",
        "subprocess:run",
        "shutil:rmtree",
        "__main__:anything",
        "builtins:eval",
        # A repo-internal-looking prefix that is NOT in the allowlist.
        "scripts.calibrate:_default_sandbox_factory",
    ],
)
def test_calibrate_rejects_sandbox_factory_outside_allowlist(
    tmp_path: Path,
    spec: str,
) -> None:
    """``--sandbox-factory`` must reject any module whose name does not
    start with one of the allowlisted prefixes (``tests.``,
    ``migration_evals.``). The CLI surface area means a pipeline that
    pipes user-controlled args could otherwise import arbitrary modules.
    """
    proc = subprocess.run(
        [
            sys.executable,
            str(CALIBRATE_SCRIPT),
            "--migration",
            "go_import_rewrite",
            "--fixtures",
            str(CALIBRATION_FIXTURES),
            "--output",
            str(tmp_path / "out.json"),
            "--stages",
            "diff,compile",
            "--recipe",
            str(CALIBRATION_RECIPE),
            "--sandbox-factory",
            spec,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_env(),
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    # Error message must be specific so operators understand why the
    # spec was rejected (not a generic ImportError or KeyError).
    assert "--sandbox-factory" in proc.stderr
    assert "allowlist" in proc.stderr or "allowed" in proc.stderr


def test_calibrate_help_text_warns_about_sandbox_factory_security(
    tmp_path: Path,
) -> None:
    """The --help output must explicitly mark --sandbox-factory as a
    development/test-only seam and warn against wiring user-controlled
    input. This is the documentation half of the o7z hardening — even if
    a future change loosens the allowlist, the help text keeps the
    contract visible at the CLI surface.
    """
    proc = subprocess.run(
        [sys.executable, str(CALIBRATE_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_env(),
    )
    assert proc.returncode == 0, proc.stderr
    help_text = proc.stdout
    # Warning is case-insensitive to keep the assertion robust against
    # minor wording changes; the substring "test only" or
    # "test-only" plus a mention of arbitrary import is the contract.
    lower = help_text.lower()
    assert "test only" in lower or "test-only" in lower
    assert "untrusted" in lower or "user-controlled" in lower
    assert "arbitrary import" in lower


def _import_calibrate_module():
    """Load ``scripts/calibrate.py`` as an importable module.

    ``scripts/calibrate.py`` lives outside the ``src/`` package layout, so
    ``importlib.import_module`` cannot reach it without permanently
    extending ``sys.path`` (and registering a top-level ``calibrate``
    entry in ``sys.modules`` that would shadow other imports). Using
    ``spec_from_file_location`` + ``exec_module`` keeps the load
    file-scoped: each call builds a fresh ``ModuleType`` from the file
    on disk and executes its top-level code into that new module — no
    process-wide ``sys.path`` mutation done by this helper, and no
    ``sys.modules`` registration leaking across tests.

    Side-effect note: the *target file's* own module-level code still
    runs on every call. In particular, the guarded ``sys.path.insert``
    block at the top of ``scripts/calibrate.py`` (which prepends the
    repo's ``src/`` directory so ``migration_evals.*`` resolves when the
    script is executed standalone) re-fires here too. The guard is
    idempotent (``if str(_SRC) not in sys.path``), so repeated calls do
    not stack duplicate entries, but callers who need pristine
    ``sys.path`` should snapshot/restore it themselves.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("calibrate_module_under_test", CALIBRATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_calibrate_resolve_sandbox_factory_unit_rejects_outside_allowlist() -> None:
    """In-process unit check that ``_resolve_sandbox_factory`` raises
    ``ValueError`` for a non-allowlisted module without performing the
    import. Subprocess tests above prove the CLI surface; this one
    proves the internal contract so refactors keep failing loudly.
    """
    calibrate_mod = _import_calibrate_module()
    with pytest.raises(ValueError) as excinfo:
        calibrate_mod._resolve_sandbox_factory("os:system")
    msg = str(excinfo.value)
    assert "allowlist" in msg or "allowed" in msg


def test_calibrate_resolve_sandbox_factory_unit_accepts_tests_prefix() -> None:
    """The committed stub factory under ``tests._calibrate_stub_sandbox``
    must remain resolvable — the allowlist must not regress the
    documented test seam."""
    calibrate_mod = _import_calibrate_module()
    factory = calibrate_mod._resolve_sandbox_factory("tests._calibrate_stub_sandbox:stub_factory")
    assert callable(factory)


def test_calibrate_resolve_sandbox_factory_rejects_module_file_outside_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth (cni): even when the prefix allowlist accepts a
    module name (``tests.`` / ``migration_evals.``), the imported
    module's ``__file__`` must resolve to a path inside ``_REPO_ROOT``.

    Threat model: an attacker-controlled directory on ``PYTHONPATH``
    containing a ``tests/`` or ``migration_evals/`` subpackage would
    otherwise be importable through this CLI seam. Verifying
    ``module.__file__`` defeats that even with a poisoned ``sys.path``.
    """
    import types

    calibrate_mod = _import_calibrate_module()

    shadow_dir = tmp_path / "shadow_pkg"
    shadow_dir.mkdir()
    shadow_file = shadow_dir / "__init__.py"
    shadow_file.write_text("def stub_factory(*a, **kw):\n    return None\n")

    fake_module = types.ModuleType("tests.shadow_pkg_fake")
    fake_module.__file__ = str(shadow_file)
    fake_module.stub_factory = lambda *a, **kw: None  # type: ignore[attr-defined]

    def _fake_import(name: str) -> types.ModuleType:
        return fake_module

    monkeypatch.setattr(calibrate_mod.importlib, "import_module", _fake_import)

    with pytest.raises(ValueError) as excinfo:
        calibrate_mod._resolve_sandbox_factory("tests.shadow_pkg_fake:stub_factory")
    msg = str(excinfo.value)
    assert "outside repo root" in msg


def test_calibrate_resolve_sandbox_factory_rejects_module_without_file_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module without a ``__file__`` attribute (built-ins, namespace
    packages) cannot be inside the repo and must be rejected. The prefix
    allowlist already excludes built-ins in practice, but the
    ``__file__`` check is the authoritative gate."""
    import types

    calibrate_mod = _import_calibrate_module()

    fake_module = types.ModuleType("tests.no_file_fake")
    # Explicitly clear __file__ — types.ModuleType may have it set to None.
    if hasattr(fake_module, "__file__"):
        delattr(fake_module, "__file__")
    fake_module.stub_factory = lambda *a, **kw: None  # type: ignore[attr-defined]

    def _fake_import(name: str) -> types.ModuleType:
        return fake_module

    monkeypatch.setattr(calibrate_mod.importlib, "import_module", _fake_import)

    with pytest.raises(ValueError) as excinfo:
        calibrate_mod._resolve_sandbox_factory("tests.no_file_fake:stub_factory")
    msg = str(excinfo.value)
    # Tight: must hit the no-__file__ branch specifically, not the
    # is-relative-to fallthrough (which would indicate the wrong code
    # path fired on a None __file__).
    assert "has no" in msg and "__file__" in msg


# ---------------------------------------------------------------------------
# Live Docker integration (opt-in, x8w)
# ---------------------------------------------------------------------------


_DOCKER_AVAILABLE = shutil.which("docker") is not None
_DOCKER_INTEGRATION = os.environ.get("MIGRATION_EVAL_DOCKER_INTEGRATION") == "1"
_CALIBRATION_RECIPE_LIVE = (
    REPO_ROOT / "configs" / "recipes" / "go_import_rewrite.calibration.recipe.yaml"
)


@pytest.mark.skipif(
    not (_DOCKER_AVAILABLE and _DOCKER_INTEGRATION),
    reason="set MIGRATION_EVAL_DOCKER_INTEGRATION=1 with Docker available",
)
def test_calibrate_end_to_end_against_docker(tmp_path: Path) -> None:
    """End-to-end calibration against the real Docker-backed sandbox.

    Asserts that the corpus + recipe + adapter wiring produce a clean
    tier-0 / tier-1 / tier-2 calibration (zero FPR, zero FNR) — the same
    numbers committed to ``configs/recipes/go_import_rewrite.calibration.json``.
    """
    out = tmp_path / "calibration.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(CALIBRATE_SCRIPT),
            "--migration",
            "go_import_rewrite",
            "--fixtures",
            str(CALIBRATION_FIXTURES),
            "--output",
            str(out),
            "--stages",
            "diff,compile,tests",
            "--recipe",
            str(_CALIBRATION_RECIPE_LIVE),
            "--sandbox-image",
            "golang:1.22",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_env(),
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    report = CalibrationReport.from_path(out)
    assert report.n_known_good == 12
    assert report.n_known_bad == 14
    for tier_name in ("diff_valid", "compile_only", "tests"):
        tier = report.tier(tier_name)
        assert tier.fpr == 0.0, f"{tier_name} fpr={tier.fpr}"
        assert tier.fnr == 0.0, f"{tier_name} fnr={tier.fnr}"


# ---------------------------------------------------------------------------
# publication_gate --require-calibration
# ---------------------------------------------------------------------------


def _stage_run_with_calibration(
    *,
    src: Path,
    dst: Path,
    calibration_payload: dict | None,
    declare_in_manifest: bool,
) -> Path:
    """Copy the run_stamped fixture into ``dst`` and (optionally) add a
    committed ``calibration.json`` plus the manifest pointer to it.

    Mirrors ``test_pre_reg._stage_run`` for the hypotheses-path absolutising
    so the gate resolves the doc against the real committed file.
    """
    shutil.copytree(src, dst)
    manifest_path = dst / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["hypotheses"] = str(HYPOTHESES_PATH)
    if calibration_payload is not None:
        cal_path = dst / "calibration.json"
        cal_path.write_text(json.dumps(calibration_payload, indent=2))
        if declare_in_manifest:
            manifest["calibration_report"] = str(cal_path)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return dst


def _run_gate(run_dir: Path, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            GATE_MODULE,
            "--check-run",
            str(run_dir),
            *extra_args,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_env(),
    )


def _passing_calibration_payload() -> dict:
    return {
        "migration_id": "go_import_rewrite",
        "schema_version": "v1",
        "n_known_good": 12,
        "n_known_bad": 14,
        "notes": "fixture",
        "per_tier": [
            {
                "tier": "diff_valid",
                "tp": 10,
                "fp": 0,
                "tn": 12,
                "fn": 0,
                "n_known_good_observed": 12,
                "n_known_bad_targeted_observed": 10,
                "fpr": 0.0,
                "fnr": 0.0,
            },
            {
                "tier": "compile_only",
                "tp": 2,
                "fp": 0,
                "tn": 2,
                "fn": 0,
                "n_known_good_observed": 2,
                "n_known_bad_targeted_observed": 2,
                "fpr": 0.0,
                "fnr": 0.0,
            },
            {
                "tier": "tests",
                "tp": 2,
                "fp": 0,
                "tn": 2,
                "fn": 0,
                "n_known_good_observed": 2,
                "n_known_bad_targeted_observed": 2,
                "fpr": 0.0,
                "fnr": 0.0,
            },
        ],
    }


def test_gate_default_mode_does_not_check_calibration(
    tmp_path: Path,
) -> None:
    """Without --require-calibration, the gate ignores calibration entirely."""
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED,
        dst=tmp_path / "run_default",
        calibration_payload=None,
        declare_in_manifest=False,
    )
    proc = _run_gate(staged)
    assert proc.returncode == 0, proc.stderr


def test_gate_require_calibration_passes_when_clean(tmp_path: Path) -> None:
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED,
        dst=tmp_path / "run_clean",
        calibration_payload=_passing_calibration_payload(),
        declare_in_manifest=True,
    )
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 0, proc.stderr


def test_gate_require_calibration_fails_on_missing_pointer(
    tmp_path: Path,
) -> None:
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED,
        dst=tmp_path / "run_no_pointer",
        calibration_payload=None,
        declare_in_manifest=False,
    )
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 1
    assert "missing 'calibration_report'" in proc.stderr


def test_gate_require_calibration_fails_on_missing_file(
    tmp_path: Path,
) -> None:
    """Manifest declares a calibration_report path but the file isn't there."""
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED,
        dst=tmp_path / "run_missing_file",
        calibration_payload=None,
        declare_in_manifest=False,
    )
    manifest_path = staged / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["calibration_report"] = str(staged / "calibration.json")
    manifest_path.write_text(json.dumps(manifest))
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 1
    assert "calibration_report file missing" in proc.stderr


def test_gate_require_calibration_fails_on_fpr_breach(
    tmp_path: Path,
) -> None:
    payload = _passing_calibration_payload()
    payload["per_tier"][0]["fpr"] = 0.30  # threshold for diff_valid is 0.05
    payload["per_tier"][0]["fp"] = 3
    payload["per_tier"][0]["tn"] = 7
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED,
        dst=tmp_path / "run_fpr_breach",
        calibration_payload=payload,
        declare_in_manifest=True,
    )
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 1
    assert "violates thresholds" in proc.stderr
    assert "diff_valid" in proc.stderr
    assert "max_fpr" in proc.stderr


def test_gate_require_calibration_fails_on_fnr_breach(
    tmp_path: Path,
) -> None:
    payload = _passing_calibration_payload()
    payload["per_tier"][0]["fnr"] = 0.50  # threshold for diff_valid is 0.10
    payload["per_tier"][0]["fn"] = 5
    payload["per_tier"][0]["tp"] = 5
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED,
        dst=tmp_path / "run_fnr_breach",
        calibration_payload=payload,
        declare_in_manifest=True,
    )
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 1
    assert "violates thresholds" in proc.stderr
    assert "max_fnr" in proc.stderr


def test_gate_require_calibration_fails_on_null_rate_when_threshold_set(
    tmp_path: Path,
) -> None:
    """A tier whose calibration produced no observations cannot satisfy a
    numeric threshold even though the file is present."""
    payload = _passing_calibration_payload()
    payload["per_tier"][0]["fpr"] = None
    payload["per_tier"][0]["fnr"] = None
    payload["per_tier"][0]["tp"] = 0
    payload["per_tier"][0]["fp"] = 0
    payload["per_tier"][0]["tn"] = 0
    payload["per_tier"][0]["fn"] = 0
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED,
        dst=tmp_path / "run_null_rates",
        calibration_payload=payload,
        declare_in_manifest=True,
    )
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 1
    assert "fpr is null" in proc.stderr or "fnr is null" in proc.stderr


def test_gate_require_calibration_fails_on_corrupt_json(
    tmp_path: Path,
) -> None:
    staged = _stage_run_with_calibration(
        src=RUN_STAMPED,
        dst=tmp_path / "run_corrupt",
        calibration_payload=None,
        declare_in_manifest=False,
    )
    cal_path = staged / "calibration.json"
    cal_path.write_text("{ this is not valid json")
    manifest_path = staged / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["calibration_report"] = str(cal_path)
    manifest_path.write_text(json.dumps(manifest))
    proc = _run_gate(staged, "--require-calibration")
    assert proc.returncode == 1
    assert "cannot load calibration report" in proc.stderr


# ---------------------------------------------------------------------------
# Canonical committed calibration.json sanity check
# ---------------------------------------------------------------------------


def test_committed_calibration_satisfies_thresholds() -> None:
    """The shipped calibration.json must already pass the docs thresholds.

    Without this guard, a published headline run could ship with a
    calibration that subtly fails one threshold and only get caught in CI."""
    cal_path = REPO_ROOT / "configs" / "recipes" / "go_import_rewrite.calibration.json"
    assert cal_path.is_file()
    from migration_evals.calibration import (
        load_calibration_thresholds,
        validate_against_thresholds,
    )

    report = CalibrationReport.from_path(cal_path)
    thresholds = load_calibration_thresholds(HYPOTHESES_PATH)
    violations = validate_against_thresholds(report, thresholds)
    assert violations == [], violations
