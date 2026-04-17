"""Tests for the stop_reason capture patch in cache_patch.

The patch wraps Agno's Anthropic and OpenAI parsers so the runner can tell
when a response was truncated at max_tokens. We drive the patched parsers
directly with fake response objects so we don't need a network call.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aru import cache_patch
from aru.cache_patch import (
    get_last_stop_reason,
    reset_last_stop_reason,
)


@pytest.fixture(autouse=True)
def ensure_patched():
    """Apply the cache patch exactly once and reset the cached value between tests."""
    cache_patch.apply_cache_patch()
    reset_last_stop_reason()
    yield
    reset_last_stop_reason()


class TestAnthropicStopReason:
    """Anthropic's MessageStopEvent and final Message both carry stop_reason."""

    def test_streaming_max_tokens_captured(self):
        from agno.models.anthropic import claude as claude_mod

        # Fake a MessageStopEvent-shaped object: the patch inspects
        # `response.message.stop_reason`.
        message = SimpleNamespace(stop_reason="max_tokens", content=[], usage=None)
        event = SimpleNamespace(message=message)

        claude_instance = claude_mod.Claude.__new__(claude_mod.Claude)
        claude_instance.__dict__.update({"context_management": None, "skills": False})

        # Call the patched delta parser. The underlying original runs on the
        # real event shape; here we exercise just the side-effect path.
        try:
            claude_mod.Claude._parse_provider_response_delta(claude_instance, event)
        except Exception:
            # Original parser may fail on the synthetic object — that's fine,
            # the patch still records stop_reason as a side effect.
            pass

        assert get_last_stop_reason() == "max_tokens"

    def test_streaming_end_turn_captured(self):
        from agno.models.anthropic import claude as claude_mod

        message = SimpleNamespace(stop_reason="end_turn", content=[], usage=None)
        event = SimpleNamespace(message=message)

        claude_instance = claude_mod.Claude.__new__(claude_mod.Claude)
        claude_instance.__dict__.update({"context_management": None, "skills": False})

        try:
            claude_mod.Claude._parse_provider_response_delta(claude_instance, event)
        except Exception:
            pass

        assert get_last_stop_reason() == "end_turn"


class TestNormalization:
    """OpenAI's `length` is the same event as Anthropic's `max_tokens`; runners
    should only have to check one value."""

    def test_length_normalized_to_max_tokens(self):
        cache_patch._record_stop_reason("length")
        assert get_last_stop_reason() == "max_tokens"

    def test_stop_normalized_to_end_turn(self):
        cache_patch._record_stop_reason("stop")
        assert get_last_stop_reason() == "end_turn"

    def test_tool_calls_normalized_to_tool_use(self):
        cache_patch._record_stop_reason("tool_calls")
        assert get_last_stop_reason() == "tool_use"

    def test_gemini_allcaps_normalized(self):
        cache_patch._record_stop_reason("MAX_TOKENS")
        assert get_last_stop_reason() == "max_tokens"

    def test_unknown_reason_passes_through(self):
        cache_patch._record_stop_reason("refusal")
        assert get_last_stop_reason() == "refusal"

    def test_none_and_empty_are_ignored(self):
        cache_patch._record_stop_reason("end_turn")
        cache_patch._record_stop_reason(None)
        assert get_last_stop_reason() == "end_turn", "None must not overwrite"
        cache_patch._record_stop_reason("")
        assert get_last_stop_reason() == "end_turn", "empty must not overwrite"


class TestResetBetweenTurns:
    def test_reset_clears_cached_value(self):
        cache_patch._record_stop_reason("max_tokens")
        assert get_last_stop_reason() == "max_tokens"
        reset_last_stop_reason()
        assert get_last_stop_reason() is None
