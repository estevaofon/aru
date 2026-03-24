"""Unit tests for arc.cli completers — SlashCommandCompleter, FileMentionCompleter, ArcCompleter."""

import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from prompt_toolkit.document import Document

from arc.cli import (
    SlashCommandCompleter,
    FileMentionCompleter,
    ArcCompleter,
    SLASH_COMMANDS,
)
from arc.config import CustomCommand


# ── SlashCommandCompleter ────────────────────────────────────────────

class TestSlashCommandCompleter:
    """Tests for slash command autocompletion."""

    def test_no_completions_for_non_slash_input(self):
        completer = SlashCommandCompleter()
        doc = Document("hello world")
        completions = list(completer.get_completions(doc, Mock()))
        assert completions == []

    def test_no_completions_for_empty_input(self):
        completer = SlashCommandCompleter()
        doc = Document("")
        completions = list(completer.get_completions(doc, Mock()))
        assert completions == []

    def test_shows_all_commands_for_single_slash(self):
        completer = SlashCommandCompleter()
        doc = Document("/")
        completions = list(completer.get_completions(doc, Mock()))
        # Should show all built-in commands
        assert len(completions) >= len(SLASH_COMMANDS)
        cmd_names = [c.text for c in completions]
        assert "/help" in cmd_names
        assert "/plan" in cmd_names
        assert "/model" in cmd_names

    def test_filters_commands_by_prefix(self):
        completer = SlashCommandCompleter()
        doc = Document("/pl")
        completions = list(completer.get_completions(doc, Mock()))
        cmd_names = [c.text for c in completions]
        assert "/plan" in cmd_names
        assert "/help" not in cmd_names
        assert "/model" not in cmd_names

    def test_case_sensitive_matching(self):
        completer = SlashCommandCompleter()
        doc = Document("/HE")
        completions = list(completer.get_completions(doc, Mock()))
        # Commands are lowercase, so no match
        assert len(completions) == 0

    def test_completion_metadata(self):
        completer = SlashCommandCompleter()
        doc = Document("/help")
        completions = list(completer.get_completions(doc, Mock()))
        assert len(completions) >= 1
        # Should have display_meta with description
        help_completion = next(c for c in completions if c.text == "/help")
        assert help_completion.display_meta
        # display_meta is FormattedText, convert to string
        meta_str = str(help_completion.display_meta).lower()
        assert "help" in meta_str

    def test_completion_start_position(self):
        completer = SlashCommandCompleter()
        doc = Document("/mod")
        completions = list(completer.get_completions(doc, Mock()))
        if completions:
            # start_position should replace the entire typed text
            assert completions[0].start_position == -4  # len("/mod")

    def test_custom_commands_empty(self):
        completer = SlashCommandCompleter(custom_commands={})
        doc = Document("/")
        completions = list(completer.get_completions(doc, Mock()))
        # Should only have built-in commands
        cmd_names = [c.text for c in completions]
        assert "/help" in cmd_names

    def test_custom_commands_included(self):
        custom = {
            "deploy": CustomCommand(
                name="deploy",
                description="Deploy the application",
                template="Deploy to production",
                source_path=".agents/commands/deploy.md",
            ),
            "test": CustomCommand(
                name="test",
                description="Run tests",
                template="Run all tests",
                source_path=".agents/commands/test.md",
            ),
        }
        completer = SlashCommandCompleter(custom_commands=custom)
        doc = Document("/")
        completions = list(completer.get_completions(doc, Mock()))
        cmd_names = [c.text for c in completions]
        assert "/deploy" in cmd_names
        assert "/test" in cmd_names

    def test_custom_command_filters_by_prefix(self):
        custom = {
            "deploy": CustomCommand(
                name="deploy",
                description="Deploy app",
                template="Deploy",
                source_path=".agents/commands/deploy.md",
            ),
            "debug": CustomCommand(
                name="debug",
                description="Debug app",
                template="Debug",
                source_path=".agents/commands/debug.md",
            ),
        }
        completer = SlashCommandCompleter(custom_commands=custom)
        doc = Document("/dep")
        completions = list(completer.get_completions(doc, Mock()))
        cmd_names = [c.text for c in completions]
        assert "/deploy" in cmd_names
        assert "/debug" not in cmd_names

    def test_custom_command_metadata(self):
        custom = {
            "migrate": CustomCommand(
                name="migrate",
                description="Run database migrations",
                template="Migrate DB",
                source_path=".agents/commands/migrate.md",
            ),
        }
        completer = SlashCommandCompleter(custom_commands=custom)
        doc = Document("/migrate")
        completions = list(completer.get_completions(doc, Mock()))
        assert len(completions) >= 1
        migrate_completion = next(c for c in completions if c.text == "/migrate")
        meta_str = str(migrate_completion.display_meta)
        assert "database migrations" in meta_str

    def test_no_completions_mid_text(self):
        # Slash not at start should not trigger completions
        completer = SlashCommandCompleter()
        doc = Document("check /help output")
        # Document.text_before_cursor returns everything before cursor
        # In this case, if cursor is after /help, it should complete
        # But if slash is not at start of entire text, no completion
        completions = list(completer.get_completions(doc, Mock()))
        # Based on implementation: only if text.startswith("/")
        assert completions == []


