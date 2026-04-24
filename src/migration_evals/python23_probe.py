"""Python 2→3 falsification probe (PRD M9).

This probe stress-tests the Java-derived M2 (harness recipe), M3 (synthetic
generator), and M5 (ledger) interfaces against a thin Python 2→3 mode.

It is **NOT a credible Python eval number**. Its sole purpose is to surface
schema inadequacies in the existing Java-shaped interfaces BEFORE any
external Java number ships. See ``docs/migration_eval/python23_probe.md``.

Probe contract
--------------

For each repo (either generated via ``python2_generator`` or supplied as a
fixture root), the probe attempts:

1. Harness — does the existing :class:`Recipe` schema carry an ecosystem /
   language discriminator? (Spoiler: it does not. Maven assumptions bake in.)
2. Synthetic — is the repo's case_type a member of the Java
   ``GENERATOR_PRIMITIVES`` set? (Spoiler: no, by construction.)
3. Ledger — does ``schemas/mig_result.schema.json`` permit a
   ``python_2to3_runtime`` oracle tier? (Spoiler: the enum is Java-shaped.)

Each module attempt records either ``"ok"`` or a structured mismatch entry.
The aggregate ``schema_revision_required`` flag flips to ``True`` when ≥2
distinct modules report a mismatch — that is the hard gate referenced from
the PRD M9 publication checklist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from migration_evals.synthetic import (
    java8_generator,
    python2_generator,
)

# Tier name we WOULD want to emit for runtime semantic checks specific to
# Python 2→3 (e.g. integer division, map() iterator, dict.items() view). The
# probe asserts this against ``mig_result.schema.json:oracle_tier``.
PYTHON_2TO3_RUNTIME_TIER: str = "python_2to3_runtime"

# Modules under stress test. Used as the canonical key set for the aggregate
# ``schema_revision_required`` calculation.
MODULES: tuple[str, ...] = ("harness", "synthetic", "ledger")

# Path to the result schema we validate against.
_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent / "schemas" / "mig_result.schema.json"
)


def compute_schema_revision_required(
    mismatches_by_module: dict[str, list[dict[str, str]]],
) -> bool:
    """Return ``True`` iff ≥2 distinct modules report ≥1 mismatch.

    Threshold logic is intentionally extracted so tests can exercise the
    True/False branches without running the full probe.
    """
    distinct = sum(
        1 for module in MODULES if mismatches_by_module.get(module)
    )
    return distinct >= 2


def _load_oracle_tier_enum() -> list[str]:
    """Read the ``oracle_tier`` enum from ``mig_result.schema.json``.

    Falls back to the known v1 enum if the schema cannot be read; this keeps
    the probe runnable in degraded environments while still recording the
    Python tier as a mismatch.
    """
    try:
        raw = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        enum = raw["properties"]["oracle_tier"]["enum"]
        if isinstance(enum, list):
            return [str(x) for x in enum]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return ["compile_only", "tests", "ast_conformance", "judge", "daikon"]


def _load_repo_case_type(repo_dir: Path) -> Optional[str]:
    """Return the ``case_type`` recorded in ``python2_meta.json``, or ``None``."""
    meta_path = repo_dir / "python2_meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    case_type = meta.get("case_type")
    return str(case_type) if isinstance(case_type, str) else None


def _check_harness_recipe_for_python(repo_dir: Path) -> list[dict[str, str]]:
    """Inspect the harness Recipe schema's adequacy for a Python repo.

    The :class:`Recipe` dataclass exposes ``dockerfile``, ``build_cmd``,
    ``test_cmd``, ``harness_provenance`` — all build-tool agnostic at the
    field level, but with no ``ecosystem``/``language`` discriminator. The
    upshot: anything written by the Java-targeted synth flow defaults to
    Maven assumptions, and a Python repo cannot self-declare ecosystem.
    """
    from migration_evals.harness.recipe import Recipe

    declared_fields = set(Recipe.__dataclass_fields__.keys())
    if "ecosystem" in declared_fields or "language" in declared_fields:
        return []
    return [
        {
            "module": "harness",
            "issue": "missing_ecosystem_discriminator",
            "field": "Recipe.ecosystem",
            "reason": (
                "Recipe schema has no ecosystem/language field. Defaults bake "
                "in Maven assumptions; a Python 2→3 harness cannot self-declare. "
                f"Observed Recipe fields: {sorted(declared_fields)}."
            ),
        }
    ]


def _check_synthetic_primitives_for_python(repo_dir: Path) -> list[dict[str, str]]:
    """Verify the repo's case_type is representable in the Java primitive set."""
    case_type = _load_repo_case_type(repo_dir)
    if case_type is None:
        # No marker — record as a mismatch because the Java synthetic schema
        # has no equivalent of ``python2_meta.json`` either.
        return [
            {
                "module": "synthetic",
                "issue": "missing_python_meta",
                "field": "python2_meta.json",
                "reason": (
                    "Repo carries no python2_meta.json; the Java synthetic "
                    "schema (emission.json) has no analogue for Python case "
                    "types."
                ),
            }
        ]
    if case_type in java8_generator.GENERATOR_PRIMITIVES:
        return []
    return [
        {
            "module": "synthetic",
            "issue": "case_type_not_in_generator_primitives",
            "field": "GENERATOR_PRIMITIVES",
            "reason": (
                f"case_type={case_type!r} is not a member of the Java "
                f"GENERATOR_PRIMITIVES set "
                f"({sorted(java8_generator.GENERATOR_PRIMITIVES)}). "
                "The Java synthetic schema does not generalize to Python."
            ),
        }
    ]


