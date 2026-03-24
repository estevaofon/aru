"""Unit tests for arc.agents.planner — agent creation and configuration."""

from unittest.mock import patch, MagicMock

import pytest

from arc.agents.planner import (
    PLANNER_INSTRUCTIONS,
    create_planner,
)


class TestPlannerInstructions:
    def test_instructions_not_empty(self):
        assert len(PLANNER_INSTRUCTIONS) > 100

    def test_mentions_rank_files(self):
        assert "rank_files" in PLANNER_INSTRUCTIONS

    def test_mentions_output_format(self):
        assert "## Summary" in PLANNER_INSTRUCTIONS
        assert "## Steps" in PLANNER_INSTRUCTIONS

    def test_mentions_no_docs(self):
        assert "Never include documentation files" in PLANNER_INSTRUCTIONS

    def test_mentions_checklist_format(self):
        assert "- [ ]" in PLANNER_INSTRUCTIONS


class TestCreatePlanner:
    @patch("arc.agents.planner.Agent")
    @patch("arc.agents.planner.Claude")
    def test_creates_agent(self, mock_claude, mock_agent):
        agent = create_planner()
        mock_agent.assert_called_once()
        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["name"] == "Planner"
        assert call_kwargs["markdown"] is True

    @patch("arc.agents.planner.Agent")
    @patch("arc.agents.planner.Claude")
    def test_default_model(self, mock_claude, mock_agent):
        create_planner()
        mock_claude.assert_called_once()
        call_kwargs = mock_claude.call_args[1]
        assert "sonnet" in call_kwargs["id"]

    @patch("arc.agents.planner.Agent")
    @patch("arc.agents.planner.Claude")
    def test_custom_model(self, mock_claude, mock_agent):
        create_planner(model_id="claude-opus-4-20250514")
        call_kwargs = mock_claude.call_args[1]
        assert call_kwargs["id"] == "claude-opus-4-20250514"

    @patch("arc.agents.planner.Agent")
    @patch("arc.agents.planner.Claude")
    def test_extra_instructions_appended(self, mock_claude, mock_agent):
        create_planner(extra_instructions="Focus on security")
        call_kwargs = mock_agent.call_args[1]
        assert "Focus on security" in call_kwargs["instructions"]
        assert PLANNER_INSTRUCTIONS in call_kwargs["instructions"]

    @patch("arc.agents.planner.Agent")
    @patch("arc.agents.planner.Claude")
    def test_no_extra_instructions(self, mock_claude, mock_agent):
        create_planner(extra_instructions="")
        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["instructions"] == PLANNER_INSTRUCTIONS

    @patch("arc.agents.planner.Agent")
    @patch("arc.agents.planner.Claude")
    def test_has_read_only_tools(self, mock_claude, mock_agent):
        create_planner()
        call_kwargs = mock_agent.call_args[1]
        tools = call_kwargs["tools"]
        tool_names = [getattr(t, "__name__", str(t)) for t in tools]
        # Planner should have read-only tools
        assert "read_file" in tool_names
        assert "glob_search" in tool_names
        assert "grep_search" in tool_names

    @patch("arc.agents.planner.Agent")
    @patch("arc.agents.planner.Claude")
    def test_lower_max_tokens(self, mock_claude, mock_agent):
        create_planner()
        call_kwargs = mock_claude.call_args[1]
        assert call_kwargs["max_tokens"] == 4096
