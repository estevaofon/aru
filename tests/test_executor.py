"""Unit tests for arc.agents.executor — agent creation and configuration."""

from unittest.mock import patch, MagicMock

import pytest

from arc.agents.executor import (
    EXECUTOR_INSTRUCTIONS,
    create_executor,
)


class TestExecutorInstructions:
    def test_instructions_not_empty(self):
        assert len(EXECUTOR_INSTRUCTIONS) > 100

    def test_mentions_read_before_edit(self):
        assert "Read files before editing" in EXECUTOR_INSTRUCTIONS

    def test_mentions_delegate_task(self):
        assert "delegate_task" in EXECUTOR_INSTRUCTIONS

    def test_mentions_no_docs(self):
        assert "NEVER create documentation files" in EXECUTOR_INSTRUCTIONS


class TestCreateExecutor:
    @patch("arc.agents.executor.Agent")
    @patch("arc.agents.executor.Claude")
    def test_creates_agent(self, mock_claude, mock_agent):
        agent = create_executor()
        mock_agent.assert_called_once()
        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["name"] == "Executor"
        assert call_kwargs["markdown"] is True

    @patch("arc.agents.executor.Agent")
    @patch("arc.agents.executor.Claude")
    def test_default_model(self, mock_claude, mock_agent):
        create_executor()
        mock_claude.assert_called_once()
        call_kwargs = mock_claude.call_args[1]
        assert "sonnet" in call_kwargs["id"]

    @patch("arc.agents.executor.Agent")
    @patch("arc.agents.executor.Claude")
    def test_custom_model(self, mock_claude, mock_agent):
        create_executor(model_id="claude-opus-4-20250514")
        call_kwargs = mock_claude.call_args[1]
        assert call_kwargs["id"] == "claude-opus-4-20250514"

    @patch("arc.agents.executor.Agent")
    @patch("arc.agents.executor.Claude")
    def test_extra_instructions_appended(self, mock_claude, mock_agent):
        create_executor(extra_instructions="Always use TypeScript")
        call_kwargs = mock_agent.call_args[1]
        assert "Always use TypeScript" in call_kwargs["instructions"]
        assert EXECUTOR_INSTRUCTIONS in call_kwargs["instructions"]

    @patch("arc.agents.executor.Agent")
    @patch("arc.agents.executor.Claude")
    def test_no_extra_instructions(self, mock_claude, mock_agent):
        create_executor(extra_instructions="")
        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["instructions"] == EXECUTOR_INSTRUCTIONS

    @patch("arc.agents.executor.Agent")
    @patch("arc.agents.executor.Claude")
    def test_tools_passed(self, mock_claude, mock_agent):
        create_executor()
        call_kwargs = mock_agent.call_args[1]
        assert "tools" in call_kwargs
        assert len(call_kwargs["tools"]) > 0
