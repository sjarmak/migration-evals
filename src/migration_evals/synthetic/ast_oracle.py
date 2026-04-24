"""AST-spec conformance oracle for synthetic Java migration repos.

This oracle is intentionally lightweight: it does not call a real Java parser.
It compares the pre- and post-migration source trees with regex-level
detectors for a *subset* of the migration primitives emitted by the
generator. That subset is declared by ``ORACLE_CHECKED_PRIMITIVES``.

PRD D5 — disjoint recipe sets
-----------------------------

The generator (``java8_generator.GENERATOR_PRIMITIVES``) covers ten
primitives. The oracle's check-set is a documented subset of at most half of
that set. This anti-tautology rule comes from PRD D5 ("M3 generator and
AST-conformance authored from disjoint recipe sets, intersection ≤ 50% of
primitives"). If you widen this set past 5/10 you must rebalance the
generator side to keep the ratio ≤ 0.5.

CLI:
    python -m migration_evals.synthetic.ast_oracle \\
        --orig ORIG_DIR --migrated MIGRATED_DIR

Output JSON shape::

    {
      "overall": "pass" | "fail" | "skip",
      "primitives": {
        "lambda": {"status": "pass" | "fail" | "skip", "detail": "..."},
        ...
      },
      "orig": "...",
      "migrated": "...",
      "elapsed_seconds": 0.02
    }
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Callable

# PRD D5: this set MUST remain a strict subset (≤ 50%) of the generator's
# GENERATOR_PRIMITIVES. Do not expand without rebalancing the generator or
# updating `test_oracle_is_disjoint_from_generator_per_d5`.
ORACLE_CHECKED_PRIMITIVES: set[str] = {
    "lambda",
    "var_infer",
    "optional",
    "text_blocks",
    "records",
}

# Regex fragments. Kept deliberately conservative so synthetic fixtures are
# detected crisply without false positives on unrelated Java code.
_RE_ANON_CLASS = re.compile(r"new\s+\w+\s*\(\s*\)\s*\{\s*\n\s*@?Override", re.MULTILINE)
_RE_LAMBDA = re.compile(r"->\s*\{|->[^=]")
_RE_EXPLICIT_DECL = re.compile(
    r"\b(ArrayList|HashMap|LinkedList|HashSet)<[^>]*>\s+\w+\s*=\s*new\s+\1<",
)
_RE_VAR_DECL = re.compile(r"\bvar\s+\w+\s*=\s*new\s+\w+")
_RE_NULL_GUARD = re.compile(r"if\s*\(\s*\w+\s*!=\s*null\s*\)")
_RE_OPTIONAL_USE = re.compile(r"Optional\s*\.\s*(ofNullable|of)\s*\(")
_RE_CONCAT_NEWLINE = re.compile(r"\"[^\"]*\\n\"\s*\+")
_RE_TEXT_BLOCK = re.compile(r"\"\"\"")
_RE_POJO_FIELDS = re.compile(r"private\s+final\s+\w[\w<>,\s]*\s+\w+\s*;")
_RE_RECORD_DECL = re.compile(r"\brecord\s+\w+\s*\(")


def _read_java_files(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    out: dict[str, str] = {}
    for path in root.rglob("*.java"):
        rel = str(path.relative_to(root))
        out[rel] = path.read_text(encoding="utf-8", errors="replace")
    return out


def _combine(texts: dict[str, str]) -> str:
    return "\n".join(texts.values())


def _detect_lambda(orig: str, migrated: str) -> tuple[str, str]:
    if not _RE_ANON_CLASS.search(orig):
        return "skip", "no anonymous-class pattern in orig"
    if _RE_ANON_CLASS.search(migrated):
        return "fail", "anonymous class still present in migrated"
    if _RE_LAMBDA.search(migrated):
        return "pass", "lambda arrow detected in migrated"
    return "fail", "no lambda arrow in migrated"


def _detect_var_infer(orig: str, migrated: str) -> tuple[str, str]:
    if not _RE_EXPLICIT_DECL.search(orig):
        return "skip", "no explicit collection decl in orig"
    if _RE_EXPLICIT_DECL.search(migrated):
        return "fail", "explicit collection decl still present"
    if _RE_VAR_DECL.search(migrated):
        return "pass", "var declaration detected"
    return "fail", "no var declaration found"


def _detect_optional(orig: str, migrated: str) -> tuple[str, str]:
    if not _RE_NULL_GUARD.search(orig):
        return "skip", "no null guard in orig"
    if _RE_OPTIONAL_USE.search(migrated):
        return "pass", "Optional usage detected"
    if _RE_NULL_GUARD.search(migrated):
        return "fail", "null guard still present, no Optional"
    return "fail", "neither Optional nor null-guard found in migrated"


def _detect_text_blocks(orig: str, migrated: str) -> tuple[str, str]:
    if not _RE_CONCAT_NEWLINE.search(orig):
        return "skip", "no concatenated newline pattern in orig"
    if _RE_TEXT_BLOCK.search(migrated):
        return "pass", "text block detected"
    return "fail", "no text block in migrated"


def _detect_records(orig: str, migrated: str) -> tuple[str, str]:
    if not _RE_POJO_FIELDS.search(orig):
        return "skip", "no POJO private-final fields in orig"
    if _RE_RECORD_DECL.search(migrated):
        return "pass", "record declaration detected"
    return "fail", "no record declaration in migrated"


_DETECTORS: dict[str, Callable[[str, str], tuple[str, str]]] = {
    "lambda": _detect_lambda,
    "var_infer": _detect_var_infer,
    "optional": _detect_optional,
    "text_blocks": _detect_text_blocks,
    "records": _detect_records,
}


def check(orig_dir: Path, migrated_dir: Path) -> dict:
    start = time.perf_counter()
    orig_texts = _combine(_read_java_files(orig_dir))
    migrated_texts = _combine(_read_java_files(migrated_dir))

    per_primitive: dict[str, dict[str, str]] = {}
    statuses: list[str] = []
    for name in sorted(ORACLE_CHECKED_PRIMITIVES):
        detector = _DETECTORS[name]
        status, detail = detector(orig_texts, migrated_texts)
        per_primitive[name] = {"status": status, "detail": detail}
        statuses.append(status)

    if all(s == "skip" for s in statuses):
        overall = "skip"
    elif any(s == "fail" for s in statuses):
        overall = "fail"
    else:
        overall = "pass"

    return {
        "overall": overall,
        "primitives": per_primitive,
        "orig": str(orig_dir),
        "migrated": str(migrated_dir),
        "elapsed_seconds": round(time.perf_counter() - start, 6),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AST-spec conformance oracle.")
    parser.add_argument("--orig", required=True, type=Path)
    parser.add_argument("--migrated", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    result = check(args.orig, args.migrated)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["overall"] != "fail" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
