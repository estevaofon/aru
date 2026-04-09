"""Regression tests for the tool-result confabulation bug.

The original failure mode: Aru stored `session.history` as flat text
(`{role, content: str}`), which meant tool results never entered history
at all. The model saw a `[Tools]` text footer but could not see what the
tool returned, leading it to confabulate ("I didn't actually call the
tool") or hallucinate content it didn't have.

These tests lock in the structured-block history format and ensure:
1. Tool results survive across turns via the new block format.
2. The prune pipeline never leaves orphan tool_use/tool_result blocks.
3. String → block backward compatibility works for legacy saved sessions.
4. `to_agno_messages` reconstructs Agno Message objects that preserve
   the tool_call_id pairing Claude's API requires.
"""

from __future__ import annotations

import pytest

from aru.session import Session, SessionStore
from aru.history_blocks import (
    coerce_content,
    coerce_history,
    tool_use_block,
    tool_result_block,
    text_block,
    to_agno_messages,
    is_tool_use,
    is_tool_result,
)
from aru.context import prune_history


def _assistant_tool_turn(tu_id: str, name: str, tool_input: dict, text: str = "Running tool"):
    """Build an assistant message with text + tool_use block."""
    return {
        "role": "assistant",
        "content": [text_block(text), tool_use_block(tu_id, name, tool_input)],
    }


def _tool_result_turn(tu_id: str, result: str):
    """Build a tool-role message with a tool_result block."""
    return {
        "role": "tool",
        "content": [tool_result_block(tu_id, result)],
    }


class TestToolResultsPersist:
    """The core regression: tool results must be visible in history."""

    def test_tool_result_survives_across_turns(self):
        """After reading a file, the next turn's history must contain the result."""
        session = Session()
        # Turn 1: user asks, assistant reads, tool returns result
        session.add_message("user", "read foo.py")
        session.add_structured_message(
            "assistant",
            [
                text_block("Reading foo.py"),
                tool_use_block("tu_1", "read_file", {"path": "foo.py"}),
            ],
        )
        session.add_structured_message(
            "tool",
            [tool_result_block("tu_1", "def foo():\n    return 42")],
        )
        # Turn 2: user asks about the content
        session.add_message("user", "what does foo return?")

        # Rebuild agent input (mirrors runner.py:to_agno_messages path)
        messages = to_agno_messages(session.history)

        # The tool result content must be present in the rebuilt messages
        all_content = " ".join(
            str(m.content) for m in messages if m.content is not None
        )
        assert "return 42" in all_content, (
            "Tool result was not preserved across turns — "
            "the confabulation bug has regressed."
        )

    def test_tool_call_id_pairing_preserved(self):
        """Every tool_use in rebuilt messages must have a matching tool_result."""
        session = Session()
        session.add_message("user", "do the thing")
        session.add_structured_message(
            "assistant",
            [
                tool_use_block("tu_a", "bash", {"cmd": "ls"}),
                tool_use_block("tu_b", "read_file", {"path": "x"}),
            ],
        )
        session.add_structured_message(
            "tool",
            [
                tool_result_block("tu_a", "file1\nfile2"),
                tool_result_block("tu_b", "contents of x"),
            ],
        )

        messages = to_agno_messages(session.history)

        # Collect tool_call_ids from assistant messages and tool_call_ids
        # from tool messages — they must match as sets.
        assistant_ids: set[str] = set()
        for m in messages:
            if m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    assistant_ids.add(tc["id"])

        tool_result_ids: set[str] = {
            m.tool_call_id for m in messages
            if m.role == "tool" and m.tool_call_id
        }

        assert assistant_ids == tool_result_ids == {"tu_a", "tu_b"}, (
            f"Tool call ID pairing broken: assistant={assistant_ids}, "
            f"tool_results={tool_result_ids}"
        )