def _check_ledger_for_python_tier() -> list[dict[str, str]]:
    """Validate that the result schema's oracle_tier enum permits the Python tier."""
    enum = _load_oracle_tier_enum()
    if PYTHON_2TO3_RUNTIME_TIER in enum:
        return []
    return [
        {
            "module": "ledger",
            "issue": "oracle_tier_enum_lacks_python_runtime",
            "field": "oracle_tier",
            "reason": (
                f"mig_result.schema.json:oracle_tier enum={enum} does not "
                f"include {PYTHON_2TO3_RUNTIME_TIER!r}. Python 2→3 runtime "
                "semantic checks have no representable tier in the ledger."
            ),
        }
    ]


def _classify_primitive_coverage(repo_dirs: list[Path]) -> dict[str, int]:
    """Return ``{case_type: count}`` for the supplied repo set.

    Repos without a ``python2_meta.json`` are bucketed under ``"unknown"``.
    """
    counts: dict[str, int] = {}
    for repo_dir in repo_dirs:
        case_type = _load_repo_case_type(repo_dir) or "unknown"
        counts[case_type] = counts.get(case_type, 0) + 1
    return counts


def run(
    count: int = 20,
    out_dir: Path | str = "runs/analysis/python23_probe",
    fixture_repo_root: Path | str | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    """Run the falsification probe and write ``out_dir/findings.json``.

    Parameters
    ----------
    count
        Number of synthetic Python 2 repos to generate. Ignored if
        ``fixture_repo_root`` is provided.
    out_dir
        Directory to write ``findings.json`` (and, when generating, the
        synthetic repo tree under ``out_dir/repos/``).
    fixture_repo_root
        Optional path to a pre-existing tree of Python 2 fixture repos. When
        supplied, generation is skipped and these repos are probed directly.
    seed
        Top-level RNG seed when generating synthetic repos.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if fixture_repo_root is not None:
        repos_root = Path(fixture_repo_root)
        repo_dirs = sorted(p for p in repos_root.iterdir() if p.is_dir())
    else:
        repos_root = out_dir / "repos"
        repo_dirs = python2_generator.generate(repos_root, count, seed)
        repo_dirs = sorted(repo_dirs)

    mismatches_by_module: dict[str, list[dict[str, str]]] = {
        module: [] for module in MODULES
    }

    # Harness + synthetic checks are per-repo. We dedupe identical entries
    # so the findings file does not balloon — the probe records the schema
    # gap once per (module, issue, field) tuple regardless of how many
    # repos trip it.
    seen_keys: dict[str, set[tuple[str, str, str]]] = {m: set() for m in MODULES}

    def _record(module: str, entries: list[dict[str, str]]) -> None:
        for entry in entries:
            key = (entry.get("issue", ""), entry.get("field", ""), entry.get("reason", ""))
            if key in seen_keys[module]:
                continue
            seen_keys[module].add(key)
            mismatches_by_module[module].append(entry)

    for repo_dir in repo_dirs:
        _record("harness", _check_harness_recipe_for_python(repo_dir))
        _record("synthetic", _check_synthetic_primitives_for_python(repo_dir))

    # Ledger check is global (about schema enum), not per-repo.
    _record("ledger", _check_ledger_for_python_tier())

    primitive_coverage = _classify_primitive_coverage(repo_dirs)
    schema_revision_required = compute_schema_revision_required(mismatches_by_module)

    findings: dict[str, Any] = {
        "schema_revision_required": schema_revision_required,
        "n_repos": len(repo_dirs),
        "primitive_coverage": primitive_coverage,
        "mismatches_by_module": mismatches_by_module,
        "modules_with_mismatches": sorted(
            m for m in MODULES if mismatches_by_module[m]
        ),
        "intent": (
            "Falsification probe (PRD M9). NOT a credible Python eval number. "
            "Surfaces schema inadequacies in M2/M3/M5 interfaces."
        ),
    }

    findings_path = out_dir / "findings.json"
    findings_path.write_text(
        json.dumps(findings, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return findings


__all__ = [
    "MODULES",
    "PYTHON_2TO3_RUNTIME_TIER",
    "compute_schema_revision_required",
    "run",
]
