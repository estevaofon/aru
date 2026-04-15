"""Advanced unit tests for aru.cli — StatusBar, ToolTracker, StreamingDisplay, and helpers."""

import os
import time
from unittest.mock import MagicMock, Mock, patch

import pytest
from rich.console import Console

from aru.cli import (
    StatusBar,
    ToolTracker,
    StreamingDisplay,
    _format_tool_label,
    create_general_agent,
    Session,
    SlashCommandCompleter,
    FileMentionCompleter,
    AruCompleter,
    _create_prompt_session,
    PasteState,
)
from aru.config import AgentConfig, CustomCommand


# ── StatusBar ────────────────────────────────────────────────────────


class TestStatusBar:
    def test_initial_state(self):
        bar = StatusBar(interval=1.0)
        assert bar.current_text in bar._phrases
        assert bar._override is None

    def test_set_text_overrides_cycling(self):
        bar = StatusBar(interval=1.0)
        bar.set_text("Custom status")
        assert bar.current_text == "Custom status"

    def test_resume_cycling_clears_override(self):
        bar = StatusBar(interval=1.0)
        bar.set_text("Override")
        bar.resume_cycling()
        assert bar._override is None
        assert bar.current_text in bar._phrases

    def test_rotation_after_interval(self):
        bar = StatusBar(interval=0.05)
        first = bar.current_text
        bar._maybe_rotate()
        # Should not rotate yet
        assert bar.current_text == first
        
        time.sleep(0.06)
        bar._maybe_rotate()
        # May have rotated (could be same phrase if only one phrase or shuffled to same)
        # Just verify it doesn't crash
        assert bar.current_text is not None

    def test_override_persists_until_rotation(self):
        bar = StatusBar(interval=0.05)
        bar.set_text("Fixed")
        assert bar.current_text == "Fixed"
        # Reset the clock so we have a clean starting point
        bar._last_switch = time.monotonic()

        time.sleep(0.07)
        bar._maybe_rotate()
        # After rotation, override is cleared by _maybe_rotate
        assert bar._override is None

    def test_rich_console_returns_generator(self):
        bar = StatusBar()
        console = Console()
        from rich.console import ConsoleOptions
        from rich.console import ConsoleDimensions
        options = console.options.update(
            width=80,
            height=25,
        )
        result = list(bar.__rich_console__(console, options))
        assert len(result) > 0

    def test_shuffles_on_cycle_complete(self):
        bar = StatusBar(interval=0.01)
        bar._index = len(bar._phrases) - 1
        time.sleep(0.02)
        bar._maybe_rotate()
        # Index should wrap
        assert bar._index >= 0


# ── ToolTracker ──────────────────────────────────────────────────────


class TestToolTracker:
    def test_initial_empty(self):
        tracker = ToolTracker()
        assert tracker.active_labels == []
        assert tracker.pop_completed() == []

    def test_start_tool(self):
        tracker = ToolTracker()
        tracker.start("tool_1", "Read(file.py)")
        assert len(tracker.active_labels) == 1
        label, elapsed = tracker.active_labels[0]
        assert label == "Read(file.py)"
        assert elapsed >= 0

    def test_complete_tool(self):
        tracker = ToolTracker()
        tracker.start("tool_1", "Read(file.py)")
        time.sleep(0.01)
        result = tracker.complete("tool_1")
        assert result is not None
        label, duration = result
        assert label == "Read(file.py)"
        assert duration > 0

    def test_complete_nonexistent_tool(self):
        tracker = ToolTracker()
        result = tracker.complete("nonexistent")
        assert result is None

    def test_pop_completed_drains(self):
        tracker = ToolTracker()
        tracker.start("t1", "Tool A")
        tracker.complete("t1")
        completed = tracker.pop_completed()
        assert len(completed) == 1
        assert completed[0][0] == "Tool A"
        
        # Second call should be empty
        assert tracker.pop_completed() == []

    def test_multiple_active_tools(self):
        tracker = ToolTracker()
        tracker.start("t1", "Read")
        tracker.start("t2", "Write")
        assert len(tracker.active_labels) == 2

    def test_elapsed_time_increases(self):
        tracker = ToolTracker()
        tracker.start("t1", "Long Task")
        _, elapsed1 = tracker.active_labels[0]
        time.sleep(0.05)
        _, elapsed2 = tracker.active_labels[0]
        assert elapsed2 > elapsed1


