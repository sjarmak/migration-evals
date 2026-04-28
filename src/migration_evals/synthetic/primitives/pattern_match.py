"""Pattern-matching-for-instanceof primitive.

Pre-migration: ``if (o instanceof String) { String s = (String) o; ... }``.
Post-migration: ``if (o instanceof String s) { ... }``.

NOT checked by the oracle (per D5 disjoint constraint).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

NAME = "pattern_match"

_TYPES = ("String", "Integer", "Long", "java.util.List")


def generate(rng: random.Random, out_dir: Path) -> dict[str, Any]:
    suffix = rng.randint(1000, 9999)
    class_name = f"PatternMatchDemo{suffix}"
    target_fqn = rng.choice(_TYPES)
    target = target_fqn.rsplit(".", 1)[-1]
    varname = f"p{rng.randint(0, 99)}"

    java_path = out_dir / "src" / "main" / "java" / "com" / "example" / f"{class_name}.java"
    java_path.parent.mkdir(parents=True, exist_ok=True)

    import_line = "" if "." not in target_fqn else f"import {target_fqn};\n"

    source = (
        "package com.example;\n"
        f"{import_line}"
        "\n"
        f"public class {class_name} {{\n"
        "    public String describe(Object o) {\n"
        f"        if (o instanceof {target}) {{\n"
        f"            {target} {varname} = ({target}) o;\n"
        f"            return {varname}.toString();\n"
        "        }\n"
        '        return "other";\n'
        "    }\n"
        "}\n"
    )
    java_path.write_text(source, encoding="utf-8")

    return {
        "primitive": NAME,
        "class_name": class_name,
        "file": str(java_path.relative_to(out_dir)),
        "target": target,
    }