class TestBackwardCompat:
    """Old session JSONs stored flat-string content — must still load."""

    def test_coerce_flat_string_to_text_block(self):
        blocks = coerce_content("Hello world")
        assert blocks == [{"type": "text", "text": "Hello world"}]

    def test_coerce_empty_string_returns_empty_list(self):
        assert coerce_content("") == []

    def test_coerce_none_returns_empty_list(self):
        assert coerce_content(None) == []

    def test_coerce_history_with_legacy_string_content(self):
        legacy = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ]
        blocks = coerce_history(legacy)
        assert blocks[0]["content"] == [{"type": "text", "text": "Hi"}]
        assert blocks[1]["content"] == [{"type": "text", "text": "Hello"}]

    def test_from_dict_loads_legacy_session(self, tmp_path):
        """Saved sessions with flat-string content must still load."""
        import json
        legacy_data = {
            "session_id": "legacy1",
            "history": [
                {"role": "user", "content": "old message"},
                {"role": "assistant", "content": "old response"},
            ],
            "model_ref": "anthropic/claude-sonnet-4-5",
            "cwd": str(tmp_path),
            "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:00",
        }
        json_path = tmp_path / "legacy1.json"
        json_path.write_text(json.dumps(legacy_data), encoding="utf-8")

        store = SessionStore(base_dir=str(tmp_path))
        loaded = store.load("legacy1")

        assert loaded is not None
        assert len(loaded.history) == 2
        # Content should be block-shaped after loading
        assert isinstance(loaded.history[0]["content"], list)
        assert loaded.history[0]["content"][0]["type"] == "text"
        assert loaded.history[0]["content"][0]["text"] == "old message"


class TestPrunePreservesPairs:
    """Fix 6: pruning must never orphan tool_use / tool_result blocks."""

    def test_prune_drops_tool_pair_atomically(self):
        """An old tool_result whose content gets cleared must still keep
        its block (matching tool_use_id), so the tool_use/tool_result
        pair is never orphaned.

        Opencode-aligned budget: prune only counts tool_result content
        chars, so the history needs multiple large tool_result payloads
        to clear the 240K entry gate.
        """
        big_output = "old file line\n" * 8_000  # ~100K chars per result
        history = [
            {"role": "user", "content": "request 1"},
            _assistant_tool_turn("old_tu", "read_file", {"path": "old.py"}),
            _tool_result_turn("old_tu", big_output),
            {"role": "user", "content": "request 2"},
            _assistant_tool_turn("mid_tu", "read_file", {"path": "mid.py"}),
            _tool_result_turn("mid_tu", big_output),
            {"role": "user", "content": "request 3"},
            _assistant_tool_turn("recent_tu", "read_file", {"path": "new.py"}),
            _tool_result_turn("recent_tu", big_output),
            {"role": "user", "content": "summarize"},
        ]

        pruned = prune_history(history, model_id="default")

        # Collect all tool_use IDs and tool_result IDs in the pruned history
        tool_use_ids = set()
        tool_result_ids = set()
        for msg in pruned:
            for block in msg.get("content", []):
                if is_tool_use(block):
                    tool_use_ids.add(block.get("id"))
                elif is_tool_result(block):
                    tool_result_ids.add(block.get("tool_use_id"))

        # Either both are present or both are absent — never a mismatch
        assert tool_use_ids == tool_result_ids, (
            f"Orphan blocks after pruning: "
            f"tool_use_ids={tool_use_ids}, tool_result_ids={tool_result_ids}"
        )

    def test_prune_keeps_recent_tool_pair(self):
        """A tool_use/tool_result pair inside the protection window must be
        kept with its content intact, even when older tool_results get cleared.

        Builds a history with two big old tool_results (enough to trigger
        prune) and one small recent pair that must survive verbatim.
        """
        big_old = "old file content\n" * 10_000  # ~170K chars each
        history = [
            {"role": "user", "content": "req 1"},
            _assistant_tool_turn("tu_old1", "read_file", {"path": "a.py"}, "reading"),
            _tool_result_turn("tu_old1", big_old),
            {"role": "user", "content": "req 2"},
            _assistant_tool_turn("tu_old2", "read_file", {"path": "b.py"}, "reading"),
            _tool_result_turn("tu_old2", big_old),
            {"role": "user", "content": "read foo"},
            _assistant_tool_turn("tu_recent", "read_file", {"path": "foo.py"}, "reading"),
            _tool_result_turn("tu_recent", "def foo(): pass"),
            {"role": "user", "content": "what does it do?"},
        ]

        pruned = prune_history(history, model_id="claude-sonnet-4-5-20250929")

        # The recent tool_use and its matching tool_result must both remain
        all_blocks = [b for m in pruned for b in m.get("content", [])]
        tool_uses = [b for b in all_blocks if is_tool_use(b) and b.get("id") == "tu_recent"]
        tool_results = [b for b in all_blocks if is_tool_result(b) and b.get("tool_use_id") == "tu_recent"]

        assert len(tool_uses) == 1, "Recent tool_use was incorrectly pruned"
        assert len(tool_results) == 1, "Recent tool_result was incorrectly pruned"
        # Recent content must be intact (not cleared)
        assert tool_results[0].get("content") == "def foo(): pass", (
            "Recent tool_result content was cleared — should be inside protection window"
        )

    def test_prune_with_no_pairs_still_works(self):
        """Pure text history should prune without errors."""
        history = [
            {"role": "user", "content": "x" * 80_000},
            {"role": "assistant", "content": "y" * 80_000},
            {"role": "user", "content": "recent"},
            {"role": "assistant", "content": "ok"},
        ]
        pruned = prune_history(history)
        # Should not raise and should return a valid list
        assert isinstance(pruned, list)
        assert len(pruned) > 0


