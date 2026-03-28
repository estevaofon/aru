"""Unit tests for aru.agents.base — build_instructions and role constants."""

import pytest

from aru.agents.base import (
    BASE_INSTRUCTIONS,
    EXECUTOR_ROLE,
    GENERAL_ROLE,
    PLANNER_ROLE,
    build_instructions,
)


class TestBuildInstructions:
    def test_planner_role_included(self):
        result = build_instructions("planner")
        assert PLANNER_ROLE in result

    def test_executor_role_included(self):
        result = build_instructions("executor")
        assert EXECUTOR_ROLE in result

    def test_general_role_included(self):
        result = build_instructions("general")
        assert GENERAL_ROLE in result

    def test_base_instructions_always_included(self):
        for role in ("planner", "executor", "general"):
            result = build_instructions(role)
            assert BASE_INSTRUCTIONS in result, f"BASE_INSTRUCTIONS missing for role={role!r}"

    def test_extra_appended(self):
        extra = "Always prefer TypeScript over JavaScript."
        result = build_instructions("general", extra=extra)
        assert extra in result

    def test_no_extra_by_default(self):
        result = build_instructions("executor")
        assert not result.endswith("\n\n")

    def test_sections_joined_by_double_newline(self):
        extra = "Project-specific rules here."
        result = build_instructions("planner", extra=extra)
        assert "\n\n" in result
        parts = result.split("\n\n")
        assert PLANNER_ROLE in "\n\n".join(parts)
        assert BASE_INSTRUCTIONS in "\n\n".join(parts)
        assert extra in "\n\n".join(parts)

    def test_invalid_role_raises(self):
        with pytest.raises(KeyError):
            build_instructions("unknown")
            
    def test_empty_extra_parameter(self):
        """Test behavior when extra parameter is empty or None."""
        # Case: Empty string
        result_empty = build_instructions("general", extra="")
        assert not result_empty.endswith("\n\n")
        assert BASE_INSTRUCTIONS in result_empty
        
        # Case: None
        result_none = build_instructions("general", extra=None)
        assert not result_none.endswith("\n\n")
        assert BASE_INSTRUCTIONS in result_none
