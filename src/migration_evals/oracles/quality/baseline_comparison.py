"""Baseline-tool comparison oracle (dsm).

For mechanical batch changes that a deterministic tool can solve, this
oracle answers the codex review's question directly: does the agent's
diff beat ``sed`` (or ``comby``, or ``gopls``)? If the baseline produces
the same effect, the agent's compute didn't pay for itself.

Implements three baseline tools, dispatched on
:attr:`migration_evals.quality_spec.QualitySpec.baseline_tool`:

- ``sed``: pure-Python regex sub via :func:`re.subn`. The recipe declares
  a :class:`~migration_evals.quality_spec.BaselinePattern` (match/replace
  regex pair plus an optional file glob).
- ``comby``: subprocess shellout to the ``comby`` CLI in stdin/stdout
  mode. The match/replace pair is interpreted as a comby template
  (generic matcher) rather than a Python regex. Tempdir isolation is
  not required here because comby runs in stdin/stdout mode and never
  touches the filesystem.
- ``gopls``: subprocess shellout to ``gopls rename`` for Go-identifier
  rename refactorings. The post-state subtree is copied into a tempdir
  before gopls runs (architect C1: ``gopls rename -w`` modifies files
  in place, so the original post-state must never be the target).

Architect H3 hold (migration_evals-09u): the standard sandbox image
does not yet bundle ``comby`` or ``gopls``. Both impls gate on
:func:`shutil.which` and emit a ``skipped`` verdict (same envelope
:func:`baseline_comparison` already uses for "no baseline_tool" and
"no baseline_pattern") when the binary is missing — so missing tools
do not flip a non-skip verdict and the report's ``baseline_passed_rate``
denominator stays consistent.

A baseline that produces an identical post-state means the agent's
diff was redundant against the baseline. A baseline that produces a
different post-state (or fails to make any change) shows the agent
added value beyond mechanical regex/comby/gopls.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from migration_evals.oracles.tier0_diff import PATCH_ARTIFACT_NAMES
from migration_evals.oracles.verdict import OracleVerdict
from migration_evals.quality_spec import BaselinePattern, QualitySpec

TIER_NAME = "baseline_comparison"
DEFAULT_COST_USD = 0.0

# Wall-clock cap on a single comby / gopls invocation. Cache is assumed
# warm; large repos that genuinely exceed this should be split into
# smaller scan units rather than have the cap raised.
COMBY_TIMEOUT_SECONDS = 30
GOPLS_TIMEOUT_SECONDS = 60

# Memory cap on subprocess stdout. ``capture_output=True`` reads the
# whole stream into RAM before the caller sees it; a pathological repo
# state that drives comby/gopls to emit hundreds of MB of text could OOM
# the eval-runner host process. Mirrors the cap pattern in
# :mod:`migration_evals.oracles.quality.cve_disappears`.
MAX_TOOL_STDOUT_BYTES = 20 * 1024 * 1024

# Cap on stderr / OSError text echoed into the verdict's
# ``details.reason`` field. Tools commonly prefix error lines with
# absolute filesystem paths; the verdict is serialised into result.json
# (which may be published) so we cap to prevent both bloat and incidental
# disclosure of internal paths.
MAX_REASON_TEXT_CHARS = 200


def _find_agent_diff(repo_path: Path) -> Path | None:
    for name in PATCH_ARTIFACT_NAMES:
        candidate = repo_path / name
        if candidate.is_file():
            return candidate
    return None


_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(\S+)")


def _files_touched_by_diff(diff_text: str) -> list[str]:
    files: list[str] = []
    for line in diff_text.splitlines():
        match = _FILE_HEADER_RE.match(line)
        if match and not line.startswith("+++ /dev/null"):
            files.append(match.group(1))
    return files


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _file_matches_glob(target: str, glob: str) -> bool:
    return fnmatch(target, glob)


def _skipped(reason: str, **extra: Any) -> OracleVerdict:
    details: dict[str, Any] = {"skipped": True, "reason": reason}
    details.update(extra)
    return OracleVerdict(
        tier=TIER_NAME,
        passed=True,
        cost_usd=DEFAULT_COST_USD,
        details=details,
    )


def _which_comby() -> str | None:
    return shutil.which("comby")


def _which_gopls() -> str | None:
    return shutil.which("gopls")


def _truncate_reason(text: str) -> str:
    return text[:MAX_REASON_TEXT_CHARS]


def _apply_baseline_sed(pre_state: str, pattern: BaselinePattern) -> tuple[str, int]:
    """Apply the ``sed``-style regex once and return (output, n_subs)."""
    return re.subn(pattern.match, pattern.replace, pre_state)


def _run_comby(
    post_state: str, pattern: BaselinePattern, cli: str
) -> subprocess.CompletedProcess[str]:
    """Single seam for monkeypatching comby in tests.

    Reads ``post_state`` via stdin, returns the rewritten text on stdout.
    No temporary files are written: comby in stdin/stdout mode is FS-pure,
    which trivially satisfies the architect C1 isolation invariant for
    this baseline.
    """
    return (
        subprocess.run(  # nosec B603 — fixed argv, no shell, inputs are caller-owned recipe strings
            [
                cli,
                pattern.match,
                pattern.replace,
                "-stdin",
                "-stdout",
                "-matcher",
                ".generic",
            ],
            check=False,
            capture_output=True,
            text=True,
            input=post_state,
            timeout=COMBY_TIMEOUT_SECONDS,
        )
    )


def _apply_baseline_comby(
    post_state: str, pattern: BaselinePattern, cli: str
) -> tuple[str | None, str]:
    """Run comby on ``post_state`` and return (output_or_None, error_reason).

    On any failure (non-zero exit, timeout, OSError, oversized stdout)
    returns ``(None, reason)`` so the caller can emit a ``skipped``
    verdict. On success returns ``(output, "")``.
    """
    try:
        proc = _run_comby(post_state, pattern, cli)
    except subprocess.TimeoutExpired:
        return None, f"comby timed out after {COMBY_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return None, f"comby invocation failed: {_truncate_reason(str(exc))}"

    if proc.returncode != 0:
        first_line = (proc.stderr or "").strip().splitlines()[:1]
        msg = first_line[0] if first_line else "no stderr"
        return None, f"comby exited {proc.returncode}: {_truncate_reason(msg)}"

    if len(proc.stdout.encode("utf-8", errors="replace")) > MAX_TOOL_STDOUT_BYTES:
        return None, (f"comby stdout exceeded MAX_TOOL_STDOUT_BYTES ({MAX_TOOL_STDOUT_BYTES})")
    return proc.stdout, ""


def _run_gopls_rename(
    workdir: Path, file_rel: str, offset: int, new_name: str, cli: str
) -> subprocess.CompletedProcess[str]:
    """Single seam for monkeypatching gopls in tests.

    Invokes ``gopls rename -w <file>:<offset> <new_name>`` against
    ``workdir`` (the tempdir copy of the post-state). ``-w`` writes the
    rename in place; the caller is responsible for keeping the tempdir
    tree distinct from the original ``repo_path`` so the original
    post-state is never modified (architect C1).
    """
    return subprocess.run(  # nosec B603 — fixed argv, no shell, paths derived from caller-controlled tempdir
        [
            cli,
            "rename",
            "-w",
            f"{workdir / file_rel}:#{offset}",
            new_name,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=GOPLS_TIMEOUT_SECONDS,
        cwd=str(workdir),
    )


_IDENT_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _identifier_offset(text: str, identifier: str) -> int | None:
    """Return the byte offset of the first whole-word occurrence of
    ``identifier`` in ``text`` (Go uses UTF-8 source so byte == char for
    ASCII identifiers, which is what gopls expects). ``None`` if absent.
    """
    pat = _IDENT_RE_CACHE.get(identifier)
    if pat is None:
        pat = re.compile(rf"\b{re.escape(identifier)}\b")
        _IDENT_RE_CACHE[identifier] = pat
    match = pat.search(text)
    if match is None:
        return None
    return len(text[: match.start()].encode("utf-8", errors="replace"))


def _apply_baseline_gopls(
    repo_path: Path,
    targets: list[str],
    pattern: BaselinePattern,
    cli: str,
) -> tuple[dict[str, str] | None, str]:
    """Copy the repo to a tempdir, run ``gopls rename`` per target, and
    return (renamed_text_per_target, error_reason).

    On any failure returns ``(None, reason)``. On success returns a
    mapping from each touched relative path to the post-rename text in
    the tempdir copy. The original ``repo_path`` is never modified.
    """
    if not (repo_path / "go.mod").is_file():
        return None, "gopls baseline requires go.mod at repo root"

    with TemporaryDirectory(prefix="migration-evals-gopls-") as tmpdir:
        work = Path(tmpdir) / "repo"
        try:
            shutil.copytree(repo_path, work, symlinks=False, dirs_exist_ok=False)
        except OSError as exc:
            return None, f"failed to stage gopls workdir: {_truncate_reason(str(exc))}"

        renamed: dict[str, str] = {}
        for rel in targets:
            file_path = work / rel
            if not file_path.is_file():
                # file dropped from working tree; treat as no-op
                continue
            text = _read_text(file_path)
            offset = _identifier_offset(text, pattern.match)
            if offset is None:
                # baseline cannot rename a symbol it can't find; record
                # the original text so the comparison emits "no change"
                # for this target.
                renamed[rel] = text
                continue
            try:
                proc = _run_gopls_rename(work, rel, offset, pattern.replace, cli)
            except subprocess.TimeoutExpired:
                return None, f"gopls rename timed out after {GOPLS_TIMEOUT_SECONDS}s"
            except OSError as exc:
                return None, f"gopls invocation failed: {_truncate_reason(str(exc))}"
            if proc.returncode != 0:
                first_line = (proc.stderr or "").strip().splitlines()[:1]
                msg = first_line[0] if first_line else "no stderr"
                return None, f"gopls exited {proc.returncode}: {_truncate_reason(msg)}"
            renamed[rel] = _read_text(file_path)
        return renamed, ""


def _no_pattern_skip(tool: str) -> OracleVerdict:
    return _skipped(
        f"baseline_tool={tool} but baseline_pattern missing",
        baseline_tool=tool,
    )


def _no_diff_skip(tool: str) -> OracleVerdict:
    return _skipped(
        "no agent patch artifact to compare against",
        baseline_tool=tool,
    )


def _summarise(
    tool: str,
    pattern: BaselinePattern,
    matches: list[dict[str, Any]],
    differs: list[str],
    n_files: int,
    n_baseline_substitutions: int,
) -> OracleVerdict:
    # Heuristic decision rule shared across all three tools: if the
    # baseline produces zero substitutions on every post-state file
    # (i.e. the post-state already has the canonical replacement) AND
    # the agent also touched those files, the agent's effect equals the
    # baseline's effect.
    baseline_passed = n_files > 0 and n_baseline_substitutions == 0 and not differs
    agent_lift = 0.0 if baseline_passed else 1.0
    details: dict[str, Any] = {
        "baseline_tool": tool,
        "baseline_pattern": {
            "match": pattern.match,
            "replace": pattern.replace,
            "files": pattern.files,
        },
        "n_files": n_files,
        "baseline_passed": baseline_passed,
        "agent_lift": agent_lift,
        "matches": matches[:32],
    }
    if differs:
        details["files_where_baseline_differs"] = differs[:8]
    return OracleVerdict(
        tier=TIER_NAME,
        passed=True,
        cost_usd=DEFAULT_COST_USD,
        details=details,
    )


def _run_sed_baseline(
    repo_path: Path,
    pattern: BaselinePattern,
    matched_targets: list[str],
) -> OracleVerdict:
    n_files = 0
    n_baseline_substitutions = 0
    matches: list[dict[str, Any]] = []
    differs: list[str] = []
    for target in matched_targets:
        absolute = repo_path / target
        if not absolute.is_file():
            continue
        post = _read_text(absolute)
        baseline_post, n_subs = _apply_baseline_sed(post, pattern)
        n_files += 1
        n_baseline_substitutions += n_subs
        matches.append(
            {
                "path": target,
                "baseline_substitutions": n_subs,
                "agent_and_baseline_agree": baseline_post == post
                and (pattern.replace in post or n_subs == 0),
            }
        )
        if baseline_post != post:
            differs.append(target)
    return _summarise("sed", pattern, matches, differs, n_files, n_baseline_substitutions)


def _run_comby_baseline(
    repo_path: Path,
    pattern: BaselinePattern,
    matched_targets: list[str],
) -> OracleVerdict:
    cli = _which_comby()
    if cli is None:
        return _skipped(
            "comby not on PATH (recipe-author-provided tool; "
            "not bundled in default sandbox image)",
            baseline_tool="comby",
        )
    n_files = 0
    n_baseline_substitutions = 0
    matches: list[dict[str, Any]] = []
    differs: list[str] = []
    for target in matched_targets:
        absolute = repo_path / target
        if not absolute.is_file():
            continue
        post = _read_text(absolute)
        baseline_post, err = _apply_baseline_comby(post, pattern, cli)
        if baseline_post is None:
            return _skipped(err, baseline_tool="comby", failing_target=target)
        # Comby doesn't report a substitution count; derive it as 1 if
        # the baseline output differs from the post-state, 0 otherwise.
        # This is asymmetric with sed (which reports the true count) but
        # the report consumer only inspects ``baseline_passed`` (a bool)
        # and ``agent_lift`` (0.0/1.0), so the looser count is sufficient.
        n_subs = 0 if baseline_post == post else 1
        n_files += 1
        n_baseline_substitutions += n_subs
        matches.append(
            {
                "path": target,
                "baseline_substitutions": n_subs,
                "agent_and_baseline_agree": baseline_post == post,
            }
        )
        if baseline_post != post:
            differs.append(target)
    return _summarise("comby", pattern, matches, differs, n_files, n_baseline_substitutions)


def _run_gopls_baseline(
    repo_path: Path,
    pattern: BaselinePattern,
    matched_targets: list[str],
) -> OracleVerdict:
    cli = _which_gopls()
    if cli is None:
        return _skipped(
            "gopls not on PATH (recipe-author-provided tool; "
            "not bundled in default sandbox image)",
            baseline_tool="gopls",
        )
    # Filter to .go files — gopls only knows how to rename Go identifiers.
    go_targets = [t for t in matched_targets if t.endswith(".go")]
    if not go_targets:
        return _skipped(
            "gopls baseline matched no .go files in agent diff",
            baseline_tool="gopls",
        )
    renamed_per_target, err = _apply_baseline_gopls(repo_path, go_targets, pattern, cli)
    if renamed_per_target is None:
        return _skipped(err, baseline_tool="gopls")

    n_files = 0
    n_baseline_substitutions = 0
    matches: list[dict[str, Any]] = []
    differs: list[str] = []
    for target in go_targets:
        absolute = repo_path / target
        if not absolute.is_file():
            continue
        post = _read_text(absolute)
        baseline_post = renamed_per_target.get(target, post)
        n_subs = 0 if baseline_post == post else 1
        n_files += 1
        n_baseline_substitutions += n_subs
        matches.append(
            {
                "path": target,
                "baseline_substitutions": n_subs,
                "agent_and_baseline_agree": baseline_post == post,
            }
        )
        if baseline_post != post:
            differs.append(target)
    return _summarise("gopls", pattern, matches, differs, n_files, n_baseline_substitutions)


_BASELINE_DISPATCH = {
    "sed": _run_sed_baseline,
    "comby": _run_comby_baseline,
    "gopls": _run_gopls_baseline,
}


def run(repo_path: Path, quality_spec: QualitySpec) -> OracleVerdict:
    repo_path = Path(repo_path)
    if quality_spec.baseline_tool is None:
        return OracleVerdict(
            tier=TIER_NAME,
            passed=True,
            cost_usd=DEFAULT_COST_USD,
            details={"skipped": True, "reason": "no baseline_tool"},
        )
    runner = _BASELINE_DISPATCH.get(quality_spec.baseline_tool)
    if runner is None:
        # ALLOWED_BASELINE_TOOLS keeps QualitySpec from accepting an
        # unknown tool, so this branch is reachable only if a future
        # tool is added to the enum without also wiring a runner.
        return _skipped(
            f"baseline_tool {quality_spec.baseline_tool!r} accepted by "
            "QualitySpec but no runner is registered",
            baseline_tool=quality_spec.baseline_tool,
        )
    pattern = quality_spec.baseline_pattern
    if pattern is None:
        return _no_pattern_skip(quality_spec.baseline_tool)
    agent_path = _find_agent_diff(repo_path)
    if agent_path is None:
        return _no_diff_skip(quality_spec.baseline_tool)

    diff_text = _read_text(agent_path)
    touched = _files_touched_by_diff(diff_text)
    matched_targets = [t for t in touched if _file_matches_glob(t, pattern.files)]
    return runner(repo_path, pattern, matched_targets)


__all__ = [
    "COMBY_TIMEOUT_SECONDS",
    "DEFAULT_COST_USD",
    "GOPLS_TIMEOUT_SECONDS",
    "MAX_REASON_TEXT_CHARS",
    "MAX_TOOL_STDOUT_BYTES",
    "TIER_NAME",
    "run",
]
