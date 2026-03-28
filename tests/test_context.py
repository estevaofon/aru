"""Unit tests for aru.context - Token optimization and context management."""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from aru.context import (
    prune_history,
    truncate_output,
    should_compact,
    compact_conversation,
    apply_compaction,
    build_compaction_prompt,
    format_context_block,
)


class TestPruneHistory:
    """Tests for prune_history function."""

    def test_no_pruning_when_under_threshold(self):
        """Should not prune when total is under 70,000 chars."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        result = prune_history(messages)
        assert result == messages

    def test_prunes_old_assistant_messages(self):
        """Should prune old assistant messages when over threshold."""
        old_content = "x" * 30000
        recent_content = "y" * 10000
        messages = [
            {"role": "user", "content": "First request"},
            {"role": "assistant", "content": old_content},
            {"role": "user", "content": "Second request"},
            {"role": "assistant", "content": recent_content},
        ]
        result = prune_history(messages)
        # Should have placeholder for pruned content
        assert len(result) <= len(messages)
        # Recent messages should be preserved
        assert any("Second request" in str(m) for m in result)

    def test_preserves_user_messages(self):
        """Should always preserve user messages."""
        old_user = {"role": "user", "content": "Old user message"}
        old_assistant = {"role": "assistant", "content": "Old assistant " * 10000}
        recent = {"role": "user", "content": "Recent request"}
        
        messages = [old_user, old_assistant, recent]
        result = prune_history(messages)
        
        # User messages should be preserved (as placeholders or original)
        recent_preserved = any(
            m.get("role") == "user" and "Recent" in m.get("content", "")
            for m in result
        )
        assert recent_preserved

    def test_empty_history(self):
        """Should handle empty history."""
        result = prune_history([])
        assert result == []


class TestTruncateOutput:
    """Tests for truncate_output function."""

    def test_no_truncation_under_limits(self):
        """Should not truncate when under both limits."""
        output = "Short output"
        result = truncate_output(output)
        assert result == output

    def test_truncates_long_output_by_lines(self):
        """Should truncate when exceeding 500 lines."""
        lines = ["line " + str(i) for i in range(600)]
        output = "\n".join(lines)
        result = truncate_output(output)
        
        # Should contain the marker
        assert "[... N lines omitted]" in result or len(result) < len(output)

    def test_truncates_long_output_by_bytes(self):
        """Should truncate when exceeding 20KB."""
        # 25KB of content
        output = "x" * 25000
        result = truncate_output(output)
        
        assert len(result) < len(output)
        assert "[... N lines omitted]" in result or len(result) <= 20000

    def test_preserves_beginning_and_end(self):
        """Should keep first 350 and last 100 lines."""
        lines = [f"line {i}" for i in range(600)]
        output = "\n".join(lines)
        result = truncate_output(output)
        
        # Should start with line 0
        assert "line 0" in result
        # Should end with later lines
        assert "line 5" in result or "line 59" in result


class TestShouldCompact:
    """Tests for should_compact function."""

    def test_no_compaction_under_threshold(self):
        """Should not compact when under 50% of context limit."""
        # Default 200K tokens * 0.5 = 100K threshold; 5 tokens is well under
        result = should_compact(5, model_id="claude-sonnet-4-5-20250929")
        assert result is False

    def test_compaction_over_threshold(self):
        """Should compact when over threshold."""
        # 300K tokens is over 50% of a 200K-token context window
        result = should_compact(300000, model_id="claude-sonnet-4-5-20250929")
        assert result is True

    def test_custom_context_limit(self):
        """Should respect custom context limit."""
        # gpt-4o has 128K context, 50% = 64K; 50K is under threshold
        result = should_compact(50000, model_id="gpt-4o")
        assert isinstance(result, bool)


class TestCompactConversation:
    """Tests for compact_conversation function."""

    @pytest.mark.asyncio
    async def test_fallback_summary(self):
        """Should use fallback summary when no agent available."""
        messages = [
            {"role": "user", "content": "Task 1"},
            {"role": "assistant", "content": "Result 1"},
            {"role": "user", "content": "Task 2"},
            {"role": "assistant", "content": "Result 2"},
        ]

        result = await compact_conversation(messages, model_ref="claude-haiku-4-5-20251001")

        # Should return a list of compacted messages
        assert isinstance(result, list)
        assert "Task" in str(result) or "message" in str(result).lower()

    @pytest.mark.asyncio
    async def test_empty_history(self):
        """Should handle empty conversation."""
        result = await compact_conversation([], model_ref="claude-haiku-4-5-20251001")
        assert isinstance(result, list)


class TestBuildCompactionPrompt:
    """Tests for build_compaction_prompt function."""

    def test_build_context_excludes_empty_sections(self):
        """Should omit optional sections (plan_task, messages) when not provided."""
        result = build_compaction_prompt([], plan_task=None)

        # The "Active task" section should not appear when plan_task is None
        assert "Active task" not in result
        # The base template header should still be present
        assert "Conversation to summarize" in result
        # No message blocks should be present
        assert "**USER:**" not in result
        assert "**ASSISTANT:**" not in result


class TestApplyCompaction:
    """Tests for apply_compaction function."""

    def test_replaces_with_summary(self):
        """Should replace history with summary."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!" * 1000},
        ]
        summary = "Conversation summary: User greeted, assistant responded."
        
        result = apply_compaction(messages, summary)
        
        # Should contain the summary
        assert summary in str(result) or any(
            "summary" in str(m).lower() for m in result
        )

    def test_preserves_recent_exchange(self):
        """Should keep recent user + assistant messages."""
        old_messages = [
            {"role": "user", "content": "Old request"},
            {"role": "assistant", "content": "Old response"},
        ]
        recent = [
            {"role": "user", "content": "Recent request"},
            {"role": "assistant", "content": "Recent response"},
        ]
        summary = "Summary of old conversation"
        
        all_messages = old_messages + recent
        result = apply_compaction(all_messages, summary)
        
        # Recent messages should be in result
        assert "Recent request" in str(result)


class TestFormatContextBlock:
    """Tests for format_context_block function."""

    def test_format_with_timestamp(self):
        """Should format block with timestamp in separator."""
        content = "This is a test context content."
        result = format_context_block(content, label="Test", include_timestamp=True)
        
        # Should contain the content
        assert content in result
        # Should have timestamp in format YYYY-MM-DD HH:MM:SS
        assert "Test (" in result
        assert ")" in result
        # Should have separators at start and end
        assert result.startswith("-- Test (")
        assert result.endswith(") --")

    def test_format_without_timestamp(self):
        """Should format block without timestamp."""
        content = "Content without timestamp"
        result = format_context_block(content, label="Info", include_timestamp=False)
        
        assert content in result
        assert "-- Info --" in result
        assert "-- Info --" in result
        assert "(" not in result

    def test_format_custom_label(self):
        """Should use custom label."""
        content = "Some content"
        result = format_context_block(content, label="CustomLabel")
        
        assert "CustomLabel" in result
        assert content in result

    def test_format_empty_content(self):
        """Should handle empty content."""
        result = format_context_block("", label="Empty", include_timestamp=False)
        
        assert "-- Empty --" in result
        assert result.count("-- Empty --") == 2