# ── FileMentionCompleter ─────────────────────────────────────────────

class TestFileMentionCompleter:
    """Tests for @file mention autocompletion."""

    def test_no_completions_without_at_sign(self, tmp_path):
        completer = FileMentionCompleter()
        doc = Document("hello world")
        with patch("os.getcwd", return_value=str(tmp_path)):
            completions = list(completer.get_completions(doc, Mock()))
        assert completions == []

    def test_no_completions_for_at_in_email(self, tmp_path):
        # @ preceded by non-whitespace should not complete
        completer = FileMentionCompleter()
        doc = Document("user@example.com")
        with patch("os.getcwd", return_value=str(tmp_path)):
            completions = list(completer.get_completions(doc, Mock()))
        assert completions == []

    def test_completes_files_in_cwd(self, tmp_path):
        (tmp_path / "config.py").touch()
        (tmp_path / "main.py").touch()
        (tmp_path / "readme.md").touch()

        completer = FileMentionCompleter()
        doc = Document("check @")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "config.py" in file_names
        assert "main.py" in file_names
        assert "readme.md" in file_names

    def test_filters_by_prefix(self, tmp_path):
        (tmp_path / "config.py").touch()
        (tmp_path / "main.py").touch()
        (tmp_path / "settings.py").touch()

        completer = FileMentionCompleter()
        doc = Document("check @co")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "config.py" in file_names
        assert "main.py" not in file_names

    def test_case_insensitive_matching(self, tmp_path):
        (tmp_path / "Config.py").touch()

        completer = FileMentionCompleter()
        doc = Document("@co")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "Config.py" in file_names

    def test_includes_directories_with_slash(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "mydir").mkdir()

        completer = FileMentionCompleter()
        doc = Document("@")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        # Check that directories have trailing slash
        # Filter by checking if text ends with '/'
        dir_completions = [c for c in completions if c.text.endswith("/")]
        assert len(dir_completions) >= 2
        dir_names = [c.text for c in dir_completions]
        assert "src/" in dir_names
        assert "mydir/" in dir_names
        # Verify display_meta is set to "dir" (may be FormattedText)
        for dc in dir_completions:
            meta_str = str(dc.display_meta) if dc.display_meta else ""
            assert "dir" in meta_str

    def test_nested_path_completion(self, tmp_path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "main.py").touch()
        (src_dir / "utils.py").touch()

        completer = FileMentionCompleter()
        doc = Document("@src/")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "src/main.py" in file_names
        assert "src/utils.py" in file_names

    def test_nested_path_with_prefix(self, tmp_path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "main.py").touch()
        (src_dir / "utils.py").touch()
        (src_dir / "config.py").touch()

        completer = FileMentionCompleter()
        doc = Document("@src/ma")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "src/main.py" in file_names
        assert "src/utils.py" not in file_names

    def test_backslash_normalized_to_forward_slash(self, tmp_path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "file.py").touch()

        completer = FileMentionCompleter()
        # Windows-style path
        doc = Document("@src\\")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        # Should normalize to forward slash
        assert "src/file.py" in file_names

    def test_skips_gitignored_files(self, tmp_path):
        (tmp_path / "tracked.py").touch()
        (tmp_path / "ignored.py").touch()

        def mock_is_ignored(path, cwd):
            return "ignored" in path

        completer = FileMentionCompleter()
        doc = Document("@")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", side_effect=mock_is_ignored):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "tracked.py" in file_names
        assert "ignored.py" not in file_names

    def test_skips_hidden_files(self, tmp_path):
        (tmp_path / "visible.py").touch()
        (tmp_path / ".hidden.py").touch()

        completer = FileMentionCompleter()
        doc = Document("@")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "visible.py" in file_names
        assert ".hidden.py" not in file_names

    def test_limits_suggestions_to_50(self, tmp_path):
        # Create 100 files
        for i in range(100):
            (tmp_path / f"file{i:03d}.py").touch()

        completer = FileMentionCompleter()
        doc = Document("@")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        # Should limit to 50
        assert len(completions) <= 50

    def test_nonexistent_directory_no_completions(self, tmp_path):
        completer = FileMentionCompleter()
        doc = Document("@nonexistent/")
        with patch("os.getcwd", return_value=str(tmp_path)):
            completions = list(completer.get_completions(doc, Mock()))
        assert completions == []

    def test_at_sign_after_whitespace(self, tmp_path):
        (tmp_path / "file.py").touch()

        completer = FileMentionCompleter()
        doc = Document("check this @fi")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "file.py" in file_names

    def test_completion_start_position(self, tmp_path):
        (tmp_path / "config.py").touch()

        completer = FileMentionCompleter()
        doc = Document("@con")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        if completions:
            # Should replace "con" (3 chars), not including "@"
            assert completions[0].start_position == -3

    def test_oserror_handled_gracefully(self, tmp_path):
        completer = FileMentionCompleter()
        doc = Document("@")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("os.listdir", side_effect=OSError("Permission denied")):
                completions = list(completer.get_completions(doc, Mock()))
        # Should not crash, just return no completions
        assert completions == []

    def test_relative_path_display(self, tmp_path):
        # Test that displayed paths are relative to cwd
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "file.py").touch()

        completer = FileMentionCompleter()
        doc = Document("@sub/")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        # Display should use forward slashes and be relative
        file_names = [c.text for c in completions]
        assert "sub/file.py" in file_names

    def test_multiple_at_signs_uses_last(self, tmp_path):
        (tmp_path / "file.py").touch()

        completer = FileMentionCompleter()
        doc = Document("@first @fi")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        # Should complete from last @
        file_names = [c.text for c in completions]
        assert "file.py" in file_names


