"""Lambda-rewrite primitive.

Pre-migration shape: anonymous inner class with a single abstract method.
Post-migration shape: lambda expression ``() -> { ... }``.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

NAME = "lambda"

_FUNCTIONAL_INTERFACES = (
    ("Runnable", "void run()", ""),
    ("java.util.concurrent.Callable<String>", "String call() throws Exception", "return \"{literal}\";"),
    ("java.util.function.Supplier<Integer>", "Integer get()", "return {literal_int};"),
)


def generate(rng: random.Random, out_dir: Path) -> dict[str, Any]:
    suffix = rng.randint(1000, 9999)
    class_name = f"LambdaDemo{suffix}"
    iface_fqn, signature, body = rng.choice(_FUNCTIONAL_INTERFACES)
    iface_simple = iface_fqn.split(".")[-1].split("<")[0]
    literal = f"msg-{rng.randint(0, 999)}"
    literal_int = rng.randint(1, 100)
    body_rendered = body.format(literal=literal, literal_int=literal_int)

    java_path = out_dir / "src" / "main" / "java" / "com" / "example" / f"{class_name}.java"
    java_path.parent.mkdir(parents=True, exist_ok=True)

    source = (
        "package com.example;\n"
        f"import {iface_fqn.split('<')[0]};\n"
        "\n"
        f"public class {class_name} {{\n"
        f"    public {iface_simple} factory() {{\n"
        f"        return new {iface_simple}() {{\n"
        f"            @Override\n"
        f"            public {signature} {{\n"
        f"                {body_rendered}\n"
        f"            }}\n"
        f"        }};\n"
        "    }\n"
        "}\n"
    )
    java_path.write_text(source, encoding="utf-8")

    return {
        "primitive": NAME,
        "class_name": class_name,
        "file": str(java_path.relative_to(out_dir)),
        "interface": iface_simple,
    }
