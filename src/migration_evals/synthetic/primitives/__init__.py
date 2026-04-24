"""Primitive generator modules for the Java 8 synthetic-repo pipeline.

Each submodule emits a pre-migration pattern (Java 8 compatible source) that a
migration agent is expected to modernize. Modules expose:

- ``NAME`` (str): stable primitive identifier used by the AST oracle and
  generator-level bookkeeping.
- ``generate(rng: random.Random, out_dir: pathlib.Path) -> dict``: writes files
  under ``out_dir`` and returns an emission descriptor consumed by the oracle.

The modules are intentionally small and independently importable.
"""

from __future__ import annotations

from . import (
    dep_bumps,
    deprecated_api,
    enhanced_switch,
    lambda_,
    optional_,
    pattern_match,
    records,
    sealed,
    text_blocks,
    var_infer,
)

ALL_MODULES = (
    lambda_,
    var_infer,
    optional_,
    text_blocks,
    records,
    sealed,
    pattern_match,
    enhanced_switch,
    deprecated_api,
    dep_bumps,
)

PRIMITIVE_NAMES: frozenset[str] = frozenset(m.NAME for m in ALL_MODULES)

__all__ = [
    "ALL_MODULES",
    "PRIMITIVE_NAMES",
    "dep_bumps",
    "deprecated_api",
    "enhanced_switch",
    "lambda_",
    "optional_",
    "pattern_match",
    "records",
    "sealed",
    "text_blocks",
    "var_infer",
]