# ── ArcCompleter ─────────────────────────────────────────────────────

class TestArcCompleter:
    """Tests for the merged ArcCompleter."""

    def test_delegates_to_slash_completer(self):
        completer = ArcCompleter()
        doc = Document("/he")
        completions = list(completer.get_completions(doc, Mock()))
        cmd_names = [c.text for c in completions]
        assert "/help" in cmd_names

    def test_delegates_to_file_completer(self, tmp_path):
        (tmp_path / "file.py").touch()

        completer = ArcCompleter()
        doc = Document("@fi")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "file.py" in file_names

    def test_no_completions_for_plain_text(self, tmp_path):
        completer = ArcCompleter()
        doc = Document("just normal text")
        with patch("os.getcwd", return_value=str(tmp_path)):
            completions = list(completer.get_completions(doc, Mock()))
        assert completions == []

    def test_passes_custom_commands_to_slash_completer(self):
        custom = {
            "deploy": CustomCommand(
                name="deploy",
                description="Deploy",
                template="Deploy",
                source_path=".agents/commands/deploy.md",
            ),
        }
        completer = ArcCompleter(custom_commands=custom)
        doc = Document("/dep")
        completions = list(completer.get_completions(doc, Mock()))
        cmd_names = [c.text for c in completions]
        assert "/deploy" in cmd_names

    def test_slash_takes_precedence_over_at(self, tmp_path):
        # If text starts with /, should not trigger @ completion
        (tmp_path / "file.py").touch()

        completer = ArcCompleter()
        doc = Document("/plan @fi")
        # Even though @ is present, slash is at start
        completions = list(completer.get_completions(doc, Mock()))
        # Should complete slash commands, not files
        # But based on implementation, it checks startswith("/")
        # So it will delegate to slash completer
        cmd_names = [c.text for c in completions]
        # "/plan @fi" doesn't match any slash command fully
        # so might be empty or have partial matches
        # The key point is that file completion is NOT triggered
        file_names = [c.text for c in completions]
        assert "file.py" not in file_names

    def test_handles_empty_input(self):
        completer = ArcCompleter()
        doc = Document("")
        completions = list(completer.get_completions(doc, Mock()))
        assert completions == []

    def test_at_sign_in_middle_of_text(self, tmp_path):
        (tmp_path / "config.py").touch()

        completer = ArcCompleter()
        doc = Document("check @co")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("arc.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "config.py" in file_names