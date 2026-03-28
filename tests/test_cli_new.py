"""Additional unit tests for aru.cli — covering untested helpers and UI components."""

import time
from unittest.mock import patch

import pytest

from aru.cli import (
    Session,
    PlanStep,
    _generate_session_id,
    _format_tool_label,
    ToolTracker,
    StatusBar,
    DEFAULT_MODEL,
)
from aru.providers import MODEL_ALIASES


# ── _generate_session_id ────────────────────────────────────────────────────

class TestGenerateSessionId:
    def test_returns_8_char_hex(self):
        sid = _generate_session_id()
        assert len(sid) == 8
        assert all(c in "0123456789abcdef" for c in sid)

    def test_unique_across_calls(self):
        ids = {_generate_session_id() for _ in range(10)}
        # All 10 should be unique (birthday paradox still very unlikely to collide)
        assert len(ids) == 10


# ── Session.estimate_tokens ─────────────────────────────────────────────────

class TestEstimateTokens:
    def test_empty_string(self):
        assert Session.estimate_tokens("") == 0

    def test_short_text(self):
        # 7 chars / 3.5 = 2 tokens
        assert Session.estimate_tokens("abcdefg") == 2

    def test_longer_text(self):
        text = "a" * 350
        assert Session.estimate_tokens(text) == 100


# ── Session.check_budget_warning ────────────────────────────────────────────

class TestCheckBudgetWarning:
    def test_no_budget_returns_none(self):
        s = Session()
        s.token_budget = 0
        assert s.check_budget_warning() is None

    def test_below_80_pct_returns_none(self):
        s = Session()
        s.token_budget = 1000
        s.total_input_tokens = 500
        s.total_output_tokens = 100  # 60 %
        assert s.check_budget_warning() is None

    def test_at_80_pct_returns_yellow_warning(self):
        s = Session()
        s.token_budget = 1000
        s.total_input_tokens = 500
        s.total_output_tokens = 300  # 80 %
        warning = s.check_budget_warning()
        assert warning is not None
        assert "80%" in warning
        assert "yellow" in warning.lower() or "yellow" in warning

    def test_at_95_pct_returns_red_critical(self):
        s = Session()
        s.token_budget = 1000
        s.total_input_tokens = 800
        s.total_output_tokens = 150  # 95 %
        warning = s.check_budget_warning()
        assert warning is not None
        assert "red" in warning.lower()


# ── Session.render_compact_progress ─────────────────────────────────────────

class TestRenderCompactProgress:
    def _make_session_with_steps(self) -> Session:
        s = Session()
        plan_text = "- [ ] Step 1: First task\n- [ ] Step 2: Second task\n- [ ] Step 3: Third task"
        s.set_plan("my task", plan_text)
        return s

    def test_empty_when_no_steps(self):
        s = Session()
        assert s.render_compact_progress(1) == ""

    def test_shows_current_step_marker(self):
        s = self._make_session_with_steps()
        result = s.render_compact_progress(current_index=2)
        assert "<< CURRENT" in result
        assert "Step 2" in result

    def test_shows_completed_steps_as_done(self):
        s = self._make_session_with_steps()
        s.plan_steps[0].status = "completed"
        result = s.render_compact_progress(current_index=2)
        assert "[x] Step 1 (done)" in result

    def test_shows_progress_header(self):
        s = self._make_session_with_steps()
        s.plan_steps[0].status = "completed"
        result = s.render_compact_progress(current_index=2)
        assert "Progress: 1/3 steps done" in result

    def test_pending_steps_listed_normally(self):
        s = self._make_session_with_steps()
        result = s.render_compact_progress(current_index=1)
        assert "Step 2" in result
        assert "Step 3" in result


# ── Session.from_dict with legacy model_key ─────────────────────────────────

