"""Local-variable type inference (``var``) primitive.

Pre-migration: explicit collection declaration with both-sides type repetition.
Post-migration: ``var xs = new ArrayList<String>();``.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

NAME = "var_infer"

_COLLECTIONS = (
    ("java.util.ArrayList", "ArrayList", "String"),
    ("java.util.HashMap", "HashMap", "String, Integer"),
    ("java.util.LinkedList", "LinkedList", "Long"),
    ("java.util.HashSet", "HashSet", "Integer"),
)


def generate(rng: random.Random, out_dir: Path) -> dict[str, Any]:
    suffix = rng.randint(1000, 9999)
    class_name = f"VarInferDemo{suffix}"
    fqn, simple, type_args = rng.choice(_COLLECTIONS)
    variable = f"items{rng.randint(0, 99)}"

    java_path = out_dir / "src" / "main" / "java" / "com" / "example" / f"{class_name}.java"
    java_path.parent.mkdir(parents=True, exist_ok=True)

    source = (
        "package com.example;\n"
        f"import {fqn};\n"
        "\n"
        f"public class {class_name} {{\n"
        f"    public {simple}<{type_args}> build() {{\n"
        f"        {simple}<{type_args}> {variable} = new {simple}<{type_args}>();\n"
        f"        return {variable};\n"
        "    }\n"
        "}\n"
    )
    java_path.write_text(source, encoding="utf-8")

    return {
        "primitive": NAME,
        "class_name": class_name,
        "file": str(java_path.relative_to(out_dir)),
        "variable": variable,
        "collection": simple,
    }
