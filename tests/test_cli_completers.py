"""Unit tests for aru.cli completers — SlashCommandCompleter, FileMentionCompleter, AruCompleter."""

import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from prompt_toolkit.document import Document

from agno.media import Image

from aru.cli import (
    SlashCommandCompleter,
    FileMentionCompleter,
    AruCompleter,
    SLASH_COMMANDS,
    _extract_agent_mention,
    _resolve_mentions,
)
from aru.completers import _IMAGE_EXTENSIONS
from aru.config import CustomAgent, CustomCommand


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
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
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
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "config.py" in file_names
        assert "main.py" not in file_names

    def test_case_insensitive_matching(self, tmp_path):
        (tmp_path / "Config.py").touch()

        completer = FileMentionCompleter()
        doc = Document("@co")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "Config.py" in file_names

    def test_includes_directories_with_slash(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "mydir").mkdir()

        completer = FileMentionCompleter()
        doc = Document("@")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
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
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
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
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
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
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
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
            with patch("aru.tools.gitignore.is_ignored", side_effect=mock_is_ignored):
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
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
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
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
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
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "file.py" in file_names

    def test_completion_start_position(self, tmp_path):
        (tmp_path / "config.py").touch()

        completer = FileMentionCompleter()
        doc = Document("@con")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
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
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        # Display should use forward slashes and be relative
        file_names = [c.text for c in completions]
        assert "sub/file.py" in file_names

    def test_multiple_at_signs_uses_last(self, tmp_path):
        (tmp_path / "file.py").touch()

        completer = FileMentionCompleter()
        doc = Document("@first @fi")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        # Should complete from last @
        file_names = [c.text for c in completions]
        assert "file.py" in file_names


# ── AruCompleter ─────────────────────────────────────────────────────

class TestAruCompleter:
    """Tests for the merged AruCompleter."""

    def test_delegates_to_slash_completer(self):
        completer = AruCompleter()
        doc = Document("/he")
        completions = list(completer.get_completions(doc, Mock()))
        cmd_names = [c.text for c in completions]
        assert "/help" in cmd_names

    def test_delegates_to_file_completer(self, tmp_path):
        (tmp_path / "file.py").touch()

        completer = AruCompleter()
        doc = Document("@fi")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "file.py" in file_names

    def test_no_completions_for_plain_text(self, tmp_path):
        completer = AruCompleter()
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
        completer = AruCompleter(custom_commands=custom)
        doc = Document("/dep")
        completions = list(completer.get_completions(doc, Mock()))
        cmd_names = [c.text for c in completions]
        assert "/deploy" in cmd_names

    def test_slash_takes_precedence_over_at(self, tmp_path):
        # If text starts with /, should not trigger @ completion
        (tmp_path / "file.py").touch()

        completer = AruCompleter()
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
        completer = AruCompleter()
        doc = Document("")
        completions = list(completer.get_completions(doc, Mock()))
        assert completions == []

    def test_at_sign_in_middle_of_text(self, tmp_path):
        (tmp_path / "config.py").touch()

        completer = AruCompleter()
        doc = Document("check @co")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        file_names = [c.text for c in completions]
        assert "config.py" in file_names


# ── @agent mention completions ──────────────────────────────────────


class TestAgentMentionCompleter:
    """Tests for @agent autocomplete in FileMentionCompleter."""

    def _make_agent(self, name="test", desc="Test agent", mode="subagent"):
        return CustomAgent(
            name=name, description=desc, system_prompt="prompt",
            source_path="/fake.md", mode=mode,
        )

    def test_suggests_agent_names(self, tmp_path):
        agents = {"reviewer": self._make_agent("Reviewer", "Review code")}
        completer = FileMentionCompleter(agents)
        doc = Document("@rev")
        with patch("os.getcwd", return_value=str(tmp_path)):
            completions = list(completer.get_completions(doc, Mock()))
        names = [c.text for c in completions]
        assert "reviewer" in names

    def test_no_match_returns_empty_agents(self, tmp_path):
        agents = {"reviewer": self._make_agent("Reviewer", "Review code")}
        completer = FileMentionCompleter(agents)
        doc = Document("@xyz")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))
        names = [c.text for c in completions]
        assert "reviewer" not in names

    def test_agent_and_file_both_suggested(self, tmp_path):
        (tmp_path / "readme.txt").touch()
        agents = {"reader": self._make_agent("Reader", "Read files")}
        completer = FileMentionCompleter(agents)
        doc = Document("@rea")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))
        names = [c.text for c in completions]
        assert "reader" in names
        assert "readme.txt" in names


# ── _extract_agent_mention ──────────────────────────────────────────


