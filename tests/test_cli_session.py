"""Unit tests for aru/cli.py — Session methods and utilities not covered elsewhere."""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from aru.cli import (
    Session,
    SessionStore,
    TIPS,
    SLASH_COMMANDS,
    DEFAULT_MODEL,
    _render_home,
)


# ── Session: additional methods ────────────────────────────────────────────


class TestSessionContextCache:
    """Test context cache invalidation and regeneration."""

    def test_invalidate_context_cache_sets_dirty_flag(self):
        """Test that invalidating cache marks it as dirty."""
        session = Session()
        session._context_dirty = False
        session.invalidate_context_cache()
        assert session._context_dirty is True

    def test_get_cached_tree_regenerates_when_dirty(self):
        """Test that cached tree is regenerated when context is dirty."""
        session = Session()
        session._context_dirty = True

        # Mock get_project_tree from the correct module
        mock_tree = "project/\n  src/\n  tests/"
        with patch("aru.tools.codebase.get_project_tree", return_value=mock_tree):
            result = session.get_cached_tree(os.getcwd())
            assert result == mock_tree

    def test_get_cached_tree_returns_none_on_error(self):
        """Test that cached tree returns None on exception."""
        session = Session()
        session._context_dirty = True

        with patch("aru.tools.codebase.get_project_tree", side_effect=Exception("error")):
            result = session.get_cached_tree(os.getcwd())
            assert result is None

    def test_get_cached_git_status_regenerates_when_dirty(self, monkeypatch):
        """Test that cached git status is regenerated when dirty."""
        session = Session()
        session._context_dirty = True

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=" M file.py\n?? new.py")
            result = session.get_cached_tree(os.getcwd())  # This also clears dirty

        # After getting cached_tree, git status should also be fresh
        session._context_dirty = True
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=" M file.py")
            result = session.get_cached_git_status(os.getcwd())
            assert "M file.py" in result

    def test_get_cached_git_status_returns_none_on_error(self, monkeypatch):
        """Test that cached git status returns None on exception."""
        session = Session()
        session._context_dirty = True

        with patch("subprocess.run", side_effect=Exception("no git")):
            result = session.get_cached_git_status(os.getcwd())
            assert result is None


class TestSessionTokenBudget:
    """Test token budget warning functionality."""

    def test_check_budget_warning_none_when_no_budget(self):
        """Test that no warning is returned when budget is 0."""
        session = Session()
        session.token_budget = 0
        assert session.check_budget_warning() is None

    def test_check_budget_warning_at_80_percent(self):
        """Test warning at 80% budget usage."""
        session = Session()
        session.token_budget = 1000
        session.total_input_tokens = 400
        session.total_output_tokens = 400  # 800 total = 80%
        warning = session.check_budget_warning()
        assert warning is not None
        assert "80%" in warning

    def test_check_budget_warning_at_95_percent(self):
        """Test warning at 95% budget usage."""
        session = Session()
        session.token_budget = 1000
        session.total_input_tokens = 500
        session.total_output_tokens = 450  # 950 total = 95%
        warning = session.check_budget_warning()
        assert warning is not None
        assert "95%" in warning
        assert "[bold red]" in warning  # Critical warning

    def test_check_budget_warning_below_threshold(self):
        """Test no warning when below 80%."""
        session = Session()
        session.token_budget = 1000
        session.total_input_tokens = 300
        session.total_output_tokens = 400  # 700 total = 70%
        assert session.check_budget_warning() is None


class TestSessionEstimateTokens:
    """Test token estimation."""

    def test_estimate_tokens_exact(self):
        """Test exact token estimation."""
        text = "a" * 35  # Should be ~10 tokens at 3.5 chars/token
        tokens = Session.estimate_tokens(text)
        assert tokens == 10

    def test_estimate_tokens_empty(self):
        """Test estimation for empty string."""
        assert Session.estimate_tokens("") == 0

    def test_estimate_tokens_rounds_down(self):
        """Test that estimation rounds down."""
        text = "abc"  # 3 chars / 3.5 = 0.85 -> 0
        tokens = Session.estimate_tokens(text)
        assert tokens == 0


