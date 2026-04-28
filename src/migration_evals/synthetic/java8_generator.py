"""Procedural Java 8 synthetic-repo generator.

Produces deterministic Java 8 projects under an output directory. Each repo
contains a ``pom.xml`` (maven, java 1.8 source/target) and ``src/main/java``
files emitted by the primitive modules under :mod:`.primitives`.

CLI:
    python -m migration_evals.synthetic.java8_generator \\
        --out /tmp/gen --count 10 --seed 42

Design notes:

- ``GENERATOR_PRIMITIVES`` is the full set of primitives exercised by the
  generator. The AST oracle deliberately covers a subset - see ``ast_oracle``
  and PRD D5.
- Determinism: a top-level seed produces a stable sequence of per-repo child
  seeds. Primitive selection uses ``rng.sample`` over the sorted primitive list
  so iteration order does not leak into output content.
- No runtime dependencies beyond the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

try:
    from .primitives import ALL_MODULES
except ImportError:  # pragma: no cover - script-style invocation
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent.parent))
    from migration_evals.synthetic.primitives import ALL_MODULES  # type: ignore[no-redef]

GENERATOR_PRIMITIVES: set[str] = {m.NAME for m in ALL_MODULES}

_MODULES_BY_NAME = {m.NAME: m for m in ALL_MODULES}


def _pom_xml(artifact_id: str, extra_deps_xml: str = "") -> str:
    """Emit a minimal Maven pom.xml targeting Java 1.8."""
    deps_block = (
        "    <dependencies>\n" + extra_deps_xml + "\n    </dependencies>\n"
        if extra_deps_xml.strip()
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        "    <modelVersion>4.0.0</modelVersion>\n"
        "    <groupId>com.example</groupId>\n"
        f"    <artifactId>{artifact_id}</artifactId>\n"
        "    <version>0.1.0-SNAPSHOT</version>\n"
        "    <packaging>jar</packaging>\n"
        "    <properties>\n"
        "        <maven.compiler.source>1.8</maven.compiler.source>\n"
        "        <maven.compiler.target>1.8</maven.compiler.target>\n"
        "        <java.version>1.8</java.version>\n"
        "        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>\n"
        "    </properties>\n"
        f"{deps_block}"
        "</project>\n"
    )


def _child_seed(seed: int, index: int) -> int:
    # Large prime stride keeps neighbour repos decorrelated.
    return seed * 1_000_003 + index


def _pick_primitives(rng: random.Random) -> list[str]:
    # Sort first for iteration-order stability; then deterministic sample.
    available = sorted(GENERATOR_PRIMITIVES)
    k = rng.randint(3, 7)
    return sorted(rng.sample(available, k))


def generate_repo(seed: int, index: int, out_root: Path) -> dict[str, Any]:
    """Generate a single repo at ``out_root/repo_<index>``.

    Returns the emission manifest dict (also written as ``emission.json``).
    """
    child_seed = _child_seed(seed, index)
    rng = random.Random(child_seed)

    repo_dir = out_root / f"repo_{index:04d}"
    repo_dir.mkdir(parents=True, exist_ok=True)

    chosen = _pick_primitives(rng)
    emissions: list[dict[str, Any]] = []
    extra_deps_xml = ""

    for name in chosen:
        module = _MODULES_BY_NAME[name]
        emission = module.generate(rng, repo_dir)
        emissions.append(emission)
        if name == "dep_bumps":
            snippet_path = repo_dir / emission["snippet"]
            extra_deps_xml = snippet_path.read_text(encoding="utf-8")

    artifact_id = f"synthetic-repo-{index:04d}"
    (repo_dir / "pom.xml").write_text(_pom_xml(artifact_id, extra_deps_xml), encoding="utf-8")

    manifest: dict[str, Any] = {
        "repo_index": index,
        "seed": seed,
        "child_seed": child_seed,
        "primitives": chosen,
        "emissions": emissions,
    }
    (repo_dir / "emission.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def generate(out: Path, count: int, seed: int) -> list[Path]:
    """Generate ``count`` repos under ``out``. Returns the list of repo dirs."""
    out.mkdir(parents=True, exist_ok=True)
    repos: list[Path] = []
    for i in range(count):
        manifest = generate_repo(seed, i, out)
        repos.append(out / f"repo_{manifest['repo_index']:04d}")
    return repos


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate synthetic Java 8 repos for migration evaluation.",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output directory")
    parser.add_argument("--count", type=int, default=10, help="Number of repos to emit")
    parser.add_argument("--seed", type=int, default=42, help="Top-level RNG seed")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    repos = generate(args.out, args.count, args.seed)
    print(
        json.dumps(
            {
                "out": str(args.out),
                "count": len(repos),
                "seed": args.seed,
                "primitives": sorted(GENERATOR_PRIMITIVES),
            }
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
