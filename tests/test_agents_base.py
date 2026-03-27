"""Tests for aru.agents.base — agent instruction building."""

import pytest

from aru.agents.base import (
    BASE_INSTRUCTIONS,
    EXECUTOR_ROLE,
    GENERAL_ROLE,
    PLANNER_ROLE,
    build_instructions,
)


class TestBuildInstructions:
    """Tests for build_instructions() function."""

    def test_planner_role_basic(self):
        """Test planner instructions without extras."""
        result = build_instructions("planner")
        
        # Should contain planner role text
        assert PLANNER_ROLE in result
        # Should contain base instructions
        assert BASE_INSTRUCTIONS in result
        # Should NOT contain other roles
        assert EXECUTOR_ROLE not in result
        assert GENERAL_ROLE not in result
        # Should have correct structure (role first, then base)
        assert result.startswith(PLANNER_ROLE)
        assert result.endswith(BASE_INSTRUCTIONS)

    def test_executor_role_basic(self):
        """Test executor instructions without extras."""
        result = build_instructions("executor")
        
        # Should contain executor role text
        assert EXECUTOR_ROLE in result
        # Should contain base instructions
        assert BASE_INSTRUCTIONS in result
        # Should NOT contain other roles
        assert PLANNER_ROLE not in result
        assert GENERAL_ROLE not in result
        # Should have correct structure
        assert result.startswith(EXECUTOR_ROLE)
        assert result.endswith(BASE_INSTRUCTIONS)

    def test_general_role_basic(self):
        """Test general agent instructions without extras."""
        result = build_instructions("general")
        
        # Should contain general role text
        assert GENERAL_ROLE in result
        # Should contain base instructions
        assert BASE_INSTRUCTIONS in result
        # Should NOT contain other roles
        assert PLANNER_ROLE not in result
        assert EXECUTOR_ROLE not in result
        # Should have correct structure
        assert result.startswith(GENERAL_ROLE)
        assert result.endswith(BASE_INSTRUCTIONS)

    def test_planner_with_extra_instructions(self):
        """Test planner with additional project-specific instructions."""
        extra = "Project rule: Always use TypeScript strict mode."
        result = build_instructions("planner", extra=extra)
        
        # Should contain all three parts
        assert PLANNER_ROLE in result
        assert BASE_INSTRUCTIONS in result
        assert extra in result
        # Extra should come last
        assert result.endswith(extra)
        # Should be separated by double newlines
        parts = result.split("\n\n")
        assert len(parts) >= 3
        assert extra in parts[-1]

    def test_executor_with_extra_instructions(self):
        """Test executor with additional project-specific instructions."""
        extra = "Always run linter after code changes.\nUse pytest for testing."
        result = build_instructions("executor", extra=extra)
        
        # Should contain all parts
        assert EXECUTOR_ROLE in result
        assert BASE_INSTRUCTIONS in result
        assert extra in result
        # Extra should be at the end
        assert result.endswith(extra)

    def test_general_with_extra_instructions(self):
        """Test general agent with additional project-specific instructions."""
        extra = "This is a Django project.\nFollow PEP 8 style guide."
        result = build_instructions("general", extra=extra)
        
        # Should contain all parts
        assert GENERAL_ROLE in result
        assert BASE_INSTRUCTIONS in result
        assert extra in result
        # Verify structure
        assert result.endswith(extra)

    def test_instruction_order(self):
        """Test that instructions are composed in correct order: role, base, extra."""
        extra = "EXTRA_MARKER_TEXT"
        
        for role in ["planner", "executor", "general"]:
            result = build_instructions(role, extra=extra)
            parts = result.split("\n\n")
            
            # Should have at least 3 sections
            assert len(parts) >= 3
            
            # Last part should contain extra
            assert "EXTRA_MARKER_TEXT" in parts[-1]
            
            # Base instructions should be second to last
            assert BASE_INSTRUCTIONS in parts[-2]

    def test_empty_extra_string(self):
        """Test that empty extra string doesn't add unnecessary spacing."""
        result = build_instructions("general", extra="")
        
        # Should only have role + base
        assert GENERAL_ROLE in result
        assert BASE_INSTRUCTIONS in result
        # Should not have trailing double newlines
        assert not result.endswith("\n\n")

    def test_multiline_extra_instructions(self):
        """Test handling of multiline extra instructions."""
        extra = """# Project Context
- This is a FastAPI project
- Use async/await for all endpoints
- Database: PostgreSQL with SQLAlchemy

## Code Style
- Use Black formatter
- Line length: 100 chars"""
        
        result = build_instructions("executor", extra=extra)
        
        # Should preserve all lines
        assert "# Project Context" in result
        assert "FastAPI project" in result
        assert "## Code Style" in result
        assert "Black formatter" in result

    def test_invalid_role_raises_keyerror(self):
        """Test that invalid role raises KeyError."""
        with pytest.raises(KeyError):
            build_instructions("invalid_role")

    def test_planner_read_only_constraint(self):
        """Test that planner instructions emphasize read-only constraint."""
        result = build_instructions("planner")
        
        # Should mention read-only limitation
        assert "READ-ONLY" in result
        assert "NO tools to create, write, or edit" in result
        # Should NOT mention write tools
        assert "write_file" in result  # Mentioned as NOT available
        assert "edit_file" in result   # Mentioned as NOT available

    def test_executor_write_permissions(self):
        """Test that executor instructions mention write capabilities."""
        result = build_instructions("executor")
        
        # Should mention write/edit capabilities
        assert "edit_file" in result
        assert "write_file" in result
        assert "implement" in result.lower()

    def test_general_tool_variety(self):
        """Test that general instructions mention various tool types."""
        result = build_instructions("general")
        
        # Should mention multiple tool categories
        assert "reading" in result
        assert "writing" in result
        assert "editing" in result
        assert "searching" in result
        assert "shell commands" in result
        assert "web" in result
        assert "delegate" in result

    def test_all_roles_have_base_instructions(self):
        """Test that all roles include base instructions."""
        for role in ["planner", "executor", "general"]:
            result = build_instructions(role)
            assert BASE_INSTRUCTIONS in result
            assert "Be concise and direct" in result
            assert "NEVER create documentation files" in result

    def test_separator_consistency(self):
        """Test that sections are consistently separated by double newlines."""
        extra = "Project specific rules here."
        
        for role in ["planner", "executor", "general"]:
            result = build_instructions(role, extra=extra)
            
            # Should use double newline as separator
            assert "\n\n" in result
            # Should not have triple newlines or more
            assert "\n\n\n" not in result

    def test_no_trailing_whitespace(self):
        """Test that result doesn't have unnecessary trailing whitespace."""
        for role in ["planner", "executor", "general"]:
            result = build_instructions(role)
            # Should not end with spaces or excessive newlines
            assert not result.endswith(" ")
            assert not result.endswith("\n\n\n")
            
    def test_extra_with_special_characters(self):
        """Test handling of extra instructions with special characters."""
        extra = "Use `code` and **bold** syntax.\nPath: /path/to/file\nRegex: ^[a-z]+$"
        result = build_instructions("general", extra=extra)
        
        # Should preserve special characters
        assert "`code`" in result
        assert "**bold**" in result
        assert "/path/to/file" in result
        assert "^[a-z]+$" in result