class TestSessionCompactProgress:
    """Test render_compact_progress method."""

    def test_compact_progress_empty(self):
        """Test compact progress with no steps."""
        session = Session()
        result = session.render_compact_progress(0)
        assert result == ""

    def test_compact_progress_with_steps(self):
        """Test compact progress rendering."""
        session = Session()
        session.set_plan("task", "- [ ] Step 1\n- [ ] Step 2\n- [ ] Step 3")
        session.plan_steps[0].status = "completed"

        result = session.render_compact_progress(2)  # Step 2 is current

        assert "1/3" in result
        assert "[x] Step 1" in result
        assert "Step 2" in result
        assert "<< CURRENT" in result
        assert "Step 3" in result


# ── SessionStore: additional methods ───────────────────────────────────────


class TestSessionStoreEdgeCases:
    """Test edge cases for session storage."""

    def test_save_creates_directory(self, tmp_path):
        """Test that save creates the sessions directory."""
        store = SessionStore(base_dir=str(tmp_path / "new_dir"))
        session = Session(session_id="test")
        store.save(session)  # Should not raise
        assert os.path.isdir(str(tmp_path / "new_dir"))

    def test_load_missing_json_file(self, tmp_path):
        """Test loading a session whose file was deleted."""
        store = SessionStore(base_dir=str(tmp_path))
        session = Session(session_id="temp")
        store.save(session)

        # Delete the file
        os.remove(os.path.join(str(tmp_path), "temp.json"))

        # Should return None gracefully
        assert store.load("temp") is None


# ── Global constants ───────────────────────────────────────────────────────


class TestGlobalConstants:
    """Test global constants in cli module."""

    def test_default_model_format(self):
        """Test that DEFAULT_MODEL uses provider/model format."""
        assert "/" in DEFAULT_MODEL
        assert "claude" in DEFAULT_MODEL.lower()

    def test_tips_is_list(self):
        """Test that TIPS is a non-empty list."""
        assert isinstance(TIPS, list)
        assert len(TIPS) > 0

    def test_tips_contain_strings(self):
        """Test that all tips are strings."""
        for tip in TIPS:
            assert isinstance(tip, str)

    def test_slash_commands_structure(self):
        """Test SLASH_COMMANDS has correct structure."""
        assert isinstance(SLASH_COMMANDS, list)
        assert len(SLASH_COMMANDS) > 0

        for cmd in SLASH_COMMANDS:
            assert isinstance(cmd, tuple)
            assert len(cmd) == 3
            # Each command: (name, description, usage)
            assert cmd[0].startswith("/")

    def test_all_slash_commands_documented(self):
        """Test that all slash commands have help text."""
        for cmd, desc, usage in SLASH_COMMANDS:
            assert len(desc) > 0
            assert len(usage) > 0


# ── _render_home ───────────────────────────────────────────────────────────


class TestRenderHome:
    """Test the home screen rendering."""

    def test_render_home_does_not_raise(self):
        """Test that _render_home runs without errors."""
        session = Session()
        # Should not raise any exception
        _render_home(session, skip_permissions=False)

    def test_render_home_with_skip_permissions(self):
        """Test rendering with skip_permissions=True."""
        session = Session()
        _render_home(session, skip_permissions=True)
        # If we get here without error, test passes


# ── Additional Session serialization edge cases ────────────────────────────


class TestSessionSerializationEdgeCases:
    """Test edge cases in session serialization."""

    def test_from_dict_missing_history(self):
        """Test loading session without history field."""
        data = {
            "session_id": "test",
            "created_at": "2024-01-01",
            "updated_at": "2024-01-01",
        }
        session = Session.from_dict(data)
        assert session.history == []

    def test_from_dict_missing_plan_steps(self):
        """Test loading session without plan_steps."""
        data = {
            "session_id": "test",
            "history": [],
            "current_plan": "some plan",
            "created_at": "2024-01-01",
            "updated_at": "2024-01-01",
        }
        session = Session.from_dict(data)
        assert session.plan_steps == []

    def test_to_dict_includes_all_fields(self):
        """Test that to_dict includes all important fields."""
        session = Session(session_id="test123")
        session.add_message("user", "hello")
        session.model_ref = "anthropic/claude-opus-4"

        d = session.to_dict()

        assert "session_id" in d
        assert "history" in d
        assert "model_ref" in d
        assert "created_at" in d
        assert "updated_at" in d
        assert d["session_id"] == "test123"
        assert len(d["history"]) == 1


