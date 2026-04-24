"""Dependency-version-bump primitive.

Pre-migration: ``pom.xml`` declares legacy dependency versions (JUnit 4,
old Guava, old Jackson).
Post-migration: bumped to current LTS versions.

This primitive contributes additional ``<dependency>`` entries to the repo's
pom.xml by writing a companion ``dep_bumps.xml`` snippet that the generator
merges. NOT checked by the oracle (per D5 disjoint constraint).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

NAME = "dep_bumps"

_DEPENDENCIES = (
    ("junit", "junit", "4.12"),
    ("com.google.guava", "guava", "20.0"),
    ("com.fasterxml.jackson.core", "jackson-databind", "2.9.8"),
    ("org.apache.commons", "commons-lang3", "3.5"),
    ("org.slf4j", "slf4j-api", "1.7.25"),
)


def generate(rng: random.Random, out_dir: Path) -> dict[str, Any]:
    picks = rng.sample(_DEPENDENCIES, 2)
    entries = []
    rendered = []
    for group, artifact, version in picks:
        entries.append({"group": group, "artifact": artifact, "version": version})
        rendered.append(
            "        <dependency>\n"
            f"            <groupId>{group}</groupId>\n"
            f"            <artifactId>{artifact}</artifactId>\n"
            f"            <version>{version}</version>\n"
            "        </dependency>"
        )

    snippet_path = out_dir / ".synthetic" / "dep_bumps.xml"
    snippet_path.parent.mkdir(parents=True, exist_ok=True)
    snippet_path.write_text("\n".join(rendered) + "\n", encoding="utf-8")

    return {
        "primitive": NAME,
        "dependencies": entries,
        "snippet": str(snippet_path.relative_to(out_dir)),
    }
