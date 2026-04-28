"""End-to-end smoke for scripts/run_eval.py.

The driver wires three pieces together:

    1. ChangesetProvider -> /tmp/eval/<id>/{repo, repo/patch.diff, meta.json}
    2. <eval-root>/<id>/repo/meta.json synthesized from the recipe template
       + the changeset provenance
    3. runner.run_from_config() over a transient YAML config

This test exercises only the Tier-0 (``diff_valid``) stage so the smoke
runs offline without Docker or a sandbox cassette. It seeds a local
file:// remote, stages two changesets (one valid patch, one broken),
runs the driver, and asserts the result.json files land with the
correct success / failure_class.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# `seeded_remote` comes from tests/conftest.py.

_REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = _REPO_ROOT / "scripts" / "run_eval.py"
RECIPE_PATH = _REPO_ROOT / "configs" / "recipes" / "java8_17.yaml"

# (migration_id, build_cmd_prefix, dockerfile_prefix) — one entry per
# recipe template under configs/recipes/. Parametrized tests below
# assert the funnel can load each template and synthesize a repo
# meta.json without per-recipe special-casing.
RECIPE_CASES = [
    ("java8_17", "mvn", "FROM maven"),
    ("go_import_rewrite", "go build", "FROM golang"),
    ("dockerfile_base_image_bump", "docker build", "FROM docker"),
    ("go_version_upgrade", "go build", "FROM golang"),
]


def _recipe_path(migration_id: str) -> Path:
    return _REPO_ROOT / "configs" / "recipes" / f"{migration_id}.yaml"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_eval", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_eval"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def re_mod():
    return _load_module()


def _valid_patch() -> str:
    return (
        "diff --git a/foo.txt b/foo.txt\n"
        "--- a/foo.txt\n"
        "+++ b/foo.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+world\n"
    )


def _broken_patch() -> str:
    return (
        "diff --git a/foo.txt b/foo.txt\n"
        "--- a/foo.txt\n"
        "+++ b/foo.txt\n"
        "@@ -1 +1 @@\n"
        "-not-the-actual-content\n"
        "+world\n"
    )


def _stage(staged: Path, instance_id: str, *, repo_url: str, sha: str, patch: str) -> None:
    d = staged / instance_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "repo_url": repo_url,
                "commit_sha": sha,
                "workflow_id": f"wf-{instance_id}",
                "agent_runner": "claude_code",
                "agent_model": "claude-sonnet-4-6",
            }
        )
    )
    (d / "patch.diff").write_text(patch)


# -- load_recipe_template --------------------------------------------------


@pytest.mark.parametrize(
    "migration_id,build_prefix,dockerfile_prefix",
    RECIPE_CASES,
    ids=[c[0] for c in RECIPE_CASES],
)
def test_load_recipe_template_reads_yaml(
    re_mod, migration_id: str, build_prefix: str, dockerfile_prefix: str
) -> None:
    tmpl = re_mod.load_recipe_template(_recipe_path(migration_id))
    assert tmpl["migration_id"] == migration_id
    assert tmpl["recipe"]["dockerfile"].startswith(dockerfile_prefix)
    assert tmpl["recipe"]["build_cmd"].startswith(build_prefix)
    assert "oracle_spec" in tmpl["stamps"]


# -- synthesize_repo_meta --------------------------------------------------


@pytest.mark.parametrize(
    "migration_id,build_prefix,dockerfile_prefix",
    RECIPE_CASES,
    ids=[c[0] for c in RECIPE_CASES],
)
def test_synthesize_repo_meta_merges_template_and_provenance(
    re_mod,
    tmp_path: Path,
    migration_id: str,
    build_prefix: str,
    dockerfile_prefix: str,
) -> None:
    inst_root = tmp_path / "inst-1"
    repo_dir = inst_root / "repo"
    repo_dir.mkdir(parents=True)
    (inst_root / "meta.json").write_text(
        json.dumps(
            {
                "repo_url": "https://github.com/example/foo",
                "commit_sha": "a" * 40,
                "workflow_id": "wf-1",
                "agent_runner": "claude_code",
                "agent_model": "claude-sonnet-4-6",
            }
        )
    )
    tmpl = re_mod.load_recipe_template(_recipe_path(migration_id))

    re_mod.synthesize_repo_meta(inst_root, tmpl)

    repo_meta = json.loads((repo_dir / "meta.json").read_text())
    # Recipe fields come from the template.
    assert repo_meta["build_cmd"].startswith(build_prefix)
    assert repo_meta["dockerfile"].startswith(dockerfile_prefix)
    # Provenance + identity come from the changeset.
    assert repo_meta["migration_id"] == migration_id
    assert repo_meta["agent_model"] == "claude-sonnet-4-6"
    assert repo_meta["task_id"] == "inst-1"


# -- driver E2E (Tier-0 only, no sandbox) ---------------------------------


@pytest.mark.parametrize(
    "migration_id", [c[0] for c in RECIPE_CASES], ids=[c[0] for c in RECIPE_CASES]
)
def test_main_runs_funnel_tier0_and_writes_result_jsons(
    re_mod, tmp_path: Path, seeded_remote, migration_id: str
) -> None:
    url, sha = seeded_remote
    staged = tmp_path / "staged"
    _stage(staged, "good", repo_url=url, sha=sha, patch=_valid_patch())
    _stage(staged, "bad", repo_url=url, sha=sha, patch=_broken_patch())
    eval_root = tmp_path / "eval"
    out_root = tmp_path / "out"

    rc = re_mod.main(
        [
            "--migration",
            migration_id,
            "--provider",
            "filesystem",
            "--root",
            str(staged),
            "--eval-root",
            str(eval_root),
            "--output-root",
            str(out_root),
            "--variant",
            "smoke",
            "--stages",
            "diff",
            "good",
            "bad",
        ]
    )
    assert rc == 0

    # One result.json per instance under output_root/<repo_name>_<seed>/
    written = sorted(out_root.glob("*/result.json"))
    assert len(written) == 2

    payloads = {p.parent.name: json.loads(p.read_text()) for p in written}
    # Tier-0 passes for the valid patch, fails (agent_error) for the broken one.
    good_key = next(k for k in payloads if k.startswith("good_"))
    bad_key = next(k for k in payloads if k.startswith("bad_"))
    assert payloads[good_key]["success"] is True
    assert payloads[bad_key]["success"] is False
    assert payloads[bad_key]["failure_class"] == "agent_error"
    # Both carry the migration_id and agent_model from the synthesized meta.
    for p in payloads.values():
        assert p["migration_id"] == migration_id
        assert p["agent_model"] == "claude-sonnet-4-6"


def test_main_emits_manifest_and_passes_publication_gate(
    re_mod, tmp_path: Path, seeded_remote
) -> None:
    """Driver writes manifest.json next to result.json, and the gate
    accepts the resulting run dir without further wiring.

    This locks the contract that ``run_eval.py`` produces gate-clean
    output by default: the publication gate has no work to do beyond
    pointing it at ``--output-root``. A regression that drops the
    manifest or breaks the stamps mapping is caught here.
    """
    url, sha = seeded_remote
    staged = tmp_path / "staged"
    _stage(staged, "good", repo_url=url, sha=sha, patch=_valid_patch())
    eval_root = tmp_path / "eval"
    out_root = tmp_path / "out"

    rc = re_mod.main(
        [
            "--migration",
            "java8_17",
            "--provider",
            "filesystem",
            "--root",
            str(staged),
            "--eval-root",
            str(eval_root),
            "--output-root",
            str(out_root),
            "--variant",
            "smoke",
            "--stages",
            "diff",
            "good",
        ]
    )
    assert rc == 0

    manifest_path = out_root / "manifest.json"
    assert manifest_path.is_file(), "manifest.json must be emitted by the driver"
    manifest = json.loads(manifest_path.read_text())
    for key in ("oracle_spec", "recipe_spec", "hypotheses"):
        assert key in manifest, f"manifest missing required key {key!r}"
        # Paths are written relative to output_root and must resolve to
        # a real committed file.
        resolved = (out_root / manifest[key]).resolve()
        assert resolved.is_file(), f"manifest[{key!r}] points at non-existent file: {resolved}"

    # Publication gate must pass against the run dir straight from the
    # driver - no manual stamping or post-processing.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "migration_evals.publication_gate",
            "--check-run",
            str(out_root),
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
    )
    assert proc.returncode == 0, (
        f"gate failed on driver output: stdout={proc.stdout!r} " f"stderr={proc.stderr!r}"
    )


def test_main_skips_pull_failures_and_returns_partial_exit(
    re_mod, tmp_path: Path, seeded_remote
) -> None:
    """A pull-failure instance does not stop the run; exit code reflects
    that at least one instance failed before the funnel even saw it."""
    url, sha = seeded_remote
    staged = tmp_path / "staged"
    _stage(staged, "good", repo_url=url, sha=sha, patch=_valid_patch())
    # "missing" is not in the staged dir -> pull fails.
    eval_root = tmp_path / "eval"
    out_root = tmp_path / "out"

    rc = re_mod.main(
        [
            "--migration",
            "java8_17",
            "--provider",
            "filesystem",
            "--root",
            str(staged),
            "--eval-root",
            str(eval_root),
            "--output-root",
            str(out_root),
            "--variant",
            "smoke",
            "--stages",
            "diff",
            "good",
            "missing",
        ]
    )
    # Pull fail counts as a non-zero exit; result.json still emitted for "good".
    assert rc == 2
    assert list(out_root.glob("good_*/result.json")), "good instance must still produce result.json"


# -- batch-change-canonical fixtures --------------------------------------

_CANONICAL_EXAMPLES = _REPO_ROOT / "tests" / "fixtures" / "changeset_examples"


def _stage_canonical(
    staged: Path,
    instance_id: str,
    *,
    example_dir: Path,
    repo_url: str,
    sha: str,
) -> None:
    """Stage a canonical example into filesystem-provider layout.

    Reads patch.diff verbatim from the committed example and rewrites
    meta.json's repo_url/commit_sha to point at the in-process seeded
    remote so pull_changesets can clone it.
    """
    d = staged / instance_id
    d.mkdir(parents=True, exist_ok=True)
    meta = json.loads((example_dir / "meta.json").read_text(encoding="utf-8"))
    meta["repo_url"] = repo_url
    meta["commit_sha"] = sha
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "patch.diff").write_text(
        (example_dir / "patch.diff").read_text(encoding="utf-8"),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "migration_id,example_subpath,instance_id,remote_fixture",
    [
        (
            "go_import_rewrite",
            "go_import_rewrite/ghodss_to_sigs",
            "canonical-go-1",
            "seeded_go_import_remote",
        ),
        (
            "dockerfile_base_image_bump",
            "dockerfile_base_image_bump/alpine_to_debian",
            "canonical-dockerfile-1",
            "seeded_dockerfile_bump_remote",
        ),
        (
            "go_version_upgrade",
            "go_version_upgrade/bump_1_22_to_1_23",
            "canonical-go-version-1",
            "seeded_go_version_upgrade_remote",
        ),
    ],
    ids=["go_import_rewrite", "dockerfile_base_image_bump", "go_version_upgrade"],
)
def test_canonical_example_passes_tier0(
    re_mod,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    migration_id: str,
    example_subpath: str,
    instance_id: str,
    remote_fixture: str,
) -> None:
    """Each batch-change-canonical example applies cleanly at tier 0
    against its matching seeded remote.

    Higher tiers are designed-into / out-of these examples (see each
    fixture's README) but require Go or Docker on PATH and are not
    exercised in CI.
    """
    url, sha = request.getfixturevalue(remote_fixture)
    example = _CANONICAL_EXAMPLES / example_subpath
    staged = tmp_path / "staged"
    _stage_canonical(staged, instance_id, example_dir=example, repo_url=url, sha=sha)
    eval_root = tmp_path / "eval"
    out_root = tmp_path / "out"

    rc = re_mod.main(
        [
            "--migration",
            migration_id,
            "--provider",
            "filesystem",
            "--root",
            str(staged),
            "--eval-root",
            str(eval_root),
            "--output-root",
            str(out_root),
            "--variant",
            "canonical",
            "--stages",
            "diff",
            instance_id,
        ]
    )
    assert rc == 0

    written = sorted(out_root.glob("*/result.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text())
    assert payload["success"] is True, f"canonical {migration_id} must pass tier 0; got {payload}"
    assert payload["migration_id"] == migration_id


# -- provider config plumbing ---------------------------------------------


def test_build_provider_config_filesystem_requires_root(re_mod) -> None:
    parser = re_mod._build_parser()
    args = parser.parse_args(
        [
            "--migration",
            "java8_17",
            "--provider",
            "filesystem",
            "--output-root",
            "/tmp/out",
            "--variant",
            "v",
            "x",
        ]
    )
    with pytest.raises(ValueError, match="filesystem requires --root"):
        re_mod._build_provider_config(args)


def test_build_provider_config_http_requires_base_url(re_mod) -> None:
    parser = re_mod._build_parser()
    args = parser.parse_args(
        [
            "--migration",
            "java8_17",
            "--provider",
            "http",
            "--output-root",
            "/tmp/out",
            "--variant",
            "v",
            "x",
        ]
    )
    with pytest.raises(ValueError, match="http requires --base-url"):
        re_mod._build_provider_config(args)


def test_build_provider_config_http_threads_through_optionals(re_mod) -> None:
    parser = re_mod._build_parser()
    args = parser.parse_args(
        [
            "--migration",
            "java8_17",
            "--provider",
            "http",
            "--base-url",
            "https://artifacts.example.com",
            "--http-header",
            "Authorization: Bearer xyz",
            "--http-header",
            "X-Trace: abc",
            "--http-timeout-s",
            "5.0",
            "--http-max-bytes",
            "1024",
            "--output-root",
            "/tmp/out",
            "--variant",
            "v",
            "x",
        ]
    )
    cfg = re_mod._build_provider_config(args)
    assert cfg == {
        "base_url": "https://artifacts.example.com",
        "headers": {"Authorization": "Bearer xyz", "X-Trace": "abc"},
        "timeout_s": 5.0,
        "max_bytes": 1024,
    }


def test_parse_http_headers_rejects_malformed(re_mod) -> None:
    with pytest.raises(ValueError, match="KEY:VALUE"):
        re_mod._parse_http_headers(["malformed-no-colon"])
    with pytest.raises(ValueError, match="key is empty"):
        re_mod._parse_http_headers([": no-key"])


def test_main_unknown_migration_returns_exit_1(re_mod, tmp_path: Path) -> None:
    rc = re_mod.main(
        [
            "--migration",
            "bogus_migration",
            "--provider",
            "filesystem",
            "--root",
            str(tmp_path),
            "--eval-root",
            str(tmp_path / "eval"),
            "--output-root",
            str(tmp_path / "out"),
            "--variant",
            "smoke",
            "good",
        ]
    )
    assert rc == 1