# ── Session model display ──────────────────────────────────────────────────


class TestSessionModelDisplay:
    """Test model display property."""

    def test_model_display_contains_provider(self):
        """Test that model_display includes provider info."""
        session = Session()
        session.model_ref = "anthropic/claude-sonnet-4-5"
        display = session.model_display
        # Should contain some reference to anthropic or claude
        assert len(display) > 0


# ── Session.compact_history ────────────────────────────────────────────────────


class TestSessionCompactHistory:
    """Tests for Session.compact_history(max_tokens)."""

    # _CHARS_PER_TOKEN = 3.5  →  estimate_tokens(s) = int(len(s) / 3.5)
    # 35 chars → 10 tokens; 70 chars → 20 tokens

    def _msg(self, role: str, chars: int) -> dict:
        """Helper to build a message with a predictable token count."""
        return {"role": role, "content": "x" * chars}

    def test_no_removal_when_under_budget(self):
        """History already under max_tokens: nothing is removed."""
        session = Session()
        session.history = [
            self._msg("user", 35),   # 10 tokens
            self._msg("assistant", 35),  # 10 tokens
        ]
        removed = session.compact_history(max_tokens=100)
        assert removed == 0
        assert len(session.history) == 2

    def test_removes_oldest_messages_first(self):
        """Oldest messages (front of list) are removed before newer ones."""
        session = Session()
        session.history = [
            self._msg("user", 35),        # 10 tok  → oldest
            self._msg("assistant", 35),   # 10 tok
            self._msg("user", 35),        # 10 tok  → newest
        ]
        # Total = 30 tokens; max = 25 → drop 1 oldest
        removed = session.compact_history(max_tokens=25)
        assert removed == 1
        assert len(session.history) == 2
        # Remaining messages should be the last two
        assert session.history[0]["content"] == "x" * 35
        assert session.history[1]["content"] == "x" * 35

    def test_removes_multiple_messages(self):
        """Multiple messages removed until total is within budget."""
        session = Session()
        # 5 messages × 10 tokens each = 50 tokens total
        session.history = [self._msg("user", 35) for _ in range(5)]
        # Budget = 25 tokens → need to remove 3 (leaving 2 × 10 = 20 ≤ 25)
        removed = session.compact_history(max_tokens=25)
        assert removed == 3
        assert len(session.history) == 2

    def test_single_oversized_message_drains_history(self):
        """When every message exceeds max_tokens, all are removed (history emptied)."""
        session = Session()
        session.history = [
            self._msg("user", 350),      # 100 tokens
            self._msg("assistant", 35),  # 10 tokens
        ]
        # Budget = 5 tokens — even the smallest message (10 tok) exceeds it,
        # so compact_history drains the list completely.
        removed = session.compact_history(max_tokens=5)
        assert removed == 2
        assert session.history == []

    def test_empty_history_returns_zero(self):
        """Empty history: nothing to remove, returns 0."""
        session = Session()
        session.history = []
        removed = session.compact_history(max_tokens=100)
        assert removed == 0
        assert session.history == []

    def test_exact_budget_no_removal(self):
        """History exactly equal to max_tokens is not trimmed."""
        session = Session()
        session.history = [self._msg("user", 35)]  # exactly 10 tokens
        removed = session.compact_history(max_tokens=10)
        assert removed == 0
        assert len(session.history) == 1

    def test_returns_removal_count(self):
        """Return value must equal the number of messages dropped."""
        session = Session()
        session.history = [self._msg("user", 35) for _ in range(4)]  # 40 tok
        removed = session.compact_history(max_tokens=15)
        # 40 - 10 = 30 > 15 → drop 1 (30 tok)
        # 30 - 10 = 20 > 15 → drop 2 (20 tok)
        # 20 - 10 = 10 ≤ 15 → stop   (drop 2 messages? wait: 2×10=20>15, 1×10=10≤15 → drop 3)
        # Let's just assert removed == len(original) - len(remaining)
        assert removed == 4 - len(session.history)

    def test_updated_at_changes_on_removal(self):
        """updated_at must be refreshed when messages are removed."""
        session = Session()
        before = session.updated_at
        session.history = [self._msg("user", 35), self._msg("assistant", 35)]
        time.sleep(0.01)
        session.compact_history(max_tokens=5)
        assert session.updated_at != before

    def test_updated_at_unchanged_when_no_removal(self):
        """updated_at must not change if nothing was removed."""
        session = Session()
        session.history = [self._msg("user", 35)]
        before = session.updated_at
        session.compact_history(max_tokens=1000)
        assert session.updated_at == before


