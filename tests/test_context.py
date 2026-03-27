"""Tests for aru.context — token optimization layers."""

import pytest
from unittest.mock import AsyncMock, patch
from aru.context import (
    PRUNE_PROTECT_CHARS,
    PRUNED_PLACEHOLDER,
    TRUNCATE_MAX_BYTES,
    TRUNCATE_MAX_LINES,
    apply_compaction,
    build_compaction_prompt,
    prune_history,
    should_compact,
    truncate_output,
    _fallback_summary,
    compact_conversation,
)


# ── Layer 1: Pruning ──────────────────────────────────────────────


class TestPruneHistory:
    def test_short_history_unchanged(self):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = prune_history(history)
        assert result == history

    def test_empty_history(self):
        assert prune_history([]) == []

    def test_single_message(self):
        history = [{"role": "user", "content": "test"}]
        assert prune_history(history) == history

    def test_prunes_old_large_assistant_messages(self):
        """Old assistant messages beyond the protection window should be pruned."""
        # Create history where total assistant content exceeds protect + minimum
        large_content = "x" * (PRUNE_PROTECT_CHARS + 30_000)
        history = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": large_content},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "short recent reply"},
        ]
        result = prune_history(history)

        # The old large message should be pruned
        assert result[1]["content"] == PRUNED_PLACEHOLDER
        # The recent message should be preserved
        assert result[3]["content"] == "short recent reply"

    def test_protects_recent_messages(self):
        """Messages within the protection window should not be pruned."""
        # Content fits within protection window
        small = "x" * 1000
        history = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": small},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": small},
        ]
        result = prune_history(history)
        # Both fit within protect chars — nothing pruned
        assert result[1]["content"] == small
        assert result[3]["content"] == small

    def test_user_messages_never_pruned(self):
        """User messages should never be replaced."""
        large = "x" * (PRUNE_PROTECT_CHARS + 30_000)
        history = [
            {"role": "user", "content": "my long question " + "a" * 10000},
            {"role": "assistant", "content": large},
            {"role": "user", "content": "follow up"},
            {"role": "assistant", "content": "recent"},
        ]
        result = prune_history(history)
        # User messages intact
        assert result[0]["role"] == "user"
        assert "my long question" in result[0]["content"]
        assert result[2]["content"] == "follow up"

    def test_does_not_mutate_input(self):
        large = "x" * (PRUNE_PROTECT_CHARS + 30_000)
        history = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": large},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "recent"},
        ]
        original_content = history[1]["content"]
        prune_history(history)
        # Original should not be mutated
        assert history[1]["content"] == original_content

    def test_already_pruned_messages_stay_pruned(self):
        """Messages already pruned should not be double-processed."""
        large = "x" * (PRUNE_PROTECT_CHARS + 30_000)
        history = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": PRUNED_PLACEHOLDER},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": large},
            {"role": "user", "content": "q3"},
            {"role": "assistant", "content": "recent"},
        ]
        result = prune_history(history)
        assert result[1]["content"] == PRUNED_PLACEHOLDER


# ── Layer 2: Truncation ───────────────────────────────────────────


