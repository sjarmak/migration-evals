"""Optional-chain primitive.

Pre-migration: null-guarded field access.
Post-migration: ``Optional.ofNullable(x).map(...).ifPresent(...)``.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

NAME = "optional"


def generate(rng: random.Random, out_dir: Path) -> dict[str, Any]:
    suffix = rng.randint(1000, 9999)
    class_name = f"OptionalDemo{suffix}"
    holder = f"user{rng.randint(0, 99)}"
    action = rng.choice(("print", "emit", "log", "publish"))

    java_path = out_dir / "src" / "main" / "java" / "com" / "example" / f"{class_name}.java"
    java_path.parent.mkdir(parents=True, exist_ok=True)

    source = (
        "package com.example;\n"
        "\n"
        f"public class {class_name} {{\n"
        "    public static class Holder {\n"
        "        public String name;\n"
        "        public Holder(String n) { this.name = n; }\n"
        "    }\n"
        "\n"
        f"    public void handle(Holder {holder}) {{\n"
        f"        if ({holder} != null) {{\n"
        f"            String n = {holder}.name;\n"
        f"            if (n != null) {{\n"
        f"                {action}(n);\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "\n"
        f"    private void {action}(String value) {{\n"
        "        System.out.println(value);\n"
        "    }\n"
        "}\n"
    )
    java_path.write_text(source, encoding="utf-8")

    return {
        "primitive": NAME,
        "class_name": class_name,
        "file": str(java_path.relative_to(out_dir)),
        "holder": holder,
        "action": action,
    }
