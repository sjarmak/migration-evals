#!/usr/bin/env python3
"""Pull agent changesets into the funnel's on-disk layout (M9.3 / tier1-1).

For each supplied instance id, pulls the agent-produced diff plus the
base commit it was produced against out of an artifact-storage backend
(via a :class:`~migration_evals.changesets.ChangesetProvider`) and
stages them in the layout the tiered-oracle funnel consumes::

    <out-root>/<instance_id>/repo/            <- fresh clone at base commit
    <out-root>/<instance_id>/repo/patch.diff  <- agent's unified diff
    <out-root>/<instance_id>/meta.json        <- {repo_url, commit_sha,
                                                  workflow_id, agent_runner,
                                                  agent_model}

Only the ``filesystem`` provider ships in-repo; other backends (S3-
compatible object store, blob storage, HTTP artifact server, ...)
implement :class:`ChangesetProvider` alongside the consuming pipeline
and register themselves before this script runs. See
``src/migration_evals/changesets.py``.

Usage
-----
    # Stage two instances from a local directory of pre-staged artifacts
    python scripts/pull_changesets.py \\
        --provider filesystem --root /tmp/staged \\
        --out-root /tmp/eval \\
        inst-1 inst-2

    # Pull a list from a file (one instance id per line, # for comments)
    python scripts/pull_changesets.py \\
        --provider filesystem --root /tmp/staged \\
        --out-root /tmp/eval \\
        --instance-ids-file ids.txt

Exit codes
----------
0   All instances staged and ``git apply --check`` succeeded on each.
1   Wrong CLI usage (missing provider config, no ids, ...).
2   One or more instances failed to stage, or at least one patch failed
    ``git apply --check``. Partial results are left on disk for debugging.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from migration_evals.changesets import (  # noqa: E402
    Changeset,
    ChangesetProvider,
    get_provider,
    validate_instance_id,
)


@dataclass(frozen=True)
class InstanceResult:
    instance_id: str
    staged_dir: Path | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


def load_instance_ids(path: Path) -> list[str]:
    """Read instance ids from a newline-delimited file (``#`` comments ok)."""
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run ``cmd`` and raise RuntimeError on non-zero exit."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd)} exited {proc.returncode}\n"
            f"stdout: {proc.stdout.strip()}\nstderr: {proc.stderr.strip()}"
        )
    return proc


def _clone_at_commit(repo_url: str, dest: Path, commit_sha: str) -> None:
    """Clone ``repo_url`` into ``dest`` and check out ``commit_sha``.

    The ``--`` separator before ``repo_url`` is required: without it, a
    URL beginning with ``--`` (e.g. ``--upload-pack=evil``) would be
    parsed by git as an option rather than a positional argument.
    """
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--quiet", "--", repo_url, str(dest)])
    _run(["git", "checkout", "--quiet", commit_sha], cwd=dest)


def _write_meta(meta_path: Path, cs: Changeset) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "repo_url": cs.repo_url,
                "commit_sha": cs.commit_sha,
                "workflow_id": cs.workflow_id,
                "agent_runner": cs.agent_runner,
                "agent_model": cs.agent_model,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _git_apply_check(repo_dir: Path, patch_path: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        ["git", "apply", "--check", str(patch_path)],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0, proc.stderr.strip()


def stage_instance(
    provider: ChangesetProvider,
    instance_id: str,
    out_root: Path,
) -> InstanceResult:
    """Fetch + stage a single instance. Never raises; returns structured result."""
    try:
        validate_instance_id(instance_id)
    except ValueError as exc:
        return InstanceResult(instance_id, None, f"invalid instance_id: {exc}")
    inst_root = out_root / instance_id
    repo_dir = inst_root / "repo"
    meta_path = inst_root / "meta.json"
    patch_path = repo_dir / "patch.diff"
    try:
        cs = provider.fetch(instance_id)
    except Exception as exc:
        return InstanceResult(instance_id, None, f"fetch failed: {exc}")
    try:
        _clone_at_commit(cs.repo_url, repo_dir, cs.commit_sha)
        patch_path.write_text(cs.patch_diff, encoding="utf-8")
        _write_meta(meta_path, cs)
    except Exception as exc:
        return InstanceResult(instance_id, inst_root, f"stage failed: {exc}")
    ok, err = _git_apply_check(repo_dir, patch_path)
    return InstanceResult(
        instance_id=instance_id,
        staged_dir=inst_root,
        error=None if ok else f"git apply --check failed: {err}",
    )


def pull_all(
    provider: ChangesetProvider,
    instance_ids: Iterable[str],
    out_root: Path,
) -> list[InstanceResult]:
    return [stage_instance(provider, iid, out_root) for iid in instance_ids]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pull_changesets",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--provider",
        default="filesystem",
        help="ChangesetProvider name (default: filesystem).",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Root directory for the 'filesystem' provider (required when --provider=filesystem).",
    )
    parser.add_argument(
        "--out-root",
        default="/tmp/eval",
        help="Root directory for the staged layout. Default: /tmp/eval.",
    )
    parser.add_argument(
        "--instance-ids-file",
        default=None,
        help="Newline-delimited file of instance ids (# comments ok).",
    )
    parser.add_argument(
        "instance_ids",
        nargs="*",
        help="Instance ids to pull (positional). Combined with --instance-ids-file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    ids: list[str] = list(args.instance_ids)
    if args.instance_ids_file:
        ids.extend(load_instance_ids(Path(args.instance_ids_file)))
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

    out_root = Path(args.out_root)
    results = pull_all(provider, ids, out_root)

    n_ok = sum(1 for r in results if r.ok)
    for r in results:
        status = "OK" if r.ok else "FAIL"
        loc = str(r.staged_dir) if r.staged_dir else "-"
        note = f" ({r.error})" if r.error else ""
        print(f"[{status}] {r.instance_id}: {loc}{note}")
    print(f"staged {n_ok}/{len(results)} instances under {out_root}")

    return 0 if n_ok == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
