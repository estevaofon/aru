"""Tests for the max_tokens recovery helper in runner.py.

The helper builds the message list we re-send after the provider truncated
an answer at the output cap. It must:

- Flush in-progress streamed text into the assistant block list so the
  resumed run sees what the model already wrote.
- Strip orphaned tool_use blocks (no matching tool_result yet) so the next
  request doesn't violate Anthropic's tool_use/tool_result pairing rule.
- Include closed tool_result rounds (complete batches of tool calls that
  did land results) so the model can see what it learned before resuming.
- Append a meta user message instructing the model to continue mid-thought.
"""
from __future__ import annotations

from agno.models.message import Message

from aru.runner import _MAX_TOKENS_RECOVERY_PROMPT, _prepare_recovery_input


def _flush_noop(_accumulated: str) -> None:
    """Stand-in for the real flush helper — our tests pre-populate blocks."""
    return None


class TestPrepareRecoveryInput:
    def test_appends_recovery_prompt_as_trailing_user_message(self):
        prior = [Message(role="user", content="hello")]
        messages = _prepare_recovery_input(
            agent=None,
            prior_history=prior,
            user_message="hello",
            assistant_blocks=[{"type": "text", "text": "partial answer"}],
            tool_result_msgs=[],
            pending_tool_uses={},
            accumulated_text="partial answer",
            flush_pending_text=_flush_noop,
            images=None,
        )
        assert messages[-1].role == "user"
        assert messages[-1].content == _MAX_TOKENS_RECOVERY_PROMPT

    def test_does_not_mutate_caller_history(self):
        prior = [Message(role="user", content="hello")]
        original_len = len(prior)
        _prepare_recovery_input(
            agent=None,
            prior_history=prior,
            user_message="hello",
            assistant_blocks=[{"type": "text", "text": "x"}],
            tool_result_msgs=[],
            pending_tool_uses={},
            accumulated_text="x",
            flush_pending_text=_flush_noop,
            images=None,
        )
        assert len(prior) == original_len

    def test_strips_orphaned_tool_use_blocks(self):
        """Open tool_uses (in pending_tool_uses) must be removed — otherwise
        the API rejects the next request.
        """
        orphan = {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}
        text = {"type": "text", "text": "writing..."}
        assistant_blocks = [text, orphan]
        pending = {"t1": orphan}

        messages = _prepare_recovery_input(
            agent=None,
            prior_history=[Message(role="user", content="hi")],
            user_message="hi",
            assistant_blocks=assistant_blocks,
            tool_result_msgs=[],
            pending_tool_uses=pending,
            accumulated_text="writing...",
            flush_pending_text=_flush_noop,
            images=None,
        )

        assert orphan not in assistant_blocks
        assert text in assistant_blocks, "non-orphaned text block must survive"
        assert pending == {}, "pending_tool_uses must be drained"
        # The assistant message rendered from blocks should carry no tool_calls.
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert all(not getattr(m, "tool_calls", None) for m in assistant_msgs)

    def test_closed_tool_rounds_forwarded_open_rounds_skipped(self):
        closed = {
            "role": "tool",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}],
            "_open": False,
        }
        open_round = {
            "role": "tool",
            "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "half"}],
            "_open": True,
        }

        messages = _prepare_recovery_input(
            agent=None,
            prior_history=[Message(role="user", content="hi")],
            user_message="hi",
            assistant_blocks=[{"type": "text", "text": "before"}],
            tool_result_msgs=[closed, open_round],
            pending_tool_uses={},
            accumulated_text="before",
            flush_pending_text=_flush_noop,
            images=None,
        )

        tool_msgs = [m for m in messages if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_call_id == "t1"

    def test_constructs_user_message_when_prior_history_empty(self):
        """On first-turn sessions prior_history is empty — the user text must
        still land in the resume request."""
        messages = _prepare_recovery_input(
            agent=None,
            prior_history=[],
            user_message="original question",
            assistant_blocks=[{"type": "text", "text": "partial"}],
            tool_result_msgs=[],
            pending_tool_uses={},
            accumulated_text="partial",
            flush_pending_text=_flush_noop,
            images=None,
        )
        user_msgs = [m for m in messages if m.role == "user"]
        # First user = the original question, last user = recovery prompt
        assert user_msgs[0].content == "original question"
        assert user_msgs[-1].content == _MAX_TOKENS_RECOVERY_PROMPT
