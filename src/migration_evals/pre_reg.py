"""Spec-SHA stamping helpers for the migration eval framework (PRD M8-lite).

Every migration-eval trial result.json carries SHA fields that bind the
result to the specific specs that governed it. Three are always present —
oracle_spec, recipe_spec, and the pre-registered hypotheses document — and
a fourth (``prompt_sha``) is added when the migration is prompt-defined
(rather than recipe-defined), so the agent prompt itself is auditable.

Exports
-------
compute_spec_sha(path)
    Return the sha256 hex digest of a file's bytes.
stamp_result(result, oracle_spec, recipe_spec, hypotheses, prompt_spec=None)
    Return a NEW dict carrying the three (or four) SHA fields. The input
    ``result`` is never mutated (deep-copied before the SHAs are attached).
    Pass ``prompt_spec`` when the run is driven by an agent prompt — the
    file's sha256 lands as ``prompt_sha`` in the result.

Rationale
---------
Stamping is a structural, mechanical operation. Any stale or missing stamp
should be caught by ``migration_evals.publication_gate``, which recomputes
the SHAs from the committed files and compares against what each trial
stored at scoring time. ``prompt_sha`` is enforced only when the run's
manifest declares a ``prompt_spec`` key.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Optional


def compute_spec_sha(path: Path) -> str:
    """Compute the sha256 hex digest of the bytes at ``path``.

    Reads the file in binary mode so that line-ending differences across
    platforms do not silently change the stamp. Returns the standard
    lowercase 64-character hex representation.
    """
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stamp_result(
    result: dict,
    oracle_spec: Path,
    recipe_spec: Path,
    hypotheses: Path,
    prompt_spec: Optional[Path] = None,
) -> dict:
    """Return a deep copy of ``result`` with the PRD M8-lite SHA stamps.

    Parameters
    ----------
    result
        An existing result payload (for example, one about to be written to
        ``result.json``). Not mutated.
    oracle_spec
        Path to the oracle spec file in force at scoring time.
    recipe_spec
        Path to the migration recipe spec file in force at scoring time.
    hypotheses
        Path to the pre-registered ``hypotheses_and_thresholds.md`` file.
    prompt_spec
        Optional path to the agent prompt file. When the migration target is
        defined by a prompt rather than a hand-authored recipe, this is the
        canonical artifact that must be hashed alongside the others. Stamped
        as ``prompt_sha`` when supplied.

    Returns
    -------
    dict
        A new dict carrying ``oracle_spec_sha``, ``recipe_spec_sha``, and
        ``pre_reg_sha`` populated from the three files (and ``prompt_sha``
        when ``prompt_spec`` is supplied). All other keys are preserved
        from the input.
    """
    stamped = copy.deepcopy(result)
    stamped["oracle_spec_sha"] = compute_spec_sha(oracle_spec)
    stamped["recipe_spec_sha"] = compute_spec_sha(recipe_spec)
    stamped["pre_reg_sha"] = compute_spec_sha(hypotheses)
    if prompt_spec is not None:
        stamped["prompt_sha"] = compute_spec_sha(prompt_spec)
    return stamped


__all__ = ["compute_spec_sha", "stamp_result"]
