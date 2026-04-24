"""Deprecated-API-swap primitive.

Pre-migration: calls into Java APIs deprecated between 8 and 17
(``new Integer(int)``, ``new Date(int, int, int)``, ``Thread.stop()``).
Post-migration: modern equivalents (``Integer.valueOf``, ``LocalDate.of``,
``Thread.interrupt``).

NOT checked by the oracle (per D5 disjoint constraint).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

NAME = "deprecated_api"

_CALLS = (
    ("integer_ctor", "Integer boxed = new Integer({arg});"),
    ("date_ctor", "java.util.Date d = new java.util.Date({year}, {month}, {day});"),
    ("thread_stop", "t.stop();"),
)


def generate(rng: random.Random, out_dir: Path) -> dict[str, Any]:
    suffix = rng.randint(1000, 9999)
    class_name = f"DeprecatedDemo{suffix}"
    picks = rng.sample(_CALLS, 2)
    lines: list[str] = []
    for kind, tpl in picks:
        if kind == "integer_ctor":
            lines.append("        " + tpl.format(arg=rng.randint(1, 99)))
        elif kind == "date_ctor":
            lines.append(
                "        "
                + tpl.format(year=rng.randint(70, 120), month=rng.randint(0, 11), day=rng.randint(1, 28))
            )
        else:
            lines.append("        Thread t = new Thread(() -> {});")
            lines.append("        " + tpl)

    java_path = out_dir / "src" / "main" / "java" / "com" / "example" / f"{class_name}.java"
    java_path.parent.mkdir(parents=True, exist_ok=True)

    body = "\n".join(lines)
    source = (
        "package com.example;\n"
        "\n"
        f"public class {class_name} {{\n"
        "    @SuppressWarnings(\"deprecation\")\n"
        "    public void run() {\n"
        f"{body}\n"
        "    }\n"
        "}\n"
    )
    java_path.write_text(source, encoding="utf-8")

    return {
        "primitive": NAME,
        "class_name": class_name,
        "file": str(java_path.relative_to(out_dir)),
        "calls": [kind for kind, _ in picks],
    }
