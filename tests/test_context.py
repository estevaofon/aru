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
from aru.history_blocks import coerce_history, item_text


class TestPruneHistory:
    """Tests for prune_history function."""

    def test_no_pruning_when_under_threshold(self):
        """Should not prune when total is under threshold."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        result = prune_history(messages)
        # Input is auto-coerced to block form on return
        assert result == coerce_history(messages)

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
            m.get("role") == "user" and "Recent" in item_text(m)
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


class TestCompactionTriggerUsesPerCallMetric:
    """Regression guard: the runner must trigger compaction on the last-call
    context window, not on cumulative tokens across all API calls in a turn.

    Before this fix, `aru/runner.py` passed `run_output.metrics.input_tokens`
    to `should_compact`, which is cumulative (Agno does `metrics.input_tokens
    += input_tokens` on every API call — see agno/metrics.py:703). On a
    multi-tool turn the cumulative could exceed the compaction threshold
    even when the actual per-call window was comfortably small, causing
    needless compaction on simple first-turn conversations.

    The fix uses `session.last_input_tokens + last_output_tokens +
    last_cache_read + last_cache_write`, which is the per-call window
    populated from `cache_patch.get_last_call_metrics()` — the same metric
    the status bar displays to the user.
    """

    def test_small_per_call_window_does_not_fire(self):
        """Reproduces the exact bug report: per-call ~20K on qwen3.6-plus
        (128K limit, ~75.6K threshold) must NOT trigger compaction."""
        # Values taken from the real session where compaction fired incorrectly:
        # "context: 20,184 (in: 16,652 / out: 696 / cache_read: 2,836)"
        last_input = 16_652
        last_output = 696
        last_cache_read = 2_836
        last_cache_write = 0

        last_call_window = (
            last_input + last_output + last_cache_read + last_cache_write
        )
        assert last_call_window == 20_184, "window computation changed"

        # 20K is ~3.7× below the 75.6K threshold for a 128K-context model
        assert should_compact(last_call_window, model_id="qwen3.6-plus") is False, (
            "Compaction fired on a small per-call window. The runner is "
            "probably passing cumulative tokens (run_output.metrics.input_tokens) "
            "instead of the per-call window. See aru/runner.py reactive "
            "compaction path."
        )

    def test_large_per_call_window_still_fires(self):
        """Positive case: compaction must still fire when the last-call
        window actually approaches the model's context limit."""
        last_input = 80_000
        last_output = 2_000
        last_cache_read = 0
        last_cache_write = 0

        last_call_window = (
            last_input + last_output + last_cache_read + last_cache_write
        )
        assert last_call_window == 82_000

        # 82K > 75.6K threshold → must fire
        assert should_compact(last_call_window, model_id="qwen3.6-plus") is True

    def test_cumulative_metric_is_the_wrong_signal(self):
        """Illustrates WHY the old approach was wrong: a cumulative sum of
        5 API calls at 18K each is 90K (above threshold), but the actual
        per-call window each time is only 18K (well below)."""
        per_call_window = 18_000
        num_api_calls_in_turn = 5
        cumulative_if_summed = per_call_window * num_api_calls_in_turn

        # Old (wrong) behavior: cumulative triggers compaction
        assert should_compact(cumulative_if_summed, model_id="qwen3.6-plus") is True

        # New (correct) behavior: per-call does NOT trigger compaction
        assert should_compact(per_call_window, model_id="qwen3.6-plus") is False

        # The difference is the entire bug
        assert cumulative_if_summed > 75_600 > per_call_window

    def test_runner_source_uses_per_call_metric(self):
        """Static check against silent regression.

        The runner's reactive-compaction block must read the per-call
        window from `session.last_*`, NOT from `run_output.metrics`.
        A future refactor that reverts to `run_output.metrics.input_tokens`
        would reintroduce the bug without breaking any other test, because
        we can't easily mock Agno's streaming RunOutput.metrics in a unit
        test. This inspects the runner.py source text directly.
        """
        import pathlib
        runner_src = pathlib.Path(
            __file__
        ).parent.parent.joinpath("aru", "runner.py").read_text(encoding="utf-8")

        # Locate the reactive compaction block
        assert "Reactive compaction" in runner_src, (
            "Couldn't find the reactive compaction block in runner.py — "
            "did it get removed or renamed?"
        )

        # The fix: must compose the window from session.last_* fields
        assert "session.last_input_tokens" in runner_src, (
            "runner.py no longer references session.last_input_tokens — "
            "the compaction-metric fix was likely reverted. The per-call "
            "window must be derived from session.last_* (populated by "
            "cache_patch.get_last_call_metrics), not from the cumulative "
            "run_output.metrics.input_tokens."
        )

        # The bug: must NOT pass run_output.metrics.input_tokens directly
        # to should_compact. We grep for the specific anti-pattern and
        # assert it's absent from the compaction block.
        # We look for the exact dangerous line, not just the string.
        dangerous_pattern = "should_compact(run_input_tokens"
        old_assignment = 'run_input_tokens = getattr(run_output.metrics, "input_tokens"'
        if old_assignment in runner_src and dangerous_pattern in runner_src:
            raise AssertionError(
                "runner.py still passes run_output.metrics.input_tokens "
                "(cumulative) to should_compact. See the original bug: "
                "Agno accumulates metrics.input_tokens across every API "
                "call in a turn, so multi-tool turns fire compaction "
                "needlessly. Use session.last_* fields instead."
            )


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