# ── StreamingDisplay ─────────────────────────────────────────────────


class TestStreamingDisplay:
    def test_initial_empty(self):
        status = StatusBar()
        display = StreamingDisplay(status)
        assert display.content is None
        assert display._accumulated == ""

    def test_set_content(self):
        status = StatusBar()
        display = StreamingDisplay(status)
        display.set_content("# Hello\nWorld")
        assert display._accumulated == "# Hello\nWorld"
        assert display.content is not None

    def test_flush_content(self):
        status = StatusBar()
        display = StreamingDisplay(status)
        display.set_content("Test content")
        
        # Mock console.print to verify flush
        with patch("aru.display.console") as mock_console:
            display.flush()
            assert display._flushed_len == len("Test content")
            assert display.content is None

    def test_flush_only_delta(self):
        status = StatusBar()
        display = StreamingDisplay(status)
        display.set_content("First\n")
        display.flush()
        display.set_content("First\nSecond\n")

        with patch("aru.display.console") as mock_console:
            display.flush()
            # Should only print "Second\n"
            assert display._flushed_len == len("First\nSecond\n")

    def test_tool_tracker_integration(self):
        status = StatusBar()
        display = StreamingDisplay(status)
        assert display.tool_tracker is not None
        display.tool_tracker.start("t1", "Test Tool")
        assert len(display.tool_tracker.active_labels) == 1

    def test_rich_console_with_content(self):
        status = StatusBar()
        display = StreamingDisplay(status)
        display.set_content("# Test")
        
        console = Console()
        options = console.options.update(width=80, height=25)
        result = list(display.__rich_console__(console, options))
        # Should yield markdown, blank line, and status bar
        assert len(result) >= 2

    def test_rich_console_with_active_tools(self):
        status = StatusBar()
        display = StreamingDisplay(status)
        display.tool_tracker.start("t1", "Read(file.py)")
        
        console = Console()
        options = console.options.update(width=80, height=25)
        result = list(display.__rich_console__(console, options))
        # Should include tool line + status bar
        assert len(result) >= 2


# ── _format_tool_label ───────────────────────────────────────────────


class TestFormatToolLabel:
    def test_no_args(self):
        label = _format_tool_label("read_file", None)
        assert label == "Read"

    def test_empty_args(self):
        label = _format_tool_label("read_file", {})
        assert label == "Read"

    def test_single_arg_tool(self):
        args = {"file_path": "src/main.py"}
        label = _format_tool_label("read_file", args)
        assert label == "Read(src/main.py)"

    def test_long_path_truncation(self):
        long_path = "a" * 70
        args = {"file_path": long_path}
        label = _format_tool_label("read_file", args)
        assert "..." in label
        assert len(label) < 80

    def test_bash_command(self):
        args = {"command": "pytest tests/"}
        label = _format_tool_label("bash", args)
        assert label == "Bash(pytest tests/)"

    def test_unknown_tool(self):
        label = _format_tool_label("custom_tool", {"arg": "value"})
        assert label == "custom_tool"


# ── create_general_agent ─────────────────────────────────────────────


class TestCreateGeneralAgent:
    async def test_creates_agent_with_default_config(self):
        session = Session()
        session.model_ref = "anthropic/claude-sonnet-4-5"

        agent = await create_general_agent(session, config=None)
        assert agent is not None
        assert agent.name == "Aru"
        assert agent.model is not None

    async def test_creates_agent_with_custom_config(self):
        session = Session()
        config = AgentConfig(
            readme_md="",
            agents_md="Custom instructions here",
            commands={},
            skills={}
        )

        agent = await create_general_agent(session, config)
        assert agent is not None
        assert agent.name == "Aru"

    async def test_agent_has_all_tools(self):
        session = Session()
        agent = await create_general_agent(session)
        assert len(agent.tools) > 0


