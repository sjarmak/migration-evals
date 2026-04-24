"""Tests for the migration-eval foundation scaffold.

Covers acceptance criteria 2, 3, 6, 8 of work unit foundation-module-scaffold:

- Schema is valid draft-07 JSON Schema with the required fields.
- Enum values in `types.py` round-trip through `str`.
- Example fixture validates against the schema.
- Stripping any required field causes validation failure.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import jsonschema
import pytest

REPO_ROOT = _REPO_ROOT
SCHEMA_PATH = REPO_ROOT / "schemas" / "mig_result.schema.json"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "example_result.json"

REQUIRED_FIELDS = [
    "task_id",
    "agent_model",
    "migration_id",
    "success",
    "failure_class",
    "oracle_tier",
    "oracle_spec_sha",
    "recipe_spec_sha",
    "pre_reg_sha",
    "score_pre_cutoff",
    "score_post_cutoff",
]


@pytest.fixture(scope="module")
def schema() -> dict:
    with SCHEMA_PATH.open() as f:
        return json.load(f)


@pytest.fixture(scope="module")
def example() -> dict:
    with FIXTURE_PATH.open() as f:
        return json.load(f)


def test_schema_is_draft_07(schema: dict) -> None:
    assert "draft-07" in schema["$schema"]
    # Validate the schema itself against the draft-07 meta-schema.
    jsonschema.Draft7Validator.check_schema(schema)


def test_schema_declares_required_fields(schema: dict) -> None:
    assert set(schema["required"]) == set(REQUIRED_FIELDS)


def test_schema_failure_class_enum(schema: dict) -> None:
    expected = {"agent_error", "harness_error", "oracle_error", "infra_error", None}
    actual = set(schema["properties"]["failure_class"]["enum"])
    assert actual == expected


def test_schema_oracle_tier_enum(schema: dict) -> None:
    expected = {"compile_only", "tests", "ast_conformance", "judge", "daikon"}
    actual = set(schema["properties"]["oracle_tier"]["enum"])
    assert actual == expected


def test_example_result_validates(schema: dict, example: dict) -> None:
    jsonschema.validate(example, schema)


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_required_field_fails(schema: dict, example: dict, field: str) -> None:
    broken = copy.deepcopy(example)
    broken.pop(field)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken, schema)


def test_failure_class_enum_roundtrip() -> None:
    from migration_evals.types import FailureClass

    expected = {
        "agent_error": FailureClass.AGENT_ERROR,
        "harness_error": FailureClass.HARNESS_ERROR,
        "oracle_error": FailureClass.ORACLE_ERROR,
        "infra_error": FailureClass.INFRA_ERROR,
    }
    for value, member in expected.items():
        assert member.value == value
        assert FailureClass(value) is member
        # str-subclass ergonomics: `.value` and `str(member)` round-trip cleanly.
        assert isinstance(member.value, str)


def test_oracle_tier_enum_roundtrip() -> None:
    from migration_evals.types import OracleTier

    expected = {
        "compile_only": OracleTier.COMPILE_ONLY,
        "tests": OracleTier.TESTS,
        "ast_conformance": OracleTier.AST_CONFORMANCE,
        "judge": OracleTier.JUDGE,
        "daikon": OracleTier.DAIKON,
    }
    for value, member in expected.items():
        assert member.value == value
        assert OracleTier(value) is member
        assert isinstance(member.value, str)


def test_enum_values_match_schema(schema: dict) -> None:
    from migration_evals.types import FailureClass, OracleTier

    schema_failure = {v for v in schema["properties"]["failure_class"]["enum"] if v is not None}
    schema_tier = set(schema["properties"]["oracle_tier"]["enum"])
    assert schema_failure == {m.value for m in FailureClass}
    assert schema_tier == {m.value for m in OracleTier}
