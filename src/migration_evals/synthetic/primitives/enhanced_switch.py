"""Enhanced-switch primitive.

Pre-migration: classic switch statement with ``case X: ... break;`` per arm.
Post-migration: switch expression with arrow arms ``case X -> ...;``.

NOT checked by the oracle (per D5 disjoint constraint).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

NAME = "enhanced_switch"

_ENUM_SETS = (
    ("Day", ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY")),
    ("Color", ("RED", "GREEN", "BLUE")),
    ("Tier", ("BASIC", "PRO", "ENTERPRISE")),
)


def generate(rng: random.Random, out_dir: Path) -> dict[str, Any]:
    suffix = rng.randint(1000, 9999)
    enum_simple, cases = rng.choice(_ENUM_SETS)
    enum_name = f"{enum_simple}{suffix}"
    class_name = f"SwitchDemo{suffix}"

    java_dir = out_dir / "src" / "main" / "java" / "com" / "example"
    java_dir.mkdir(parents=True, exist_ok=True)

    enum_source = (
        "package com.example;\n"
        "\n"
        f"public enum {enum_name} {{\n"
        f"    {', '.join(cases)};\n"
        "}\n"
    )
    (java_dir / f"{enum_name}.java").write_text(enum_source, encoding="utf-8")

    case_bodies = "\n".join(
        f'            case {c}:\n                return "{c.lower()}";' for c in cases
    )
    class_source = (
        "package com.example;\n"
        "\n"
        f"public class {class_name} {{\n"
        f"    public String describe({enum_name} value) {{\n"
        "        switch (value) {\n"
        f"{case_bodies}\n"
        "            default:\n"
        '                return "unknown";\n'
        "        }\n"
        "    }\n"
        "}\n"
    )
    (java_dir / f"{class_name}.java").write_text(class_source, encoding="utf-8")

    return {
        "primitive": NAME,
        "enum": enum_name,
        "class_name": class_name,
        "cases": list(cases),
    }