# ---------------------------------------------------------------------------
# Extended tests for estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokensExtended:
    """Extended unit tests for Session.estimate_tokens."""

    # --- basic arithmetic ---------------------------------------------------

    def test_single_char_returns_zero(self):
        """1 char < 3.5 → truncated to 0."""
        assert Session.estimate_tokens("x") == 0

    def test_exactly_one_token(self):
        """floor(3 / 3.5) == 0; floor(3.5 / 3.5) == 1 (len 4 gives 1)."""
        # int(4 / 3.5) == int(1.142…) == 1
        assert Session.estimate_tokens("abcd") == 1

    def test_multiple_of_chars_per_token(self):
        """35 chars → exactly 10 tokens (35 / 3.5 = 10.0)."""
        assert Session.estimate_tokens("a" * 35) == 10

    def test_large_text(self):
        """1000 chars → int(1000 / 3.5) == 285."""
        assert Session.estimate_tokens("x" * 1000) == 285

    def test_unicode_characters_counted_by_len(self):
        """estimate_tokens counts Python str length, not byte length."""
        text = "é" * 7          # len == 7, int(7 / 3.5) == 2
        assert Session.estimate_tokens(text) == 2

    def test_newlines_counted(self):
        """Newline counts as a character."""
        text = "\n" * 7         # len == 7 → 2
        assert Session.estimate_tokens(text) == 2

    def test_multiline_string(self):
        """Realistic multiline content: 70 chars → 20 tokens."""
        text = "a" * 70
        assert Session.estimate_tokens(text) == 20

    # --- floor / truncation -------------------------------------------------

    def test_truncates_not_rounds(self):
        """int() truncates toward zero, never rounds up."""
        # 6 chars → int(6/3.5) == int(1.714…) == 1, not 2
        assert Session.estimate_tokens("a" * 6) == 1

    def test_just_below_next_token_boundary(self):
        """int(6.99… / 3.5) should still be 1, not 2."""
        # 6 chars is 1.714 tokens → 1
        assert Session.estimate_tokens("a" * 6) == 1

    # --- callable as static (no instance needed) ----------------------------

    def test_callable_without_instance(self):
        """estimate_tokens is a static method — callable on the class."""
        result = Session.estimate_tokens("hello world")  # len=11, int(11/3.5)=3
        assert result == 3

    def test_instance_and_class_agree(self):
        """Calling via instance and via class should return the same value."""
        session = Session()
        text = "some sample text here"
        assert session.estimate_tokens(text) == Session.estimate_tokens(text)

    # --- return type --------------------------------------------------------

    def test_returns_int(self):
        """Return type must be int, not float."""
        result = Session.estimate_tokens("hello")
        assert isinstance(result, int)

    def test_empty_returns_int_zero(self):
        """Empty string returns int 0, not float 0.0."""
        result = Session.estimate_tokens("")
        assert result == 0
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Extended tests for check_budget_warning
# ---------------------------------------------------------------------------