class TestSessionFromDictLegacy:
    def test_legacy_model_key_mapped_to_model_ref(self):
        data = {
            "session_id": "abc12345",
            "history": [],
            "plan_steps": [],
            "model_key": "sonnet",   # old format, no "model_ref" key
        }
        s = Session.from_dict(data)
        assert s.model_ref == MODEL_ALIASES["sonnet"]

    def test_model_ref_takes_precedence_over_model_key(self):
        data = {
            "session_id": "abc12345",
            "history": [],
            "plan_steps": [],
            "model_ref": "ollama/llama3",
            "model_key": "sonnet",  # should be ignored
        }
        s = Session.from_dict(data)
        assert s.model_ref == "ollama/llama3"

    def test_unknown_legacy_key_falls_back_to_default(self):
        data = {
            "session_id": "abc12345",
            "history": [],
            "plan_steps": [],
            "model_key": "nonexistent_key",
        }
        s = Session.from_dict(data)
        assert s.model_ref == DEFAULT_MODEL


# ── _format_tool_label ──────────────────────────────────────────────────────

class TestFormatToolLabel:
    def test_known_tool_without_args(self):
        label = _format_tool_label("read_file", {})
        # Should return a human-friendly label (from TOOL_DISPLAY_NAMES)
        assert "Read" in label

    def test_batch_tool_shows_count(self):
        args = {"files": [{"path": "a.py"}, {"path": "b.py"}, {"path": "c.py"}]}
        label = _format_tool_label("write_files", args)
        assert "3" in label

    def test_single_arg_tool_shows_truncated_path(self):
        args = {"file_path": "aru/cli.py"}
        label = _format_tool_label("read_file", args)
        assert "aru/cli.py" in label

    def test_long_arg_is_truncated(self):
        long_path = "a" * 80
        args = {"file_path": long_path}
        label = _format_tool_label("read_file", args)
        # Should not contain the full 80-char path
        assert long_path not in label
        assert "..." in label

    def test_unknown_tool_falls_back_to_tool_name(self):
        label = _format_tool_label("my_custom_tool", {})
        assert "my_custom_tool" in label

    def test_bash_uses_command_arg(self):
        args = {"command": "git status"}
        label = _format_tool_label("bash", args)
        assert "git status" in label


# ── ToolTracker ─────────────────────────────────────────────────────────────

class TestToolTracker:
    def test_initial_state_empty(self):
        tracker = ToolTracker()
        assert tracker.active_labels == []
        assert tracker.pop_completed() == []

    def test_start_and_active_labels(self):
        tracker = ToolTracker()
        tracker.start("t1", "Read file.py")
        labels = tracker.active_labels
        assert len(labels) == 1
        label, elapsed = labels[0]
        assert label == "Read file.py"
        assert elapsed >= 0.0

    def test_complete_returns_label_and_duration(self):
        tracker = ToolTracker()
        tracker.start("t1", "Bash ls")
        time.sleep(0.01)
        label, duration = tracker.complete("t1")
        assert label == "Bash ls"
        assert duration >= 0.0

    def test_complete_removes_from_active(self):
        tracker = ToolTracker()
        tracker.start("t1", "Read a.py")
        tracker.complete("t1")
        assert tracker.active_labels == []

    def test_pop_completed_drains_queue(self):
        tracker = ToolTracker()
        tracker.start("t1", "Read a.py")
        tracker.complete("t1")
        completed = tracker.pop_completed()
        assert len(completed) == 1
        assert tracker.pop_completed() == []

    def test_multiple_active_tools(self):
        tracker = ToolTracker()
        tracker.start("t1", "Read a.py")
        tracker.start("t2", "Bash ls")
        assert len(tracker.active_labels) == 2

    def test_complete_unknown_id_does_not_raise(self):
        tracker = ToolTracker()
        # Completing a non-existent tool_id should not raise
        result = tracker.complete("nonexistent")
        assert result is None or isinstance(result, tuple)


# ── StatusBar ───────────────────────────────────────────────────────────────

class TestStatusBar:
    def test_initial_text_is_not_empty(self):
        bar = StatusBar()
        assert isinstance(bar.current_text, str)
        assert len(bar.current_text) > 0

    def test_set_text_overrides_cycling(self):
        bar = StatusBar()
        bar.set_text("Custom status message")
        assert bar.current_text == "Custom status message"

    def test_resume_cycling_clears_custom_text(self):
        bar = StatusBar()
        bar.set_text("Custom message")
        bar.resume_cycling()
        # After resuming, the bar should return to a cycling phrase (not our custom msg)
        assert bar.current_text != "Custom message"

    def test_custom_interval_accepted(self):
        bar = StatusBar(interval=1.0)
        assert bar._interval == 1.0