class TestStructuredMessagePersistence:
    """session.add_message should accept both strings and block lists."""

    def test_add_message_with_string(self):
        session = Session()
        session.add_message("user", "hello")
        assert len(session.history) == 1
        assert session.history[0]["content"] == [{"type": "text", "text": "hello"}]

    def test_add_message_with_blocks(self):
        session = Session()
        session.add_structured_message(
            "assistant",
            [
                text_block("done"),
                tool_use_block("tu1", "bash", {"cmd": "ls"}),
            ],
        )
        assert len(session.history) == 1
        content = session.history[0]["content"]
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "tool_use"

    def test_empty_message_is_skipped(self):
        """Empty content should not create a history entry."""
        session = Session()
        session.add_message("user", "")
        assert len(session.history) == 0

    def test_roundtrip_through_serialization(self, tmp_path):
        """Save and load a session with tool_use blocks — structure must persist."""
        session = Session(session_id="roundtrip1")
        session.cwd = str(tmp_path)
        session.add_message("user", "do X")
        session.add_structured_message(
            "assistant",
            [
                text_block("working on it"),
                tool_use_block("tu_rt", "read_file", {"path": "a.py"}),
            ],
        )
        session.add_structured_message(
            "tool",
            [tool_result_block("tu_rt", "file contents here")],
        )

        store = SessionStore(base_dir=str(tmp_path))
        store.save(session)

        loaded = store.load("roundtrip1")
        assert loaded is not None
        assert len(loaded.history) == 3

        # Tool_use block must survive the round trip
        assistant_blocks = loaded.history[1]["content"]
        assert any(is_tool_use(b) and b.get("id") == "tu_rt" for b in assistant_blocks)

        # Tool_result block too
        tool_blocks = loaded.history[2]["content"]
        assert any(
            is_tool_result(b) and b.get("tool_use_id") == "tu_rt"
            and "file contents here" in b.get("content", "")
            for b in tool_blocks
        )


class TestAgnoMessageTranslation:
    """to_agno_messages must produce valid Agno Message objects."""

    def test_user_text_becomes_user_message(self):
        history = [{"role": "user", "content": [text_block("hi")]}]
        msgs = to_agno_messages(history)
        assert len(msgs) == 1
        assert msgs[0].role == "user"
        assert "hi" in str(msgs[0].content)

    def test_assistant_with_tool_use_populates_tool_calls_attr(self):
        """Agno's Claude adapter reads tool_calls (not content) for tool_uses."""
        history = [
            {
                "role": "assistant",
                "content": [
                    text_block("calling tool"),
                    tool_use_block("tu_x", "bash", {"cmd": "ls"}),
                ],
            }
        ]
        msgs = to_agno_messages(history)
        assert len(msgs) == 1
        msg = msgs[0]
        assert msg.role == "assistant"
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0]["id"] == "tu_x"
        assert msg.tool_calls[0]["function"]["name"] == "bash"

    def test_tool_role_produces_tool_message_with_tool_call_id(self):
        """Tool role items become Agno tool messages with tool_call_id set."""
        history = [
            {
                "role": "tool",
                "content": [tool_result_block("tu_y", "result text")],
            }
        ]
        msgs = to_agno_messages(history)
        assert len(msgs) == 1
        assert msgs[0].role == "tool"
        assert msgs[0].tool_call_id == "tu_y"
        assert msgs[0].content == "result text"

    def test_user_role_with_tool_result_blocks_emits_tool_messages(self):
        """Legacy: user msgs carrying tool_results become separate tool messages."""
        history = [
            {
                "role": "user",
                "content": [tool_result_block("tu_z", "legacy result")],
            }
        ]
        msgs = to_agno_messages(history)
        # Expect a single tool-role message (no user text to emit)
        assert len(msgs) == 1
        assert msgs[0].role == "tool"
        assert msgs[0].tool_call_id == "tu_z"
