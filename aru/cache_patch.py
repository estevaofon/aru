"""Monkey-patch Agno's model layer to reduce token consumption.

Two optimizations:

1. **Tool result pruning** (ALL providers): After each tool execution, old tool
   results in the message list are truncated to a short summary. This prevents
   O(n²) token growth where each API call re-sends all previous tool results.

2. **Cache breakpoints** (Anthropic only): Marks the last 2 messages with
   cache_control for Anthropic's prompt caching.

These patches intercept Agno's internal loop so they work transparently
regardless of which provider is used.
"""

from __future__ import annotations

# Max chars to keep from old tool results
_TOOL_RESULT_KEEP_CHARS = 200
# Number of recent tool results to keep in full
_KEEP_RECENT_RESULTS = 1


def _prune_tool_messages(messages):
    """Truncate old tool result content in the message list.

    Keeps only the last N tool results in full. Older ones are truncated
    to a short preview. This runs BEFORE each API call, so accumulated
    tool results don't bloat the context on every re-send.
    """
    # Find all tool message indices
    tool_indices = [
        i for i, msg in enumerate(messages)
        if getattr(msg, "role", None) == "tool"
    ]

    if len(tool_indices) <= _KEEP_RECENT_RESULTS:
        return

    # Prune all except the last N
    for idx in tool_indices[:-_KEEP_RECENT_RESULTS]:
        msg = messages[idx]
        content = getattr(msg, "content", None)
        if content is None:
            continue

        content_str = str(content)
        if len(content_str) <= _TOOL_RESULT_KEEP_CHARS:
            continue

        truncated = content_str[:_TOOL_RESULT_KEEP_CHARS] + "\n[...truncated]"
        try:
            msg.content = truncated
            if hasattr(msg, "compressed_content"):
                msg.compressed_content = None
        except (AttributeError, TypeError):
            pass


def apply_cache_patch():
    """Apply all patches to reduce Agno's token consumption."""
    _patch_tool_result_pruning()
    _patch_claude_cache_breakpoints()


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
