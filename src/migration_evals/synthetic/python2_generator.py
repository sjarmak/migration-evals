"""Thin Python 2→3 synthetic-repo generator (PRD M9 falsification probe).

This generator is **deliberately minimal**. It exists solely to feed the
``python23_probe`` so we can stress-test the M2/M3/M5 interfaces — the output
is NOT a credible Python eval corpus.

Coverage: each emitted repo carries exactly one Python-idiosyncratic case
drawn from :data:`PYTHON2_CASE_TYPES`:

- ``str_bytes``     — Python 2 ``str``-is-``bytes``; py3 requires explicit
                      bytes/str disambiguation.
- ``setup_py_div``  — Python 2 packaging via ``setup.py``; py3 ecosystem
                      prefers ``pyproject.toml``.
- ``two_to_three``  — runtime semantic shifts that 2to3 catches imperfectly:
                      ``5 / 2``, ``map()``, ``dict.items()``.

Determinism: a top-level seed produces a stable per-repo child seed, and
case-type selection round-robins across :data:`PYTHON2_CASE_TYPES` so the
first ``len(PYTHON2_CASE_TYPES)`` repos are guaranteed to cover every case.

CLI::

    python -m migration_evals.synthetic.python2_generator \\
        --out /tmp/py2gen --count 5 --seed 42
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

# Python-idiosyncratic case identifiers. These are intentionally NOT members
# of ``java8_generator.GENERATOR_PRIMITIVES`` — that mismatch is the falsification
# finding the probe is designed to surface.
PYTHON2_CASE_TYPES: tuple[str, ...] = (
    "str_bytes",
    "setup_py_div",
    "two_to_three",
)


def _child_seed(seed: int, index: int) -> int:
    return seed * 1_000_003 + index


def _select_case_type(rng: random.Random, index: int) -> str:
    """Round-robin for the first len(PYTHON2_CASE_TYPES) indices, then RNG.

    Guarantees that ``count >= len(PYTHON2_CASE_TYPES)`` always covers every
    case type — required by the probe's coverage classifier and by the test
    that asserts ≥3 distinct case-type repos.
    """
    if index < len(PYTHON2_CASE_TYPES):
        return PYTHON2_CASE_TYPES[index]
    return rng.choice(PYTHON2_CASE_TYPES)


def _emit_setup_py(repo_dir: Path, name: str, modules: list[str]) -> None:
    body = (
        "from distutils.core import setup\n"
        "\n"
        "setup(\n"
        f"    name=\"{name}\",\n"
        "    version=\"0.1.0\",\n"
        "    description=\"Synthetic Python 2 fixture for the M9 falsification probe.\",\n"
        f"    py_modules={modules!r},\n"
        ")\n"
    )
    (repo_dir / "setup.py").write_text(body, encoding="utf-8")


def _emit_str_bytes_case(rng: random.Random, repo_dir: Path) -> dict[str, Any]:
    suffix = rng.randint(1000, 9999)
    name = f"py2_str_bytes_{suffix}"
    _emit_setup_py(repo_dir, name, ["app"])
    src = (
        "# Python 2 str-is-bytes; relies on implicit ASCII coerce.\n"
        "\n"
        "def emit_payload():\n"
        "    payload = \"foo\".encode()\n"
        "    return payload + b\"-bar\"\n"
        "\n"
        "def join_with(label):\n"
        "    # Py2: silent ASCII coerce; py3: TypeError without explicit decode.\n"
        "    return label + emit_payload().decode(\"ascii\")\n"
    )
    (repo_dir / "app.py").write_text(src, encoding="utf-8")
    return {"case_type": "str_bytes", "files": ["setup.py", "app.py"], "name": name}


def _emit_setup_py_div_case(rng: random.Random, repo_dir: Path) -> dict[str, Any]:
    suffix = rng.randint(1000, 9999)
    name = f"py2_setup_py_div_{suffix}"
    _emit_setup_py(repo_dir, name, ["legacy_module"])
    src = (
        "\"\"\"Legacy Python 2 module. Migration includes packaging shift.\"\"\"\n"
        "\n"
        "def greet(person):\n"
        "    return \"hello \" + person\n"
    )
    (repo_dir / "legacy_module.py").write_text(src, encoding="utf-8")
    # Deliberately NO pyproject.toml — that absence IS the case.
    return {
        "case_type": "setup_py_div",
        "files": ["setup.py", "legacy_module.py"],
        "name": name,
    }


def _emit_two_to_three_case(rng: random.Random, repo_dir: Path) -> dict[str, Any]:
    suffix = rng.randint(1000, 9999)
    name = f"py2_two_to_three_{suffix}"
    _emit_setup_py(repo_dir, name, ["runtime_div"])
    src = (
        "# Py2 vs py3 semantic shifts: division, map(), dict.items().\n"
        "\n"
        "def half(n):\n"
        "    # Without `from __future__ import division`, py2 returns floor.\n"
        "    return n / 2\n"
        "\n"
        "def items_then_consume(d):\n"
        "    # Py2: dict.items() returns a list; py3 returns a view.\n"
        "    pairs = d.items()\n"
        "    return list(pairs)\n"
        "\n"
        "def doubled(xs):\n"
        "    # Py2: map() returns a list; py3 returns an iterator.\n"
        "    return map(lambda x: x * 2, xs)\n"
    )
    (repo_dir / "runtime_div.py").write_text(src, encoding="utf-8")
    return {
        "case_type": "two_to_three",
        "files": ["setup.py", "runtime_div.py"],
        "name": name,
    }


_EMITTERS = {
    "str_bytes": _emit_str_bytes_case,
    "setup_py_div": _emit_setup_py_div_case,
    "two_to_three": _emit_two_to_three_case,
}


def generate_repo(seed: int, index: int, out_root: Path) -> dict[str, Any]:
    """Generate a single repo at ``out_root/repo_<index>``.

    Returns the emission manifest dict (also written as ``python2_meta.json``).
    """
    child_seed = _child_seed(seed, index)
    rng = random.Random(child_seed)

    repo_dir = out_root / f"repo_{index:04d}"
    repo_dir.mkdir(parents=True, exist_ok=True)

    case_type = _select_case_type(rng, index)
    emitter = _EMITTERS[case_type]
    emission = emitter(rng, repo_dir)

    manifest: dict[str, Any] = {
        "case_type": case_type,
        "repo_index": index,
        "seed": seed,
        "child_seed": child_seed,
        "emission": emission,
        "note": (
            "Synthetic Python 2 fixture for the M9 falsification probe. "
            "NOT a credible Python eval corpus."
        ),
    }
    (repo_dir / "python2_meta.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
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
        description="Generate synthetic Python 2 repos for the M9 falsification probe.",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output directory")
    parser.add_argument("--count", type=int, default=20, help="Number of repos to emit")
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
                "case_types": list(PYTHON2_CASE_TYPES),
            }
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