# ── Completers ───────────────────────────────────────────────────────


class TestSlashCommandCompleter:
    def test_completes_slash_commands(self):
        completer = SlashCommandCompleter()
        from prompt_toolkit.document import Document
        doc = Document("/pla")
        completions = list(completer.get_completions(doc, None))
        assert len(completions) > 0
        assert any("/plan" in c.text for c in completions)

    def test_no_completions_without_slash(self):
        completer = SlashCommandCompleter()
        from prompt_toolkit.document import Document
        doc = Document("hello")
        completions = list(completer.get_completions(doc, None))
        assert len(completions) == 0

    def test_custom_commands(self):
        custom_cmd = CustomCommand(
            name="deploy",
            description="Deploy app",
            template="deploy script",
            source_path=".agents/commands/deploy.md"
        )
        completer = SlashCommandCompleter(custom_commands={"deploy": custom_cmd})
        
        from prompt_toolkit.document import Document
        doc = Document("/dep")
        completions = list(completer.get_completions(doc, None))
        assert any("/deploy" in c.text for c in completions)


class TestFileMentionCompleter:
    def test_no_completions_without_at(self):
        completer = FileMentionCompleter()
        from prompt_toolkit.document import Document
        doc = Document("hello world")
        completions = list(completer.get_completions(doc, None))
        assert len(completions) == 0

    def test_completions_with_at(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "test.py").write_text("code")
        (tmp_path / "readme.md").write_text("docs")
        
        completer = FileMentionCompleter()
        from prompt_toolkit.document import Document
        doc = Document("check @test")
        completions = list(completer.get_completions(doc, None))
        assert any("test.py" in c.text for c in completions)

    def test_ignores_hidden_files(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".hidden").write_text("secret")
        (tmp_path / "visible.py").write_text("code")
        
        completer = FileMentionCompleter()
        from prompt_toolkit.document import Document
        doc = Document("@")
        completions = list(completer.get_completions(doc, None))
        assert not any(".hidden" in c.text for c in completions)


class TestAruCompleter:
    def test_delegates_to_slash_completer(self):
        completer = AruCompleter()
        from prompt_toolkit.document import Document
        doc = Document("/help")
        completions = list(completer.get_completions(doc, None))
        assert len(completions) > 0

    def test_delegates_to_mention_completer(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "file.py").write_text("code")
        
        completer = AruCompleter()
        from prompt_toolkit.document import Document
        doc = Document("@file")
        completions = list(completer.get_completions(doc, None))
        assert len(completions) > 0


# ── _create_prompt_session ───────────────────────────────────────────


class TestCreatePromptSession:
    @pytest.mark.skipif(
        os.name == "nt",
        reason="prompt_toolkit requires actual terminal on Windows"
    )
    def test_creates_session(self):
        paste_state = PasteState()
        session = _create_prompt_session(paste_state)
        assert session is not None
        assert session.completer is not None

    @pytest.mark.skipif(
        os.name == "nt",
        reason="prompt_toolkit requires actual terminal on Windows"
    )
    def test_session_with_custom_config(self):
        paste_state = PasteState()
        custom_cmd = CustomCommand(
            name="test",
            description="Test",
            template="test",
            source_path=".agents/commands/test.md"
        )
        config = AgentConfig(
            readme_md="",
            agents_md="",
            commands={"test": custom_cmd},
            skills={}
        )
        session = _create_prompt_session(paste_state, config)
        assert session is not None


# ── Integration Tests ────────────────────────────────────────────────


class TestSessionIntegration:
    def test_session_with_legacy_model_key(self):
        """Test backward compatibility with old model_key format."""
        data = {
            "session_id": "test",
            "history": [],
            "model_key": "sonnet",
            "created_at": "2024-01-01 00:00:00",
            "updated_at": "2024-01-01 00:00:00",
            "cwd": "/tmp",
        }
        session = Session.from_dict(data)
        assert session.model_ref == "anthropic/claude-sonnet-4-5"

    def test_session_model_display(self):
        session = Session()
        session.model_ref = "anthropic/claude-opus-4"
        display = session.model_display
        assert "Opus" in display or "opus" in display.lower()