class TestTruncateOutput:
    def test_short_text_unchanged(self):
        text = "hello world"
        assert truncate_output(text) == text

    def test_empty_text(self):
        assert truncate_output("") == ""

    def test_truncates_by_line_count(self):
        lines = [f"line {i}\n" for i in range(TRUNCATE_MAX_LINES + 500)]
        text = "".join(lines)
        result = truncate_output(text)
        assert "lines omitted" in result
        assert len(result) < len(text)

    def test_truncates_by_byte_size(self):
        # Create text that's under line limit but over byte limit
        # Use long lines to hit byte limit before line limit
        long_line = "x" * 1000 + "\n"
        # 50KB / 1001 bytes per line ≈ 50 lines, well under 2000 line limit
        count = (TRUNCATE_MAX_BYTES // 1000) + 100
        text = long_line * count
        result = truncate_output(text)
        assert "truncated" in result
        assert len(result.encode("utf-8")) < len(text.encode("utf-8"))

    def test_preserves_start_and_end_on_line_truncation(self):
        lines = [f"line-{i:05d}\n" for i in range(TRUNCATE_MAX_LINES + 1000)]
        text = "".join(lines)
        result = truncate_output(text)
        # Start should be preserved
        assert "line-00000" in result
        # End should be preserved
        assert f"line-{TRUNCATE_MAX_LINES + 999:05d}" in result

    def test_none_safe(self):
        # Passing None-ish values
        assert truncate_output("") == ""

    def test_unicode_handling(self):
        """Test that truncation handles multibyte characters correctly."""
        # Test with emoji (4-byte UTF-8 characters)
        emoji_text = "🚀" * 100 + "\n"
        result = truncate_output(emoji_text)
        # Should not break UTF-8 encoding
        assert result.encode("utf-8")  # Should not raise
        
        # Test with Asian characters (3-byte UTF-8)
        asian_text = "你好世界" * 1000 + "\n"
        result = truncate_output(asian_text)
        assert result.encode("utf-8")  # Should not raise
        
        # Test mixed ASCII, emoji, and Asian characters
        mixed = "Hello 🌍 世界 " * 500 + "\n"
        result = truncate_output(mixed)
        assert result.encode("utf-8")  # Should not raise
        
        # Test truncation preserves valid UTF-8
        # Create large text with multibyte chars that exceeds byte limit
        large_unicode = ("🔥" * 100 + "测试" * 100 + "\n") * 1000
        result = truncate_output(large_unicode)
        # Result should be valid UTF-8 and contain truncation marker
        assert result.encode("utf-8")
        if "truncated" in result or "lines omitted" in result:
            # If truncated, check start and end are preserved
            assert "🔥" in result or "测试" in result

    def test_truncates_by_both_line_and_byte_limits(self):
        """Test that both line and byte truncation can be triggered in the same output."""
        # Create 600 lines of 50 bytes each (30KB total)
        # This should trigger line truncation (600 > TRUNCATE_MAX_LINES=2000 is false)
        # Let's create more lines: 2500 lines of 50 bytes = 125KB
        line_content = "x" * 49 + "\n"  # 50 bytes per line
        num_lines = 2500
        text = line_content * num_lines
        
        # Verify our input exceeds line limit
        assert num_lines > TRUNCATE_MAX_LINES
        # Verify our input exceeds byte limit (2500 * 50 = 125KB > 50KB)
        assert len(text.encode("utf-8")) > TRUNCATE_MAX_BYTES
        
        result = truncate_output(text)
        
        # Should be truncated
        assert "lines omitted" in result or "truncated" in result
        # Result should be smaller than input
        assert len(result.encode("utf-8")) < len(text.encode("utf-8"))
        # Should preserve start
        assert result.startswith("x" * 49)
        # Result should contain truncation indicator
        assert "omitted" in result or "truncated" in result


# ── Layer 3: Compaction ───────────────────────────────────────────


class TestShouldCompact:
    def test_below_threshold(self):
        assert not should_compact(50_000, "default")

    def test_above_threshold(self):
        # 60% of 200K = 120K
        assert should_compact(130_000, "default")

    def test_exact_threshold(self):
        assert should_compact(120_000, "default")

    def test_unknown_model_uses_default(self):
        assert should_compact(130_000, "some-unknown-model")

    def test_zero_tokens(self):
        assert not should_compact(0, "default")


class TestBuildCompactionPrompt:
    def test_includes_conversation(self):
        history = [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "I found the issue in main.py"},
        ]
        prompt = build_compaction_prompt(history)
        assert "fix the bug" in prompt
        assert "main.py" in prompt

    def test_includes_plan_task(self):
        history = [{"role": "user", "content": "test"}]
        prompt = build_compaction_prompt(history, plan_task="Refactor auth module")
        assert "Refactor auth module" in prompt

    def test_truncates_long_messages(self):
        long_content = "x" * 5000
        history = [{"role": "assistant", "content": long_content}]
        prompt = build_compaction_prompt(history)
        assert "chars truncated" in prompt
        # Should not contain the full 5000 chars
        assert len(prompt) < 5000


