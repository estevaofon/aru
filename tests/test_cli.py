"""Unit tests for aru.cli — session management, plan parsing, and helpers."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from aru.cli import (
    _sanitize_input,
    _resolve_mentions,
    AgentRunResult,
    PlanStep,
    parse_plan_steps,
    Session,
    SessionStore,
    PasteState,
    DEFAULT_MODEL,
    SLASH_COMMANDS,
    _MENTION_RE,
)
from aru.providers import MODEL_ALIASES


# ── _sanitize_input ─────────────────────────────────────────────────

class TestSanitizeInput:
    def test_normal_text(self):
        assert _sanitize_input("hello world") == "hello world"

    def test_unicode_text(self):
        assert _sanitize_input("café résumé") == "café résumé"

    def test_empty_string(self):
        assert _sanitize_input("") == ""

    def test_replaces_invalid_surrogates(self):
        # Simulate broken surrogates — should not raise
        result = _sanitize_input("test\x00data")
        assert isinstance(result, str)


# ── _resolve_mentions ────────────────────────────────────────────────

class TestResolveMentions:
    def test_no_mentions(self, tmp_path):
        mr = _resolve_mentions("hello world", str(tmp_path))
        assert mr.text == "hello world"
        assert mr.count == 0
        assert mr.file_messages == []

    def test_resolves_file_mention(self, tmp_path):
        (tmp_path / "config.py").write_text("DEBUG = True")
        mr = _resolve_mentions("check @config.py", str(tmp_path))
        # File content now goes into file_messages, not inline text
        assert mr.count == 1
        assert len(mr.file_messages) == 2  # assistant label + user content
        assert "read_file: config.py" in mr.file_messages[0]["content"]
        assert "DEBUG = True" in mr.file_messages[1]["content"]

    def test_nonexistent_file_ignored(self, tmp_path):
        mr = _resolve_mentions("check @missing.py", str(tmp_path))
        assert mr.text == "check @missing.py"
        assert mr.count == 0

    def test_deduplicates_mentions(self, tmp_path):
        (tmp_path / "file.py").write_text("code")
        mr = _resolve_mentions("@file.py and @file.py", str(tmp_path))
        assert mr.count == 1
        assert len(mr.file_messages) == 2  # one pair

    def test_multiple_files(self, tmp_path):
        (tmp_path / "a.py").write_text("aaa")
        (tmp_path / "b.py").write_text("bbb")
        mr = _resolve_mentions("@a.py and @b.py", str(tmp_path))
        assert mr.count == 2
        assert len(mr.file_messages) == 4  # two pairs
        all_content = " ".join(m["content"] for m in mr.file_messages)
        assert "aaa" in all_content
        assert "bbb" in all_content

    def test_mention_regex_pattern(self):
        matches = _MENTION_RE.findall("check @file.py now")
        assert "file.py" in matches

    def test_mention_not_in_email(self):
        matches = _MENTION_RE.findall("user@email.com")
        assert "email.com" not in matches


# ── PlanStep ─────────────────────────────────────────────────────────

class TestPlanStep:
    def test_creation(self):
        step = PlanStep(1, "Do something")
        assert step.index == 1
        assert step.description == "Do something"
        assert step.status == "pending"

    def test_checkbox_pending(self):
        step = PlanStep(1, "task")
        assert "[ ]" in step.checkbox

    def test_checkbox_completed(self):
        step = PlanStep(1, "task")
        step.status = "completed"
        assert "[x]" in step.checkbox

    def test_checkbox_in_progress(self):
        step = PlanStep(1, "task")
        step.status = "in_progress"
        assert "[~]" in step.checkbox

    def test_checkbox_failed(self):
        step = PlanStep(1, "task")
        step.status = "failed"
        assert "[!]" in step.checkbox

    def test_str(self):
        step = PlanStep(2, "Edit file")
        assert str(step) == "Step 2: Edit file"

    def test_to_dict(self):
        step = PlanStep(1, "task")
        step.status = "completed"
        d = step.to_dict()
        assert d == {"index": 1, "description": "task", "subtasks": [], "status": "completed"}

    def test_from_dict(self):
        d = {"index": 3, "description": "test", "status": "in_progress"}
        step = PlanStep.from_dict(d)
        assert step.index == 3
        assert step.description == "test"
        assert step.status == "in_progress"

    def test_from_dict_default_status(self):
        d = {"index": 1, "description": "test"}
        step = PlanStep.from_dict(d)
        assert step.status == "pending"


# ── parse_plan_steps ─────────────────────────────────────────────────

class TestParsePlanSteps:
    def test_checkbox_format(self):
        plan = """\