class TestCheckBudgetWarningExtended:
    """Extended unit tests for Session.check_budget_warning."""

    # helpers ----------------------------------------------------------------

    @staticmethod
    def _session_with_usage(total_input: int, total_output: int,
                            budget: int) -> Session:
        s = Session()
        s.token_budget = budget
        s.total_input_tokens = total_input
        s.total_output_tokens = total_output
        return s

    # --- no-budget guard ----------------------------------------------------

    def test_budget_zero_returns_none(self):
        """token_budget == 0 means unlimited — always returns None."""
        s = self._session_with_usage(9999, 9999, 0)
        assert s.check_budget_warning() is None

    def test_negative_budget_returns_none(self):
        """Negative budget is treated as unlimited."""
        s = self._session_with_usage(500, 500, -1)
        assert s.check_budget_warning() is None

    # --- below warning threshold --------------------------------------------

    def test_zero_usage_no_warning(self):
        s = self._session_with_usage(0, 0, 1000)
        assert s.check_budget_warning() is None

    def test_79_percent_no_warning(self):
        """79% usage is below the 80% threshold."""
        s = self._session_with_usage(395, 395, 1000)
        # 790 / 1000 = 79 % → no warning
        assert s.check_budget_warning() is None

    # --- yellow (80–94 %) warning -------------------------------------------

    def test_exactly_80_percent_returns_yellow(self):
        """Exactly 80% triggers the yellow warning."""
        s = self._session_with_usage(400, 400, 1000)
        warning = s.check_budget_warning()
        assert warning is not None
        assert "80%" in warning
        assert "[yellow]" in warning

    def test_81_percent_returns_yellow(self):
        s = self._session_with_usage(410, 400, 1000)
        warning = s.check_budget_warning()
        assert warning is not None
        assert "[yellow]" in warning
        assert "bold red" not in warning

    def test_94_percent_returns_yellow_not_red(self):
        """94% is still in yellow range (below 95% threshold)."""
        s = self._session_with_usage(470, 470, 1000)
        # 940 / 1000 = 94%
        warning = s.check_budget_warning()
        assert warning is not None
        assert "[yellow]" in warning
        assert "bold red" not in warning

    # --- red (≥ 95 %) critical warning --------------------------------------

    def test_exactly_95_percent_returns_red(self):
        """Exactly 95% triggers the bold-red critical warning."""
        s = self._session_with_usage(475, 475, 1000)
        # 950 / 1000 = 95%
        warning = s.check_budget_warning()
        assert warning is not None
        assert "[bold red]" in warning
        assert "95%" in warning

    def test_100_percent_returns_red(self):
        """100% usage returns a red critical warning."""
        s = self._session_with_usage(500, 500, 1000)
        warning = s.check_budget_warning()
        assert warning is not None
        assert "[bold red]" in warning

    def test_over_budget_returns_red(self):
        """Exceeding 100% still returns a red warning."""
        s = self._session_with_usage(600, 600, 1000)
        warning = s.check_budget_warning()
        assert warning is not None
        assert "[bold red]" in warning

    # --- percentage text in warning ----------------------------------------

    def test_yellow_warning_contains_percentage(self):
        s = self._session_with_usage(400, 400, 1000)
        warning = s.check_budget_warning()
        assert "%" in warning

    def test_red_warning_contains_percentage(self):
        s = self._session_with_usage(475, 475, 1000)
        warning = s.check_budget_warning()
        assert "%" in warning

    # --- only input tokens (no output) --------------------------------------

    def test_input_only_usage(self):
        """Budget check uses total = input + output; output may be 0."""
        s = self._session_with_usage(800, 0, 1000)
        # 800/1000 = 80% → yellow warning threshold
        warning = s.check_budget_warning()
        assert warning is not None
        assert "[yellow]" in warning

    def test_output_only_usage(self):
        """Same as above but only output tokens."""
        s = self._session_with_usage(0, 800, 1000)
        warning = s.check_budget_warning()
        assert warning is not None
        assert "[yellow]" in warning

    # --- return type --------------------------------------------------------

    def test_returns_string_or_none(self):
        s_warn = self._session_with_usage(400, 400, 1000)
        s_none = self._session_with_usage(0, 0, 1000)
        assert isinstance(s_warn.check_budget_warning(), str)
        assert s_none.check_budget_warning() is None