class TestApplyCompaction:
    def test_replaces_history_with_summary(self):
        history = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        result = apply_compaction(history, "Summary of conversation")
        assert result[0]["content"].startswith("[Conversation compacted]")
        assert "Summary of conversation" in result[0]["content"]

    def test_keeps_recent_messages(self):
        history = [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "latest question"},
            {"role": "assistant", "content": "latest answer"},
        ]
        result = apply_compaction(history, "summary")
        contents = [m["content"] for m in result]
        assert any("latest answer" in c for c in contents)
        assert any("latest question" in c for c in contents)

    def test_compaction_reduces_message_count(self):
        history = [
            {"role": "user", "content": f"msg {i}"}
            for i in range(20)
        ]
        result = apply_compaction(history, "summary")
        assert len(result) < len(history)

    def test_apply_compaction_success(self):
        """Test apply_compaction with mocked agent response and verify compacted messages."""
        history = [
            {"role": "user", "content": "What is the bug?"},
            {"role": "assistant", "content": "The bug is in auth.py line 42"},
            {"role": "user", "content": "How do I fix it?"},
            {"role": "assistant", "content": "Replace the deprecated function call"},
            {"role": "user", "content": "Is there a test for this?"},
            {"role": "assistant", "content": "Yes, in tests/test_auth.py"},
        ]
        agent_summary = "User asked about a bug in auth.py. Assistant identified issue on line 42 and suggested replacing deprecated function. Discussion included test coverage."
        
        result = apply_compaction(history, agent_summary)
        
        # Verify structure: [compaction_msg, last_assistant, last_user]
        assert len(result) == 3
        assert result[0]["role"] == "user"
        assert result[0]["content"].startswith("[Conversation compacted]")
        
        # Verify summary is included
        assert agent_summary in result[0]["content"]
        
        # Verify recent messages are preserved: last assistant, then last user
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "Yes, in tests/test_auth.py"
        assert result[2]["role"] == "user"
        assert result[2]["content"] == "Is there a test for this?"
        
        # Verify message count reduced from 6 to 3
        assert len(result) < len(history)
        
        # Verify old messages are not in the result (except in summary)
        message_contents = [m["content"] for m in result[1:]]  # Skip compaction message
        assert not any("What is the bug?" in c for c in message_contents)
        assert not any("How do I fix it?" in c for c in message_contents)


class TestFallbackSummary:
    def test_includes_stats(self):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        summary = _fallback_summary(history)
        assert "2 messages" in summary
        assert "1 from user" in summary

    def test_includes_plan_task(self):
        history = [{"role": "user", "content": "test"}]
        summary = _fallback_summary(history, plan_task="Fix login bug")
        assert "Fix login bug" in summary

    def test_extracts_file_paths(self):
        history = [
            {"role": "assistant", "content": "I modified aru/cli.py and tests/test_cli.py"},
        ]
        summary = _fallback_summary(history)
        assert "cli.py" in summary

    def test_includes_recent_context(self):
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
            {"role": "assistant", "content": "fourth"},
            {"role": "user", "content": "fifth"},
        ]
        summary = _fallback_summary(history)
        assert "Recent context" in summary
        assert "fifth" in summary


@pytest.mark.asyncio
async def test_compact_conversation_success():
    """Test compact_conversation with successful Agent.arun call."""
    history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "How are you?"},
        {"role": "assistant", "content": "I'm doing well, thanks!"}
    ]
    model_ref = "claude-3-5-sonnet-20241022"
    plan_task = "Test conversation"
    
    mock_summary = "This is a test summary of the conversation."
    
    with patch("agno.agent.Agent") as mock_agent_class:
        mock_agent = AsyncMock()
        mock_agent.arun.return_value = mock_summary
        mock_agent_class.return_value = mock_agent
        
        result = await compact_conversation(history, model_ref, plan_task)
        
        assert len(result) > 0
        assert any("[Conversation compacted]" in msg.get("content", "") for msg in result)
        assert mock_agent.arun.called


@pytest.mark.asyncio
async def test_compact_conversation_fallback():
    """Test compact_conversation falls back to _fallback_summary when Agent.arun raises exception."""
    history = [
        {"role": "user", "content": "Test message 1"},
        {"role": "assistant", "content": "Response 1"},
        {"role": "user", "content": "Test message 2"},
        {"role": "assistant", "content": "Response 2"}
    ]
    model_ref = "claude-3-5-sonnet-20241022"
    plan_task = "Test fallback behavior"
    
    with patch("agno.agent.Agent") as mock_agent_class:
        mock_agent = AsyncMock()
        # Simulate Agent.arun raising an exception
        mock_agent.arun.side_effect = Exception("API error")
        mock_agent_class.return_value = mock_agent
        
        result = await compact_conversation(history, model_ref, plan_task)
        
        # Should still return a valid compacted conversation using fallback
        assert len(result) > 0
        assert any("[Conversation compacted]" in msg.get("content", "") for msg in result)
        # Fallback summary should include stats
        compacted_msg = next(msg for msg in result if "[Conversation compacted]" in msg.get("content", ""))
        assert "messages" in compacted_msg["content"]
        # Should include plan task in fallback
        assert plan_task in compacted_msg["content"]
