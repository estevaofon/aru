"""Unit tests for aru.agents.executor — agent creation and configuration."""

from unittest.mock import patch, MagicMock

import pytest

from aru.agents.base import build_instructions, EXECUTOR_ROLE, BASE_INSTRUCTIONS
from aru.agents.executor import create_executor

# Build full executor instructions for test assertions
EXECUTOR_INSTRUCTIONS = build_instructions("executor")


class TestExecutorInstructions:
    def test_instructions_not_empty(self):
        assert len(EXECUTOR_INSTRUCTIONS) > 100

    def test_mentions_read_before_edit(self):
        assert "Read files before editing" in EXECUTOR_INSTRUCTIONS

    def test_mentions_delegate_task(self):
        assert "delegate_task" in EXECUTOR_INSTRUCTIONS

    def test_mentions_no_docs(self):
        assert "NEVER create documentation files" in EXECUTOR_INSTRUCTIONS

    def test_base_instructions_included(self):
        assert BASE_INSTRUCTIONS in EXECUTOR_INSTRUCTIONS

    def test_role_instructions_included(self):
        assert EXECUTOR_ROLE in EXECUTOR_INSTRUCTIONS


class TestCreateExecutor:
    @patch("aru.agents.executor.Agent")
    @patch("aru.agents.executor.create_model")
    def test_creates_agent(self, mock_create_model, mock_agent):
        agent = create_executor()
        mock_agent.assert_called_once()
        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["name"] == "Executor"
        assert call_kwargs["markdown"] is True

    @patch("aru.agents.executor.Agent")
    @patch("aru.agents.executor.create_model")
    def test_default_model(self, mock_create_model, mock_agent):
        create_executor()
        # First call is the main model, second is the compression model
        assert mock_create_model.call_count >= 1
        call_args = mock_create_model.call_args_list[0]
        assert call_args[0][0] == "anthropic/claude-sonnet-4-5"

    @patch("aru.agents.executor.Agent")
    @patch("aru.agents.executor.create_model")
    def test_custom_model_ref(self, mock_create_model, mock_agent):
        create_executor(model_ref="ollama/llama3.1")
        call_args = mock_create_model.call_args_list[0]
        assert call_args[0][0] == "ollama/llama3.1"

    @patch("aru.agents.executor.Agent")
    @patch("aru.agents.executor.create_model")
    def test_extra_instructions_appended(self, mock_create_model, mock_agent):
        create_executor(extra_instructions="Always use TypeScript")
        call_kwargs = mock_agent.call_args[1]
        assert "Always use TypeScript" in call_kwargs["instructions"]
        assert EXECUTOR_ROLE in call_kwargs["instructions"]

    @patch("aru.agents.executor.Agent")
    @patch("aru.agents.executor.create_model")
    def test_no_extra_instructions(self, mock_create_model, mock_agent):
        create_executor(extra_instructions="")
        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["instructions"] == EXECUTOR_INSTRUCTIONS

    @patch("aru.agents.executor.Agent")
    @patch("aru.agents.executor.create_model")
    def test_tools_passed(self, mock_create_model, mock_agent):
        create_executor()
        call_kwargs = mock_agent.call_args[1]
        assert "tools" in call_kwargs
        assert len(call_kwargs["tools"]) > 0
