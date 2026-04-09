"""Structured content blocks for session.history.

Aru stores conversation history as a list of messages where each item has
a role and a list of *content blocks*, inspired by Anthropic's native
content-block format. This allows tool_use / tool_result pairs to survive
across turns, which the old flat-text history could not represent.

Block shapes:
    {"type": "text",        "text": str}
    {"type": "tool_use",    "id": str, "name": str, "input": dict}
    {"type": "tool_result", "tool_use_id": str, "content": str}

This module provides:
- Constructors for each block type.
- Helpers to convert flat strings <-> block lists (for backward compatibility
  with old session JSON files).
- A translator that turns a block-shaped history into Agno `Message` objects,
  which are what the runner passes to `agent.arun()`.
- Utility predicates used by context pruning.

Agno's Message schema is *not* the same as our block format — Agno expects
`role="assistant"` messages to carry tool calls via `message.tool_calls`
(not content blocks) and tool results via `role="tool"` messages with a
`tool_call_id`. The translator in `to_agno_messages` handles that mapping.
"""

from __future__ import annotations

import json
from typing import Any, Iterable


Block = dict[str, Any]
HistoryItem = dict[str, Any]  # {"role": str, "content": list[Block]}


# ── Block constructors ────────────────────────────────────────────────

def text_block(text: str) -> Block:
    return {"type": "text", "text": text}


def tool_use_block(tool_id: str, name: str, tool_input: dict | None) -> Block:
    return {
        "type": "tool_use",
        "id": tool_id,
        "name": name,
        "input": tool_input or {},
    }


def tool_result_block(tool_use_id: str, content: str) -> Block:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }


# ── Block predicates ──────────────────────────────────────────────────

def is_text(block: Block) -> bool:
    return block.get("type") == "text"


def is_tool_use(block: Block) -> bool:
    return block.get("type") == "tool_use"


def is_tool_result(block: Block) -> bool:
    return block.get("type") == "tool_result"


def block_char_len(block: Block) -> int:
    """Approximate char length of a block (for token estimation / pruning)."""
    btype = block.get("type")
    if btype == "text":
        return len(block.get("text", ""))
    if btype == "tool_use":
        # Account for name + JSON-serialized input (approximate)
        name = block.get("name", "")
        try:
            inp = json.dumps(block.get("input") or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            inp = str(block.get("input") or "")
        return len(name) + len(inp) + 20  # 20 for structural overhead
    if btype == "tool_result":
        return len(str(block.get("content", "")))
    return 0


def item_char_len(item: HistoryItem) -> int:
    content = item.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(block_char_len(b) for b in content)
    return 0


# ── Backward compat with flat-text history ───────────────────────────

def coerce_content(content: Any) -> list[Block]:
    """Normalize any content value into a list of blocks.

    - str → [text_block(str)]
    - list of blocks → as-is (with basic validation)
    - list of strings → each wrapped in text_block
    - None → []
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [text_block(content)] if content else []
    if isinstance(content, list):
        out: list[Block] = []
        for item in content:
            if isinstance(item, dict) and "type" in item:
                out.append(item)
            elif isinstance(item, str):
                if item:
                    out.append(text_block(item))
        return out
    # Unknown type — stringify as a last resort
    return [text_block(str(content))]


def coerce_history_item(item: HistoryItem) -> HistoryItem:
    """Coerce a single history item to block-shaped form (idempotent)."""
    return {"role": item["role"], "content": coerce_content(item.get("content"))}


def coerce_history(history: list[HistoryItem]) -> list[HistoryItem]:
    """Coerce an entire history list to block-shaped form."""
    return [coerce_history_item(m) for m in history]


# ── String projection (for display, title, logs, compactor input) ────

def item_text(item: HistoryItem) -> str:
    """Best-effort flat-text projection of a history item.

    Concatenates text blocks, and represents tool_use/tool_result blocks as
    bracketed placeholders so the compactor (and any code that still needs
    a string) has a readable summary.
    """
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            name = block.get("name", "?")
            inp = block.get("input") or {}
            try:
                inp_preview = json.dumps(inp, ensure_ascii=False)
            except (TypeError, ValueError):
                inp_preview = str(inp)
            if len(inp_preview) > 200:
                inp_preview = inp_preview[:200] + "..."
            parts.append(f"[tool call: {name}({inp_preview})]")
        elif btype == "tool_result":
            result = str(block.get("content", ""))
            if len(result) > 500:
                result = result[:500] + f"... [+{len(result) - 500} chars]"
            parts.append(f"[tool result: {result}]")
    return "\n".join(p for p in parts if p)


# ── Iteration helpers ────────────────────────────────────────────────

def iter_blocks(history: Iterable[HistoryItem]):
    """Yield (item_index, block_index, block) across the whole history."""
    for i, item in enumerate(history):
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for j, block in enumerate(content):
            yield i, j, block


def tool_use_ids_in_item(item: HistoryItem) -> list[str]:
    """Return all tool_use ids in an assistant item's content."""
    content = item.get("content")
    if not isinstance(content, list):
        return []
    return [b["id"] for b in content if is_tool_use(b) and "id" in b]


# ── Translate block history → Agno Message objects ───────────────────

def to_agno_messages(history: list[HistoryItem]) -> list:
    """Convert block-shaped history into Agno `Message` objects.

    - user items: text blocks concatenated into `content` string
    - assistant items: text blocks concatenated into `content` string, and
      tool_use blocks promoted to `tool_calls` (Agno's schema)
    - tool items: one Message per tool_result block with `role="tool"` and
      `tool_call_id` populated (Agno maps this to Anthropic's user/tool_result)

    This function is the single translation layer between Aru's storage
    format and the runtime format Agno's Claude adapter expects (see
    `.venv/Lib/site-packages/agno/utils/models/claude.py:334-358`).
    """
    from agno.models.message import Message  # local import to avoid cycles

    out: list[Message] = []
    for item in history:
        role = item.get("role", "user")
        blocks = item.get("content") or []
        if isinstance(blocks, str):
            blocks = [text_block(blocks)]

        if role == "user":
            text_parts = [b.get("text", "") for b in blocks if is_text(b)]
            tool_result_blocks = [b for b in blocks if is_tool_result(b)]

            # Tool results must be emitted as separate `role="tool"` Messages
            for tr in tool_result_blocks:
                out.append(
                    Message(
                        role="tool",
                        content=str(tr.get("content", "")),
                        tool_call_id=tr.get("tool_use_id", ""),
                        from_history=True,
                    )
                )
            if text_parts:
                out.append(
                    Message(
                        role="user",
                        content="\n".join(p for p in text_parts if p),
                        from_history=True,
                    )
                )

        elif role == "assistant":
            text_parts = [b.get("text", "") for b in blocks if is_text(b)]
            tool_calls = []
            for b in blocks:
                if not is_tool_use(b):
                    continue
                tool_calls.append(
                    {
                        "id": b.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": b.get("name", ""),
                            "arguments": json.dumps(b.get("input") or {}),
                        },
                    }
                )
            msg = Message(
                role="assistant",
                content="\n".join(p for p in text_parts if p) or None,
                tool_calls=tool_calls or None,
                from_history=True,
            )
            out.append(msg)

        elif role == "tool":
            # Explicit tool-role items (we don't produce these ourselves but
            # support them for forward compat with loaded sessions).
            for tr in blocks:
                if not is_tool_result(tr):
                    continue
                out.append(
                    Message(
                        role="tool",
                        content=str(tr.get("content", "")),
                        tool_call_id=tr.get("tool_use_id", ""),
                        from_history=True,
                    )
                )
        # Unknown roles silently dropped

    return out
