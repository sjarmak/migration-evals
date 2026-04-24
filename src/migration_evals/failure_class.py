"""Failure classification for migration eval trials.

PRD milestone M6: when a trial has `success=False`, we assign exactly one of
four `FailureClass` values based on the artifacts in the trial directory.

Decision order (checked top to bottom; first match wins):

    1. infra_error   — sandbox / container failures; harness never reached the agent
    2. harness_error — recipe / harness failures BEFORE the agent started
    3. oracle_error  — agent said pass but oracle subsystem threw
    4. agent_error   — everything else (the agent failed the task itself)

See `docs/migration_eval/failure_classification.md` for the full rule table
and example signatures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from migration_evals.types import FailureClass


# Phrases that, when found in status.txt / stderr / stdout, imply the sandbox
# or container layer failed before the trial could meaningfully run.
_INFRA_SIGNATURES = (
    "docker",
    "container exited",
    "sandbox failed",
    "image pull",
    "oci runtime",
    "kubelet",
    "no space left on device",
)

# Phrases that imply the harness / recipe layer failed before the agent
# could start its own work.
_HARNESS_SIGNATURES = (
    "recipe failed",
    "recipe error",
    "harness error",
    "harness failed",
    "harness timeout",
    "recipe not found",
    "install failed",
    "bootstrap failed",
)


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def _read_text_if_exists(path: Path) -> str:
    """Return file text lowercased, or empty string if file absent/unreadable."""
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return ""
    return ""


def _load_result(trial_dir: Path) -> dict:
    """Return the parsed result.json payload, or empty dict if missing."""
    result_path = trial_dir / "result.json"
    if not result_path.is_file():
        return {}
    try:
        with result_path.open("r", encoding="utf-8") as f:
            return json.loads(f.read())
    except (OSError, json.JSONDecodeError):
        return {}


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(needle in haystack for needle in needles)


# ---------------------------------------------------------------------------
# Per-class signal detectors
# ---------------------------------------------------------------------------

def _has_infra_signal(trial_dir: Path, payload: dict) -> bool:
    if bool(payload.get("infra_error_marker")):
        return True
    status_text = _read_text_if_exists(trial_dir / "status.txt")
    if _contains_any(status_text, _INFRA_SIGNATURES):
        return True
    # Some harnesses write to infra.log; cheap to check.
    infra_log = _read_text_if_exists(trial_dir / "infra.log")
    if _contains_any(infra_log, _INFRA_SIGNATURES):
        return True
    return False


def _has_harness_signal(trial_dir: Path, payload: dict) -> bool:
    if bool(payload.get("harness_error_marker")):
        return True
    for name in ("stderr.log", "stdout.log", "harness.log"):
        text = _read_text_if_exists(trial_dir / name)
        if _contains_any(text, _HARNESS_SIGNATURES):
            return True
    return False


def _has_oracle_signal(trial_dir: Path, payload: dict) -> bool:
    if bool(payload.get("oracle_error_marker")):
        return True
    # Agent self-reports success but overall success=False → oracle disagreed
    # or raised. This only fires when we've explicitly been told the agent
    # believed it was done (agent_reported_success=True).
    if bool(payload.get("agent_reported_success")) and not bool(payload.get("success", False)):
        return True
    # Look for oracle-subsystem traces on disk.
    for pattern in ("ast_oracle_trace*", "judge_error*", "oracle_trace*"):
        if any(trial_dir.glob(pattern)):
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify(trial_dir: Path) -> Optional[FailureClass]:
    """Return the failure class for a failed trial.

    Returns `None` when `result.json` exists and `success=True` — success
    trials do not have a failure class. Returns `FailureClass.AGENT_ERROR`
    when result.json is missing or unreadable (no evidence of infra /
    harness / oracle failure, so the agent layer is the default).
    """
    trial_dir = Path(trial_dir)
    payload = _load_result(trial_dir)

    if payload and bool(payload.get("success")):
        return None

    if _has_infra_signal(trial_dir, payload):
        return FailureClass.INFRA_ERROR
    if _has_harness_signal(trial_dir, payload):
        return FailureClass.HARNESS_ERROR
    if _has_oracle_signal(trial_dir, payload):
        return FailureClass.ORACLE_ERROR
    return FailureClass.AGENT_ERROR


__all__ = ["classify"]
