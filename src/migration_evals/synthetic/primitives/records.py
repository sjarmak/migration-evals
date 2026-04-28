"""Record primitive.

Pre-migration: POJO with private final fields, constructor, and getters.
Post-migration: ``record X(...) {}``.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

NAME = "records"

_SHAPES = (
    (("x", "int"), ("y", "int")),
    (("firstName", "String"), ("lastName", "String")),
    (("id", "long"), ("label", "String"), ("active", "boolean")),
    (("amount", "double"), ("currency", "String")),
)


def generate(rng: random.Random, out_dir: Path) -> dict[str, Any]:
    suffix = rng.randint(1000, 9999)
    class_name = f"Record{suffix}"
    fields = rng.choice(_SHAPES)

    ctor_params = ", ".join(f"{t} {n}" for n, t in fields)
    field_decls = "\n".join(f"    private final {t} {n};" for n, t in fields)
    ctor_assigns = "\n".join(f"        this.{n} = {n};" for n, _t in fields)
    getters = "\n\n".join(
        (f"    public {t} get{n[0].upper()}{n[1:]}() {{\n" f"        return this.{n};\n" "    }")
        for n, t in fields
    )

    java_path = out_dir / "src" / "main" / "java" / "com" / "example" / f"{class_name}.java"
    java_path.parent.mkdir(parents=True, exist_ok=True)

    source = (
        "package com.example;\n"
        "\n"
        f"public final class {class_name} {{\n"
        f"{field_decls}\n"
        "\n"
        f"    public {class_name}({ctor_params}) {{\n"
        f"{ctor_assigns}\n"
        "    }\n"
        "\n"
        f"{getters}\n"
        "}\n"
    )
    java_path.write_text(source, encoding="utf-8")

    return {
        "primitive": NAME,
        "class_name": class_name,
        "file": str(java_path.relative_to(out_dir)),
        "fields": [list(f) for f in fields],
    }
