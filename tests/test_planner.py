"""Unit tests for aru.agents.planner — agent creation and configuration."""

from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from aru.agents.base import build_instructions, PLANNER_ROLE, BASE_INSTRUCTIONS
from aru.agents.planner import create_planner
from aru.tools.codebase import delegate_research, _RESEARCH_RESULT_MAX_CHARS

# Build full planner instructions for test assertions
PLANNER_INSTRUCTIONS = build_instructions("planner")


class TestPlannerInstructions:
    def test_instructions_not_empty(self):
        assert len(PLANNER_INSTRUCTIONS) > 100

    def test_mentions_delegate_research(self):
        assert "delegate_research" in PLANNER_INSTRUCTIONS

    def test_mentions_output_format(self):
        assert "## Summary" in PLANNER_INSTRUCTIONS
        assert "## Steps" in PLANNER_INSTRUCTIONS

    def test_mentions_no_docs(self):
        assert "NEVER create documentation files" in PLANNER_INSTRUCTIONS

    def test_mentions_checklist_format(self):
        assert "- [ ]" in PLANNER_INSTRUCTIONS

    def test_base_instructions_included(self):
        assert BASE_INSTRUCTIONS in PLANNER_INSTRUCTIONS

    def test_role_instructions_included(self):
        assert PLANNER_ROLE in PLANNER_INSTRUCTIONS


class TestCreatePlanner:
    @patch("aru.agents.planner.Agent")
    @patch("aru.agents.planner.create_model")
    def test_creates_agent(self, mock_create_model, mock_agent):
        agent = create_planner()
        mock_agent.assert_called_once()
        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["name"] == "Planner"
        assert call_kwargs["markdown"] is True

    @patch("aru.agents.planner.Agent")
    @patch("aru.agents.planner.create_model")
    def test_default_model(self, mock_create_model, mock_agent):
        create_planner()
        # First call is the main model, second is the compression model
        assert mock_create_model.call_count >= 1
        call_args = mock_create_model.call_args_list[0]
        assert call_args[0][0] == "anthropic/claude-sonnet-4-5"

    @patch("aru.agents.planner.Agent")
    @patch("aru.agents.planner.create_model")
    def test_custom_model_ref(self, mock_create_model, mock_agent):
        create_planner(model_ref="openai/gpt-4o")
        call_args = mock_create_model.call_args_list[0]
        assert call_args[0][0] == "openai/gpt-4o"

    @patch("aru.agents.planner.Agent")
    @patch("aru.agents.planner.create_model")
    def test_extra_instructions_appended(self, mock_create_model, mock_agent):
        create_planner(extra_instructions="Focus on security")
        call_kwargs = mock_agent.call_args[1]
        assert "Focus on security" in call_kwargs["instructions"]
        assert PLANNER_ROLE in call_kwargs["instructions"]

    @patch("aru.agents.planner.Agent")
    @patch("aru.agents.planner.create_model")
    def test_no_extra_instructions(self, mock_create_model, mock_agent):
        create_planner(extra_instructions="")
        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["instructions"] == PLANNER_INSTRUCTIONS

    @patch("aru.agents.planner.Agent")
    @patch("aru.agents.planner.create_model")
    def test_has_read_only_tools(self, mock_create_model, mock_agent):
        create_planner()
        call_kwargs = mock_agent.call_args[1]
        tools = call_kwargs["tools"]
        tool_names = [getattr(t, "__name__", str(t)) for t in tools]
        # Planner should have read-only tools
        assert "read_file" in tool_names
        assert "glob_search" in tool_names
        assert "grep_search" in tool_names

    @patch("aru.agents.planner.Agent")
    @patch("aru.agents.planner.create_model")
    def test_lower_max_tokens(self, mock_create_model, mock_agent):
        create_planner()
        # First call is the main model with max_tokens=4096
        call_args = mock_create_model.call_args_list[0]
        assert call_args[1]["max_tokens"] == 4096

    @patch("aru.agents.planner.Agent")
    @patch("aru.agents.planner.create_model")
    def test_has_delegate_research(self, mock_create_model, mock_agent):
        create_planner()
        call_kwargs = mock_agent.call_args[1]
        tools = call_kwargs["tools"]
        tool_names = [getattr(t, "__name__", str(t)) for t in tools]
        assert "delegate_research" in tool_names

    @patch("aru.agents.planner.Agent")
    @patch("aru.agents.planner.create_model")
    def test_no_write_tools(self, mock_create_model, mock_agent):
        create_planner()
        call_kwargs = mock_agent.call_args[1]
        tools = call_kwargs["tools"]
        tool_names = [getattr(t, "__name__", str(t)) for t in tools]
        assert "write_file" not in tool_names
        assert "edit_file" not in tool_names
        assert "bash" not in tool_names


class TestDelegateResearch:
    @pytest.mark.asyncio
    async def test_returns_research_answer(self):
        mock_result = MagicMock()
        mock_result.content = "auth is handled in aru/auth.py via JWT middleware"

        mock_agent_instance = MagicMock()
        mock_agent_instance.arun = AsyncMock(return_value=mock_result)

        with patch("agno.agent.Agent", return_value=mock_agent_instance), \
             patch("aru.providers.create_model"):
            result = await delegate_research("understand auth", "which file handles JWT?")

        assert "auth" in result
        assert result.startswith("[Research-")

    @pytest.mark.asyncio
    async def test_truncates_long_answer(self):
        mock_result = MagicMock()
        mock_result.content = "x" * 2000  # way over the limit

        mock_agent_instance = MagicMock()
        mock_agent_instance.arun = AsyncMock(return_value=mock_result)

        with patch("agno.agent.Agent", return_value=mock_agent_instance), \
             patch("aru.providers.create_model"):
            result = await delegate_research("task", "query")

        assert len(result) <= _RESEARCH_RESULT_MAX_CHARS + 100  # prefix + truncation marker
        assert "[truncated]" in result

    @pytest.mark.asyncio
    async def test_handles_empty_result(self):
        mock_result = MagicMock()
        mock_result.content = ""

        mock_agent_instance = MagicMock()
        mock_agent_instance.arun = AsyncMock(return_value=mock_result)

        with patch("agno.agent.Agent", return_value=mock_agent_instance), \
             patch("aru.providers.create_model"):
            result = await delegate_research("task", "query")

        assert "No findings" in result

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        mock_agent_instance = MagicMock()
        mock_agent_instance.arun = AsyncMock(side_effect=RuntimeError("network error"))

        with patch("agno.agent.Agent", return_value=mock_agent_instance), \
             patch("aru.providers.create_model"):
            result = await delegate_research("task", "query")

        assert "Error" in result
        assert "network error" in result
