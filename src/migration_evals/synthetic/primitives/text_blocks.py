"""Text-block primitive.

Pre-migration: multi-line string built via ``+`` concatenation and ``\\n``.
Post-migration: Java 15 triple-quoted text block.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

NAME = "text_blocks"

_TEMPLATES = (
    ("sql", ["SELECT *", "FROM {table}", "WHERE id = ?"]),
    ("json", ["{{", "  \\\"id\\\": {id},", "  \\\"ok\\\": true", "}}"]),
    ("yaml", ["version: {ver}", "kind: Deployment", "metadata:", "  name: {name}"]),
)


def generate(rng: random.Random, out_dir: Path) -> dict[str, Any]:
    suffix = rng.randint(1000, 9999)
    class_name = f"TextBlockDemo{suffix}"
    kind, lines = rng.choice(_TEMPLATES)
    table = f"t_{rng.randint(100, 999)}"
    id_ = rng.randint(1, 9999)
    ver = rng.choice(("apps/v1", "v1"))
    name = f"svc-{rng.randint(0, 99)}"

    filled = [ln.format(table=table, id=id_, ver=ver, name=name) for ln in lines]
    literal_lines = " +\n            ".join(f"\"{ln}\\n\"" for ln in filled)

    java_path = out_dir / "src" / "main" / "java" / "com" / "example" / f"{class_name}.java"
    java_path.parent.mkdir(parents=True, exist_ok=True)

    source = (
        "package com.example;\n"
        "\n"
        f"public class {class_name} {{\n"
        "    public String render() {\n"
        f"        String {kind} = {literal_lines};\n"
        f"        return {kind};\n"
        "    }\n"
        "}\n"
    )
    java_path.write_text(source, encoding="utf-8")

    return {
        "primitive": NAME,
        "class_name": class_name,
        "file": str(java_path.relative_to(out_dir)),
        "kind": kind,
    }
