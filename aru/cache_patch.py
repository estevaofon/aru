"""Monkey-patch Agno's model layer to reduce token consumption.

Three optimizations:

1. **Tool result pruning** (ALL providers): After each tool execution, old tool
   results in the message list are truncated to a short summary. This prevents
   O(n²) token growth where each API call re-sends all previous tool results.

2. **Cache breakpoints** (Anthropic only): Marks the last 2 messages with
   cache_control for Anthropic's prompt caching.

3. **Per-call metrics** (ALL providers): Captures input/output tokens of the
   last API call (context window size), exposed via get_last_call_metrics().

These patches intercept Agno's internal loop so they work transparently
regardless of which provider is used.
"""

from __future__ import annotations

# Token-budget pruning (aligned with OpenCode's strategy):
# - Protect recent tool results within a token budget
# - Only prune if there's enough to free (avoid churn)
# - Walk backwards, protecting recent content first
# OpenCode uses 40K protect / 20K minimum; we use chars (~4 chars/token)
_PRUNE_PROTECT_CHARS = 160_000   # ~40K tokens — recent content always kept
_PRUNE_MINIMUM_CHARS = 80_000    # ~20K tokens — only prune if this much is freeable
_PRUNED_PLACEHOLDER = "[Old tool result cleared]"

# Last API call metrics (updated on every internal API call)
_last_call_input_tokens: int = 0
_last_call_output_tokens: int = 0
_last_call_cache_read: int = 0
_last_call_cache_write: int = 0


def get_last_call_metrics() -> tuple[int, int, int, int]:
    """Return (input, output, cache_read, cache_write) from the most recent API call."""
    return _last_call_input_tokens, _last_call_output_tokens, _last_call_cache_read, _last_call_cache_write


def _prune_tool_messages(messages):
    """Clear old tool result content using a token-budget approach.

    Walks backwards through messages, protecting recent content up to
    PRUNE_PROTECT_CHARS. Older tool results beyond that budget are replaced
    with a short placeholder. Only prunes if total freeable chars exceed
    PRUNE_MINIMUM_CHARS (avoids unnecessary churn on small conversations).

    Aligned with OpenCode's strategy: budget-based, not fixed-N.
    """
    # Collect tool message indices and their content sizes
    tool_indices = []
    for i, msg in enumerate(messages):
        if getattr(msg, "role", None) == "tool":
            content = getattr(msg, "content", None)
            content_len = len(str(content)) if content is not None else 0
            tool_indices.append((i, content_len))

    if not tool_indices:
        return

    # Walk backwards, accumulating protected chars
    protected_chars = 0
    prune_candidates = []  # (index, content_len) of messages outside protection

    for idx, content_len in reversed(tool_indices):
        if protected_chars + content_len <= _PRUNE_PROTECT_CHARS:
            protected_chars += content_len
        else:
            prune_candidates.append((idx, content_len))

    # Only prune if there's enough to free
    freeable = sum(cl for _, cl in prune_candidates)
    if freeable < _PRUNE_MINIMUM_CHARS:
        return

    # Replace old tool results with placeholder
    for idx, _ in prune_candidates:
        msg = messages[idx]
        content = getattr(msg, "content", None)
        if content is None:
            continue
        # Skip if already pruned
        if str(content) == _PRUNED_PLACEHOLDER:
            continue
        try:
            msg.content = _PRUNED_PLACEHOLDER
            if hasattr(msg, "compressed_content"):
                msg.compressed_content = None
        except (AttributeError, TypeError):
            pass


def apply_cache_patch():
    """Apply all patches to reduce Agno's token consumption."""
    _patch_tool_result_pruning()
    _patch_claude_cache_breakpoints()
    _patch_per_call_metrics()


def _patch_tool_result_pruning():
    """Patch format_function_call_results to prune old tool results.

    This is called after each tool execution, right before the next API call.
    Works for ALL providers (Claude, OpenAI, Qwen, etc.) since it patches
    the base Model class.
    """
    from agno.models.base import Model

    _original_format_results = Model.format_function_call_results

    def _patched_format_results(self, messages, function_call_results, **kwargs):
        # First: prune old tool results already in messages
        _prune_tool_messages(messages)
        # Then: add new results normally
        return _original_format_results(self, messages, function_call_results, **kwargs)

    Model.format_function_call_results = _patched_format_results


def _patch_claude_cache_breakpoints():
    """Patch Claude's format_messages to add cache breakpoints.

    Marks the last 2 messages with cache_control for Anthropic's prompt
    caching. Non-Anthropic providers ignore these fields.
    """
    try:
        import agno.utils.models.claude as claude_utils
    except ImportError:
        return

    _original_format = claude_utils.format_messages

    def _patched_format_messages(messages, compress_tool_results=False):
        chat_messages, system_message = _original_format(
            messages, compress_tool_results=compress_tool_results
        )

        if not chat_messages:
            return chat_messages, system_message

        # Add cache_control to last 2 messages
        cache_marker = {"type": "ephemeral"}
        marked = 0
        for msg in reversed(chat_messages):
            if marked >= 2:
                break
            content = msg.get("content")
            if isinstance(content, list) and content:
                last_item = content[-1]
                if isinstance(last_item, dict):
                    last_item["cache_control"] = cache_marker
                    marked += 1
                elif hasattr(last_item, "type"):
                    try:
                        as_dict = last_item.model_dump() if hasattr(last_item, "model_dump") else dict(last_item)
                        as_dict["cache_control"] = cache_marker
                        content[-1] = as_dict
                        marked += 1
                    except Exception:
                        pass
            elif isinstance(content, str):
                msg["content"] = [{"type": "text", "text": content, "cache_control": cache_marker}]
                marked += 1

        return chat_messages, system_message

    claude_utils.format_messages = _patched_format_messages


def _patch_per_call_metrics():
    """Patch accumulate_model_metrics to capture per-API-call token counts.

    After each internal API call, Agno calls this function to sum tokens
    into RunMetrics. We intercept it to snapshot the last call's tokens,
    giving us the actual context window size (comparable to OpenCode/Claude Code).
    """
    from agno.metrics import accumulate_model_metrics as _original_accumulate

    import agno.metrics as _metrics_module

    def _patched_accumulate(model_response, model, model_type, run_metrics=None):
        global _last_call_input_tokens, _last_call_output_tokens
        global _last_call_cache_read, _last_call_cache_write
        usage = getattr(model_response, "response_usage", None)
        if usage is not None:
            _last_call_input_tokens = getattr(usage, "input_tokens", 0) or 0
            _last_call_output_tokens = getattr(usage, "output_tokens", 0) or 0
            _last_call_cache_read = getattr(usage, "cache_read_tokens", 0) or 0
            _last_call_cache_write = getattr(usage, "cache_write_tokens", 0) or 0
        return _original_accumulate(model_response, model, model_type, run_metrics)

    _metrics_module.accumulate_model_metrics = _patched_accumulate

    # Also patch the reference in base.py since it may have imported directly
    try:
        import agno.models.base as _base_module
        _base_module.accumulate_model_metrics = _patched_accumulate
    except (ImportError, AttributeError):
        pass
