"""Shared enums and type aliases for the migration eval framework.

These symbols are the minimal vocabulary required by the foundation scaffold.
Downstream work units extend this module with additional data classes as
concrete logic is introduced.
"""

from __future__ import annotations

from enum import Enum


class FailureClass(str, Enum):
    """Four-way discriminated failure classification per PRD M6.

    Exactly one of these values is attached to a trial whose `success=False`.
    When `success=True`, the `failure_class` field is `null`.
    """

    AGENT_ERROR = "agent_error"
    HARNESS_ERROR = "harness_error"
    ORACLE_ERROR = "oracle_error"
    INFRA_ERROR = "infra_error"


class OracleTier(str, Enum):
    """Tiered oracle funnel stage labels per PRD M1.

    Each value identifies which tier of the cascading oracle funnel produced
    the trial's pass/fail signal. Cheaper tiers sit earlier in the list.

    DIFF_VALID is Tier-0: a near-zero-cost syntactic / patch-application
    check that catches the worst hallucinations (malformed patches, files
    that fail to parse) before paying for a Tier-1 sandbox compile.
    """

    DIFF_VALID = "diff_valid"
    COMPILE_ONLY = "compile_only"
    TESTS = "tests"
    AST_CONFORMANCE = "ast_conformance"
    JUDGE = "judge"
    DAIKON = "daikon"


__all__ = ["FailureClass", "OracleTier"]
