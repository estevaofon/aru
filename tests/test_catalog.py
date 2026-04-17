"""Smoke tests for the native agent catalog and unified factory."""

from aru.agents.base import (
    BASE_INSTRUCTIONS,
    EXECUTOR_ROLE,
    EXPLORER_ROLE,
    GENERAL_ROLE,
    PLANNER_ROLE,
    build_instructions,
)
from aru.agents.catalog import AGENTS


class TestCatalog:
    def test_native_agents_present(self):
        assert set(AGENTS.keys()) == {"build", "plan", "executor", "explorer"}

    def test_modes(self):
        assert AGENTS["build"].mode == "primary"
        assert AGENTS["plan"].mode == "primary"
        assert AGENTS["executor"].mode == "primary"
        assert AGENTS["explorer"].mode == "subagent"

    def test_explorer_uses_small_model(self):
        assert AGENTS["explorer"].small_model is True
        assert AGENTS["build"].small_model is False
        assert AGENTS["plan"].small_model is False

    def test_max_tokens(self):
        # Primary agents (build/executor) take the full model cap — catalog
        # leaves them unbounded and providers.create_model clamps to the
        # model's ceiling. Subagents stay tight.
        assert AGENTS["build"].max_tokens is None
        assert AGENTS["executor"].max_tokens is None
        assert AGENTS["plan"].max_tokens == 4096
        assert AGENTS["explorer"].max_tokens == 8192


class TestToolFactories:
    def test_plan_tools_are_read_only(self):
        names = [getattr(t, "__name__", str(t)) for t in AGENTS["plan"].tools_factory()]
        assert "read_file" in names
        assert "grep_search" in names
        assert "glob_search" in names
        assert "write_file" not in names
        assert "edit_file" not in names
        assert "bash" not in names
        assert "delegate_task" not in names

    def test_explorer_tools_allow_bash_but_no_write(self):
        names = [getattr(t, "__name__", str(t)) for t in AGENTS["explorer"].tools_factory()]
        assert "read_file" in names
        assert "bash" in names
        assert "rank_files" in names
        assert "write_file" not in names
        assert "edit_file" not in names
        assert "delegate_task" not in names

    def test_build_tools_have_full_set(self):
        names = [getattr(t, "__name__", str(t)) for t in AGENTS["build"].tools_factory()]
        for required in ("read_file", "write_file", "edit_file", "bash", "delegate_task"):
            assert required in names

    def test_executor_tools_include_task_list(self):
        names = [getattr(t, "__name__", str(t)) for t in AGENTS["executor"].tools_factory()]
        assert "create_task_list" in names
        assert "update_task" in names
        assert "write_file" in names


class TestBuildInstructions:
    def test_planner_role_included(self):
        text = build_instructions("planner")
        assert PLANNER_ROLE in text
        assert BASE_INSTRUCTIONS in text
        assert "## Summary" in text
        assert "## Steps" in text

    def test_executor_role_included(self):
        text = build_instructions("executor")
        assert EXECUTOR_ROLE in text
        assert BASE_INSTRUCTIONS in text

    def test_general_role_included(self):
        text = build_instructions("general")
        assert GENERAL_ROLE in text
        assert BASE_INSTRUCTIONS in text

    def test_explorer_role_included(self):
        text = build_instructions("explorer")
        assert EXPLORER_ROLE in text
        assert BASE_INSTRUCTIONS in text
        assert "READ-ONLY MODE" in text

    def test_extra_instructions_appended(self):
        text = build_instructions("general", extra="Custom rule X")
        assert "Custom rule X" in text
        assert GENERAL_ROLE in text
