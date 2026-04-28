"""Sealed-hierarchy primitive.

Pre-migration: ``abstract class Base`` with a closed set of subclasses.
Post-migration: ``sealed class Base permits Sub1, Sub2 {}``.

NOT checked by the oracle (per D5 disjoint constraint).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

NAME = "sealed"

_HIERARCHIES = (
    ("Shape", ("Circle", "Square", "Triangle")),
    ("Event", ("Created", "Updated", "Deleted")),
    ("Payment", ("Card", "Wire")),
)


def generate(rng: random.Random, out_dir: Path) -> dict[str, Any]:
    suffix = rng.randint(1000, 9999)
    base, subs = rng.choice(_HIERARCHIES)
    base_name = f"{base}{suffix}"
    sub_names = [f"{s}{suffix}" for s in subs]

    java_dir = out_dir / "src" / "main" / "java" / "com" / "example"
    java_dir.mkdir(parents=True, exist_ok=True)

    base_source = (
        "package com.example;\n"
        "\n"
        f"public abstract class {base_name} {{\n"
        "    public abstract String describe();\n"
        "}\n"
    )
    (java_dir / f"{base_name}.java").write_text(base_source, encoding="utf-8")

    for sub in sub_names:
        sub_source = (
            "package com.example;\n"
            "\n"
            f"public final class {sub} extends {base_name} {{\n"
            "    @Override\n"
            "    public String describe() {\n"
            f'        return "{sub}";\n'
            "    }\n"
            "}\n"
        )
        (java_dir / f"{sub}.java").write_text(sub_source, encoding="utf-8")

    return {
        "primitive": NAME,
        "base": base_name,
        "subclasses": sub_names,
    }
