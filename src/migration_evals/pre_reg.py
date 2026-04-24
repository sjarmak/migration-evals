"""Spec-SHA stamping helpers for the migration eval framework (PRD M8-lite).

Every migration-eval trial result.json carries three SHA fields that bind the
result to the specific oracle spec, recipe spec, and pre-registered
hypotheses_and_thresholds document that governed it. The helpers in this
module compute and attach those stamps.

Exports
-------
compute_spec_sha(path)
    Return the sha256 hex digest of a file's bytes.
stamp_result(result, oracle_spec, recipe_spec, hypotheses)
    Return a NEW dict carrying the three SHA fields. The input `result` is
    never mutated (deep-copied before the SHAs are attached).

Rationale
---------
Stamping is a structural, mechanical operation. Any stale or missing stamp
should be caught by `scripts/maintenance/publication_gate.py`, which
recomputes the SHAs from the committed files and compares against what each
trial stored at scoring time.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path


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
) -> dict:
    """Return a deep copy of ``result`` with the three PRD M8-lite SHA stamps.

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

    Returns
    -------
    dict
        A new dict carrying ``oracle_spec_sha``, ``recipe_spec_sha``, and
        ``pre_reg_sha`` populated from the three files. All other keys are
        preserved from the input.
    """
    stamped = copy.deepcopy(result)
    stamped["oracle_spec_sha"] = compute_spec_sha(oracle_spec)
    stamped["recipe_spec_sha"] = compute_spec_sha(recipe_spec)
    stamped["pre_reg_sha"] = compute_spec_sha(hypotheses)
    return stamped


__all__ = ["compute_spec_sha", "stamp_result"]
