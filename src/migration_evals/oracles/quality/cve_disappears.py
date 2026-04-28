"""Named-CVE-disappears quality oracle (migration-evals-o7h).

For dep-bump batch-change recipes whose corpus targets a single
vulnerable package version, this oracle answers a focused question:
after the agent's diff is applied, does ``trivy fs`` still report the
named CVE?

The oracle is **informational only** — it always returns ``passed=True``.
The signal lives in ``details.cve_present`` (mirrors the contract used by
:mod:`migration_evals.oracles.quality.baseline_comparison`). Flipping
``passed=False`` based on an externally-maintained vulnerability database
is exactly the funnel-determinism hazard ADR 0001 warns about (the same
input could grade differently on two runs because the trivy DB advanced
between them); the verdict surfaces as a per-tier pass-rate in the
report's *Batch-change quality* section instead.

Opt-in is **per-recipe only** — set ``quality.cve_id`` and
``quality.cve_scanner_tool=trivy`` in the recipe YAML. ADR 0001 forbids
per-instance ``meta.json`` overrides because that resurrects the
single-rule-across-many-repos violation the ADR excludes.

Trivy is **not bundled** in the default sandbox image. The oracle returns
a ``skipped`` verdict when ``trivy`` is missing from the host PATH so
recipe authors can opt in by installing trivy locally without the
framework taking a hard dependency on a vulnerability scanner with a
rolling DB freshness problem.

Trivy invocation hardening:

- ``--skip-db-update`` and ``--skip-java-db-update`` keep the scan
  offline; recipe authors are responsible for warming the cache before
  the eval run. A scan that needs the DB but cannot reach it fails
  closed (``skipped``) rather than hanging on the timeout.
- ``--offline-scan`` blocks any network egress trivy might otherwise
  attempt for license/secret-scanning auxiliaries.
- No ``--severity`` filter: the named-CVE check is severity-blind by
  definition (we look up an exact ID).
- Output JSON ``SchemaVersion`` is gated against a known-supported
  list. Unknown schemas → ``skipped`` with the version recorded in
  ``details``, never a false-positive ``cve_present`` reading.
- Both the trivy CLI version and the vulnerability-DB ``UpdatedAt``
  timestamp (when surfaced by trivy) are stamped into ``details`` so
  ``oracle_spec_sha`` does not lie about which DB produced the verdict.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from migration_evals.oracles.verdict import OracleVerdict
from migration_evals.quality_spec import QualitySpec

TIER_NAME = "cve_disappears"
DEFAULT_COST_USD = 0.0

# Wall-clock cap on the scan itself. Cache is assumed warm via the
# explicit --skip-db-update flag below; 15s is comfortably generous for
# small repo trees and short enough to surface real hangs.
TRIVY_TIMEOUT_SECONDS = 15

# Memory cap on the trivy stdout buffer. ``subprocess.run(capture_output=True)``
# reads the whole stream into memory before the caller sees it, so a
# pathological repo state that triggers trivy to emit hundreds of MB of
# JSON could OOM the eval-runner process (the oracle runs on the host,
# not in the sandbox). Mirrors the ``MAX_DIFF_BYTES`` pattern in
# ``touched_paths.py``. 20 MB is well above any realistic dep-bump-repo
# scan; a scan that genuinely produces more is a corpus-shape problem
# the oracle should refuse rather than silently truncate.
MAX_TRIVY_STDOUT_BYTES = 20 * 1024 * 1024

# Cap on stderr / OSError text echoed into the verdict's ``details.reason``
# field. Trivy's first-line stderr often carries the absolute repo path it
# was scanning; the verdict ends up serialised into ``result.json``, which
# may be published. Capping prevents both pathological stderr from bloating
# every skipped verdict and accidental disclosure of long internal paths.
MAX_REASON_TEXT_CHARS = 200

# JSON SchemaVersion values this oracle understands. Bumped explicitly
# when a new trivy major audited and confirmed not to have moved the
# Results[].Vulnerabilities[].VulnerabilityID field.
SUPPORTED_TRIVY_SCHEMA_VERSIONS: tuple[int, ...] = (2,)

# Captures the leading ``Version: <semver>`` line emitted by ``trivy --version``.
_VERSION_RE = re.compile(r"Version:\s*([0-9][0-9A-Za-z._+\-]*)")


@dataclass(frozen=True)
class _TrivyResult:
    """Outcome of one ``trivy fs`` invocation.

    ``stdout`` carries the JSON document (or ``""`` on hard failure).
    ``returncode`` is the trivy exit code; trivy uses 0 for "scan
    completed" regardless of findings, so a non-zero code here means
    the scan itself failed (DB missing, IO error, etc.).
    """

    returncode: int
    stdout: str
    stderr: str


def _which_trivy() -> str | None:
    return shutil.which("trivy")


def _run_trivy(repo_path: Path, cli: str) -> _TrivyResult:
    """Single seam for monkeypatching in tests.

    Runs ``trivy fs`` against ``repo_path`` using the resolved ``cli``
    binary path provided by the caller and returns a structured result.
    The caller is responsible for resolving ``cli`` via
    :func:`_which_trivy` and gating on a missing trivy *before* calling
    this function — that keeps the seam single-purpose (scan only) and
    avoids redundant ``shutil.which`` calls. The flag set is documented
    at module top and is intentionally not configurable per-recipe to
    keep the oracle deterministic across recipes.
    """
    proc = subprocess.run(  # nosec B603 — fixed argv list, no shell, repo_path validated by caller
        [
            cli,
            "fs",
            "--format",
            "json",
            "--quiet",
            "--skip-db-update",
            "--skip-java-db-update",
            "--offline-scan",
            str(repo_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=TRIVY_TIMEOUT_SECONDS,
    )
    return _TrivyResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _query_trivy_version(cli: str) -> str | None:
    """Best-effort capture of the trivy CLI version string."""
    try:
        proc = subprocess.run(  # nosec B603 — fixed argv list, no shell
            [cli, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    text = proc.stdout or proc.stderr
    match = _VERSION_RE.search(text)
    if match:
        return match.group(1)
    return text.strip().splitlines()[0] if text.strip() else None


def _skipped(reason: str, **extra: Any) -> OracleVerdict:
    details: dict[str, Any] = {"skipped": True, "reason": reason}
    details.update(extra)
    return OracleVerdict(
        tier=TIER_NAME,
        passed=True,
        cost_usd=DEFAULT_COST_USD,
        details=details,
    )


def _scan_for_cve(payload: Any, cve_id: str) -> bool:
    """Return True if ``cve_id`` appears as a VulnerabilityID anywhere
    in trivy's ``Results[].Vulnerabilities[]`` shape.

    Trivy v0.50+ uses a nested ``Results`` list; this function walks it
    explicitly rather than tolerantly recursing, so a future
    schema-shape change surfaces as a parse failure (skipped) rather
    than as a silent miss.
    """
    if not isinstance(payload, dict):
        return False
    results = payload.get("Results")
    if not isinstance(results, list):
        return False
    for entry in results:
        if not isinstance(entry, dict):
            continue
        vulns = entry.get("Vulnerabilities")
        if not isinstance(vulns, list):
            continue
        for vuln in vulns:
            if isinstance(vuln, dict) and vuln.get("VulnerabilityID") == cve_id:
                return True
    return False


def _extract_db_updated_at(payload: Any) -> str | None:
    """Pull the vulnerability-DB ``UpdatedAt`` timestamp from
    ``Metadata.DB.UpdatedAt`` when trivy stamps it; missing is fine
    (returned as ``None``)."""
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("Metadata")
    db = metadata.get("DB") if isinstance(metadata, dict) else None
    updated = db.get("UpdatedAt") if isinstance(db, dict) else None
    return updated if isinstance(updated, str) and updated else None


def run(repo_path: Path, quality_spec: QualitySpec) -> OracleVerdict:
    repo_path = Path(repo_path)

    if quality_spec.cve_id is None:
        return _skipped("cve_id not configured")
    if quality_spec.cve_scanner_tool is None:
        return _skipped("cve_scanner_tool not configured", cve_id=quality_spec.cve_id)
    # cve_scanner_tool is validated to be ("trivy",) in QualitySpec.__post_init__,
    # so by the time we get here it is always "trivy".
    cli = _which_trivy()
    if cli is None:
        return _skipped(
            "trivy not on PATH (recipe-author-provided tool; not bundled in default sandbox)",
            scanner_tool="trivy",
            cve_id=quality_spec.cve_id,
        )
    scanner_version = _query_trivy_version(cli)

    try:
        result = _run_trivy(repo_path, cli)
    except subprocess.TimeoutExpired:
        return _skipped(
            f"trivy timed out after {TRIVY_TIMEOUT_SECONDS}s",
            scanner_tool="trivy",
            scanner_version=scanner_version,
            cve_id=quality_spec.cve_id,
        )
    except OSError as exc:
        # Truncate the OSError text — its __str__ may include the
        # absolute filesystem path of the failing execve, which would
        # otherwise land verbatim in result.json via details.reason.
        return _skipped(
            f"trivy invocation failed: {str(exc)[:MAX_REASON_TEXT_CHARS]}",
            scanner_tool="trivy",
            scanner_version=scanner_version,
            cve_id=quality_spec.cve_id,
        )

    base_details: dict[str, Any] = {
        "scanner_tool": "trivy",
        "scanner_version": scanner_version,
        "cve_id": quality_spec.cve_id,
    }

    if result.returncode != 0:
        stderr_lines = (result.stderr or "").strip().splitlines()
        # Cap stderr first line — trivy commonly prefixes ERROR lines
        # with the absolute repo path being scanned; copying the full
        # line into details.reason can leak internal paths into
        # published result.json files.
        first_line = (stderr_lines[0] if stderr_lines else "no stderr")[:MAX_REASON_TEXT_CHARS]
        return _skipped(
            f"trivy exited {result.returncode}: {first_line}",
            **base_details,
        )

    if len(result.stdout.encode("utf-8", errors="replace")) > MAX_TRIVY_STDOUT_BYTES:
        return _skipped(
            f"trivy stdout exceeded MAX_TRIVY_STDOUT_BYTES ({MAX_TRIVY_STDOUT_BYTES}); "
            "refusing to parse — verify the corpus is dep-bump-shaped",
            **base_details,
        )

    try:
        payload = json.loads(result.stdout) if result.stdout else None
    except json.JSONDecodeError as exc:
        return _skipped(
            f"trivy stdout was not valid JSON: {exc.msg[:MAX_REASON_TEXT_CHARS]}",
            **base_details,
        )

    schema = payload.get("SchemaVersion") if isinstance(payload, dict) else None
    if not isinstance(schema, int) or schema not in SUPPORTED_TRIVY_SCHEMA_VERSIONS:
        return _skipped(
            f"unsupported trivy SchemaVersion {schema!r}; "
            f"this oracle understands {SUPPORTED_TRIVY_SCHEMA_VERSIONS}",
            schema_version=schema,
            **base_details,
        )

    cve_present = _scan_for_cve(payload, quality_spec.cve_id)
    db_updated_at = _extract_db_updated_at(payload)

    return OracleVerdict(
        tier=TIER_NAME,
        passed=True,
        cost_usd=DEFAULT_COST_USD,
        details={
            **base_details,
            "schema_version": schema,
            "db_updated_at": db_updated_at,
            "cve_present": cve_present,
        },
    )


__all__ = [
    "DEFAULT_COST_USD",
    "MAX_REASON_TEXT_CHARS",
    "MAX_TRIVY_STDOUT_BYTES",
    "SUPPORTED_TRIVY_SCHEMA_VERSIONS",
    "TIER_NAME",
    "TRIVY_TIMEOUT_SECONDS",
    "run",
]