class TestExtractAgentMention:
    """Tests for @agent detection in user input."""

    def _make_agents(self):
        return {
            "reviewer": CustomAgent(
                name="Reviewer", description="Review", system_prompt="p",
                source_path="/f.md", mode="primary",
            ),
            "builder": CustomAgent(
                name="Builder", description="Build", system_prompt="p",
                source_path="/f.md", mode="subagent",
            ),
        }

    def test_detects_agent_at_start(self):
        agents = self._make_agents()
        result = _extract_agent_mention("@reviewer check this code", agents)
        assert result == ("reviewer", "@reviewer check this code")

    def test_detects_agent_in_middle(self):
        agents = self._make_agents()
        result = _extract_agent_mention("use @reviewer to check this", agents)
        assert result == ("reviewer", "use @reviewer to check this")

    def test_returns_none_for_unknown_agent(self):
        agents = self._make_agents()
        assert _extract_agent_mention("@unknown do something", agents) is None

    def test_returns_none_for_no_mention(self):
        agents = self._make_agents()
        assert _extract_agent_mention("just a regular message", agents) is None

    def test_works_with_no_message_body(self):
        agents = self._make_agents()
        result = _extract_agent_mention("@builder", agents)
        assert result == ("builder", "@builder")

    def test_case_insensitive(self):
        agents = self._make_agents()
        result = _extract_agent_mention("@Reviewer check code", agents)
        assert result == ("reviewer", "@Reviewer check code")

    def test_agent_mixed_with_file_mentions(self):
        agents = self._make_agents()
        result = _extract_agent_mention("use @reviewer to check @main.py", agents)
        assert result == ("reviewer", "use @reviewer to check @main.py")

    def test_agent_not_matched_if_attached_to_word(self):
        agents = self._make_agents()
        # @reviewer preceded by non-whitespace should not match
        assert _extract_agent_mention("email@reviewer", agents) is None


# ── Image mention support ──────────────────────────────────────────


class TestImageMentions:
    """Tests for image file detection in @mentions."""

    def test_resolve_mentions_returns_mention_result(self, tmp_path):
        mr = _resolve_mentions("hello", str(tmp_path))
        assert mr.text == "hello"
        assert mr.count == 0
        assert mr.images == []
        assert mr.file_messages == []

    def test_resolve_mentions_image_file(self, tmp_path):
        img = tmp_path / "screenshot.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        mr = _resolve_mentions("analyze @screenshot.png", str(tmp_path))
        assert mr.count == 1
        assert len(mr.images) == 1
        assert isinstance(mr.images[0], Image)
        assert mr.images[0].id == "screenshot.png"
        # Image content should NOT be in file_messages
        assert len(mr.file_messages) == 0

    def test_resolve_mentions_mixed_files_and_images(self, tmp_path):
        (tmp_path / "code.py").write_text("print('hello')", encoding="utf-8")
        (tmp_path / "diagram.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        mr = _resolve_mentions("review @code.py and @diagram.jpg", str(tmp_path))
        assert mr.count == 2
        assert len(mr.images) == 1
        assert mr.images[0].id == "diagram.jpg"
        # Text file content is emitted as structured tool_result blocks
        all_content = " ".join(
            b.get("content", "")
            for m in mr.file_messages
            if m["role"] == "tool"
            for b in m["content"]
            if b.get("type") == "tool_result"
        )
        assert "print('hello')" in all_content

    def test_resolve_mentions_multiple_images(self, tmp_path):
        (tmp_path / "a.png").write_bytes(b"\x89PNG" + b"\x00" * 100)
        (tmp_path / "b.webp").write_bytes(b"RIFF" + b"\x00" * 100)

        mr = _resolve_mentions("compare @a.png @b.webp", str(tmp_path))
        assert mr.count == 2
        assert len(mr.images) == 2

    def test_resolve_mentions_image_too_large(self, tmp_path):
        img = tmp_path / "huge.png"
        # Write just over the 20MB limit header
        img.write_bytes(b"\x89PNG" + b"\x00" * (20 * 1024 * 1024 + 1))

        mr = _resolve_mentions("analyze @huge.png", str(tmp_path))
        assert mr.count == 0
        assert len(mr.images) == 0

    def test_resolve_mentions_all_image_extensions(self, tmp_path):
        for ext in _IMAGE_EXTENSIONS:
            fname = f"test{ext}"
            (tmp_path / fname).write_bytes(b"\x00" * 100)

        mentions = " ".join(f"@test{ext}" for ext in _IMAGE_EXTENSIONS)
        mr = _resolve_mentions(mentions, str(tmp_path))
        assert len(mr.images) == len(_IMAGE_EXTENSIONS)

    def test_image_completer_shows_image_metadata(self, tmp_path):
        (tmp_path / "photo.png").touch()
        (tmp_path / "code.py").touch()

        completer = FileMentionCompleter()
        doc = Document("@")
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("aru.tools.gitignore.is_ignored", return_value=False):
                completions = list(completer.get_completions(doc, Mock()))

        by_name = {c.text: c for c in completions}
        assert "photo.png" in by_name
        assert "code.py" in by_name
        # Image should have "image" in metadata
        photo_meta = str(by_name["photo.png"].display_meta)
        assert "image" in photo_meta
        # Code file should NOT have "image" in metadata
        code_meta = str(by_name["code.py"].display_meta)
        assert "image" not in code_meta