## Steps
- [ ] Step 1: Read the file
- [ ] Step 2: Edit the function
- [ ] Step 3: Run tests
"""
        steps = parse_plan_steps(plan)
        assert len(steps) == 3
        assert "Read the file" in steps[0].description
        assert "Run tests" in steps[2].description

    def test_numbered_format(self):
        plan = """\
## Steps
1. Read the file
2. Edit the function
3. Run tests
"""
        steps = parse_plan_steps(plan)
        assert len(steps) == 3

    def test_step_prefix_format(self):
        plan = """\
## Steps
- [ ] Step 1: Read files
- [ ] Step 2: Make changes
"""
        steps = parse_plan_steps(plan)
        assert len(steps) == 2
        # Step prefix should be cleaned
        assert not steps[0].description.startswith("Step 1:")

    def test_empty_plan(self):
        steps = parse_plan_steps("")
        assert steps == []

    def test_no_steps_text(self):
        steps = parse_plan_steps("Just some plain text without any steps")
        assert steps == []

    def test_single_numbered_item_not_plan(self):
        # Need at least 2 items for numbered fallback
        plan = "1. Only one item"
        steps = parse_plan_steps(plan)
        assert steps == []

    def test_checked_checkbox(self):
        plan = "- [x] Already done\n- [ ] Still pending"
        steps = parse_plan_steps(plan)
        assert len(steps) == 2


# ── Session ──────────────────────────────────────────────────────────

class TestSession:
    def test_creation_defaults(self):
        session = Session()
        assert session.session_id
        assert session.history == []
        assert session.current_plan is None
        assert session.model_ref == DEFAULT_MODEL
        assert session.total_input_tokens == 0

    def test_model_id_property(self):
        session = Session()
        session.model_ref = "anthropic/claude-opus-4"
        assert session.model_id == "claude-opus-4-20250514"

    def test_title_from_plan_task(self):
        session = Session()
        session.plan_task = "Add authentication to the API"
        assert session.title == "Add authentication to the API"

    def test_title_from_first_message(self):
        session = Session()
        session.history = [{"role": "user", "content": "fix the bug in login"}]
        assert "fix the bug" in session.title

    def test_title_empty_session(self):
        session = Session()
        assert session.title == "(empty session)"

    def test_add_message(self):
        session = Session()
        session.add_message("user", "hello")
        assert len(session.history) == 1
        assert session.history[0]["role"] == "user"

    def test_add_message_summarizes_and_truncates_history(self):
        session = Session()
        for i in range(35):
            session.add_message("user", f"msg {i}")
        # History should be bounded (summarization + hard cap)
        assert len(session.history) <= 30
        # First message should be a summary of older messages
        assert "[Conversation summary" in session.history[0]["content"]

    def test_set_plan(self):
        session = Session()
        plan = "- [ ] Step 1: Read\n- [ ] Step 2: Write"
        session.set_plan("task description", plan)
        assert session.current_plan == plan
        assert session.plan_task == "task description"
        assert len(session.plan_steps) == 2

    def test_clear_plan(self):
        session = Session()
        session.set_plan("task", "- [ ] Step 1\n- [ ] Step 2")
        session.clear_plan()
        assert session.current_plan is None
        assert session.plan_task is None
        assert session.plan_steps == []

    def test_track_tokens(self):
        session = Session()
        metrics = MagicMock()
        metrics.input_tokens = 100
        metrics.output_tokens = 50
        metrics.cache_read_tokens = 30
        metrics.cache_write_tokens = 10
        session.track_tokens(metrics)
        assert session.total_input_tokens == 100
        assert session.total_output_tokens == 50
        assert session.total_cache_read_tokens == 30
        assert session.api_calls == 1

    def test_track_tokens_none_metrics(self):
        session = Session()
        session.track_tokens(None)
        assert session.total_input_tokens == 0

    def test_token_summary_empty(self):
        session = Session()
        assert session.token_summary == ""

    def test_token_summary_with_tokens(self):
        session = Session()
        session.total_input_tokens = 1000
        session.total_output_tokens = 500
        session.api_calls = 3
        summary = session.token_summary
        assert "1,000" in summary or "1000" in summary
        assert "calls: 3" in summary

    def test_token_summary_with_cache(self):
        session = Session()
        session.total_input_tokens = 100
        session.total_output_tokens = 50
        session.total_cache_read_tokens = 200
        session.api_calls = 1
        assert "cached" in session.token_summary

    def test_to_dict_and_from_dict(self):
        session = Session(session_id="test123")
        session.add_message("user", "hello")
        session.set_plan("task", "- [ ] Step 1: Do it\n- [ ] Step 2: Test it")
        session.model_ref = "anthropic/claude-opus-4"

        d = session.to_dict()
        restored = Session.from_dict(d)

        assert restored.session_id == "test123"
        assert len(restored.history) == 1
        assert restored.current_plan is not None
        assert restored.model_ref == "anthropic/claude-opus-4"
        assert len(restored.plan_steps) == 2

    def test_get_context_summary_empty(self):
        session = Session()
        result = session.get_context_summary()
        assert result == ""

    def test_get_context_summary_with_plan(self):
        session = Session()
        session.set_plan("my task", "- [ ] Step 1: Do it\n- [ ] Step 2: Test")
        result = session.get_context_summary()
        assert "Active Plan" in result
        assert "my task" in result

    def test_get_context_summary_without_plan(self):
        """History is now passed as real messages, not in context summary."""
        session = Session()
        session.add_message("user", "hello")
        session.add_message("assistant", "hi there")
        result = session.get_context_summary()
        assert result == ""

    def test_render_plan_progress_empty(self):
        session = Session()
        assert session.render_plan_progress() == ""

    def test_render_plan_progress(self):
        session = Session()
        session.set_plan("task", "- [ ] Step 1\n- [ ] Step 2\n- [ ] Step 3")
        session.plan_steps[0].status = "completed"
        session.plan_steps[1].status = "in_progress"
        result = session.render_plan_progress()
        assert "1/3" in result


# ── AgentRunResult ───────────────────────────────────────────────────

class TestAgentRunResult:
    def test_with_tools_summary_no_tools(self):
        result = AgentRunResult(content="Hello world")
        assert result.with_tools_summary() == "Hello world"

    def test_with_tools_summary_with_tools(self):
        result = AgentRunResult(
            content="I edited the file.",
            tool_calls=["Read(foo.py)", "Edit(foo.py)"],
        )
        summary = result.with_tools_summary()
        assert "[Tools]" in summary
        assert "Read(foo.py)" in summary
        assert "Edit(foo.py)" in summary
        assert summary.startswith("I edited the file.")

    def test_with_tools_summary_none_content(self):
        result = AgentRunResult(content=None, tool_calls=["Read(x.py)"])
        assert result.with_tools_summary() is None

    def test_empty_tool_calls(self):
        result = AgentRunResult(content="text", tool_calls=[])
        assert result.with_tools_summary() == "text"

    def test_summarize_preserves_tools_section(self):
        """When history is summarized, [Tools] sections should be preserved."""
        session = Session()
        msg_with_tools = "I fixed the bug.\n\n[Tools]\n  - Edit(main.py)\n  - Bash(pytest)"
        session.history = [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": msg_with_tools},
        ] * 5  # 10 messages
        # Add enough to trigger summarization (threshold=20)
        for i in range(15):
            session.add_message("user", f"msg {i}")
        # The summary should contain [Tools] references
        summary_msg = session.history[0]["content"]
        assert "[Tools]" in summary_msg
        assert "Edit(main.py)" in summary_msg


# ── SessionStore ─────────────────────────────────────────────────────

class TestSessionStore:
    def test_save_and_load(self, tmp_path):
        store = SessionStore(base_dir=str(tmp_path))
        session = Session(session_id="abc123")
        session.add_message("user", "test")
        store.save(session)

        loaded = store.load("abc123")
        assert loaded is not None
        assert loaded.session_id == "abc123"
        assert len(loaded.history) == 1

    def test_load_nonexistent(self, tmp_path):
        store = SessionStore(base_dir=str(tmp_path))
        assert store.load("missing") is None

    def test_load_prefix_match(self, tmp_path):
        store = SessionStore(base_dir=str(tmp_path))
        session = Session(session_id="abcdef12")
        store.save(session)

        loaded = store.load("abcdef")
        assert loaded is not None
        assert loaded.session_id == "abcdef12"

    def test_list_sessions(self, tmp_path):
        store = SessionStore(base_dir=str(tmp_path))
        for i in range(3):
            s = Session(session_id=f"session_{i}")
            s.add_message("user", f"msg {i}")
            store.save(s)

        sessions = store.list_sessions()
        assert len(sessions) == 3

    def test_list_sessions_limit(self, tmp_path):
        store = SessionStore(base_dir=str(tmp_path))
        for i in range(5):
            s = Session(session_id=f"s{i}")
            s.add_message("user", f"msg {i}")
            store.save(s)

        sessions = store.list_sessions(limit=2)
        assert len(sessions) == 2

    def test_list_sessions_empty(self, tmp_path):
        store = SessionStore(base_dir=str(tmp_path))
        assert store.list_sessions() == []

    def test_load_corrupt_file(self, tmp_path):
        store = SessionStore(base_dir=str(tmp_path))
        corrupt = tmp_path / "bad.json"
        corrupt.write_text("{invalid json")
        assert store.load("bad") is None

    def test_load_last(self, tmp_path):
        store = SessionStore(base_dir=str(tmp_path))
        s1 = Session(session_id="old")
        s1.add_message("user", "old msg")
        store.save(s1)

        # Explicitly set a later timestamp so the test is deterministic
        s2 = Session(session_id="new")
        s2.add_message("user", "new msg")
        store.save(s2)
        # Overwrite s2's file with a later updated_at to guarantee ordering
        path = os.path.join(str(tmp_path), "new.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["updated_at"] = "9999-12-31T23:59:59.999"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        last = store.load_last()
        assert last is not None
        assert last.session_id == "new"

    def test_load_last_empty(self, tmp_path):
        store = SessionStore(base_dir=str(tmp_path))
        assert store.load_last() is None


# ── PasteState ───────────────────────────────────────────────────────

class TestPasteState:
    def test_initial_state(self):
        ps = PasteState()
        assert ps.pasted_content is None
        assert ps.line_count == 0

    def test_set(self):
        ps = PasteState()
        ps.set("line1\nline2\nline3")
        assert ps.pasted_content == "line1\nline2\nline3"
        assert ps.line_count == 3

    def test_clear(self):
        ps = PasteState()
        ps.set("content\nhere")
        ps.clear()
        assert ps.pasted_content is None
        assert ps.line_count == 0

    def test_build_message_with_annotation(self):
        ps = PasteState()
        ps.set("code here")
        result = ps.build_message("review this")
        assert "review this" in result
        assert "code here" in result
        assert "```" in result

    def test_build_message_no_annotation(self):
        ps = PasteState()
        ps.set("just code")
        result = ps.build_message("")
        assert result == "just code"

    def test_build_message_no_paste(self):
        ps = PasteState()
        result = ps.build_message("normal text")
        assert result == "normal text"


# ── Constants ────────────────────────────────────────────────────────

class TestCliConstants:
    def test_legacy_model_aliases(self):
        assert "sonnet" in MODEL_ALIASES
        assert "opus" in MODEL_ALIASES
        assert "haiku" in MODEL_ALIASES

    def test_default_model_is_valid_ref(self):
        from aru.providers import resolve_model_ref, get_provider
        provider_key, model_name = resolve_model_ref(DEFAULT_MODEL)
        assert get_provider(provider_key) is not None

    def test_slash_commands_have_tuples(self):
        for cmd in SLASH_COMMANDS:
            assert len(cmd) == 3
            assert cmd[0].startswith("/")

    def test_slash_commands_coverage(self):
        cmd_names = [cmd[0] for cmd in SLASH_COMMANDS]
        assert "/plan" in cmd_names
        assert "/help" in cmd_names
        assert "/model" in cmd_names


class TestAskYesNoCli:
    def test_returns_true_for_yes_variants(self, monkeypatch):
        from aru import cli

        monkeypatch.setattr(cli.console, "input", lambda _: "Yes")
        assert cli.ask_yes_no("Continue?") is True

    def test_returns_false_on_eof(self, monkeypatch):
        from aru import cli

        def _raise(_):
            raise EOFError()

        monkeypatch.setattr(cli.console, "input", _raise)
        assert cli.ask_yes_no("Continue?") is False
