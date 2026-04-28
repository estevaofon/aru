"""Tests for the StreamSink protocol + run_stream extraction (E3a)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from aru.streaming import StreamSink, StreamState, run_stream


class RecordingSink:
    """StreamSink that records every call — used to verify the loop."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def enter(self) -> None:
        self.events.append(("enter", {}))

    def exit(self, exc: BaseException | None = None) -> None:
        self.events.append(("exit", {"exc": type(exc).__name__ if exc else None}))

    def on_tool_started(self, **kw: Any) -> None:
        self.events.append(("tool_started", kw))

    def on_tool_completed(self, **kw: Any) -> None:
        self.events.append(("tool_completed", kw))

    def on_tool_batch_finished(self, *, session: Any) -> None:
        self.events.append(("tool_batch_finished", {}))

    def on_content_delta(self, *, delta: str, accumulated: str) -> None:
        self.events.append(("content_delta", {"delta": delta, "accumulated": accumulated}))

    def on_stall(self) -> None:
        self.events.append(("stall", {}))

    def on_retry(self, *, attempt: int, max_attempts: int) -> None:
        self.events.append(("retry", {"attempt": attempt}))

    def on_retry_exhausted(self, *, max_attempts: int) -> None:
        self.events.append(("retry_exhausted", {}))

    def notify(self, message: str, style: str = "") -> None:
        self.events.append(("notify", {"msg": message}))

    def on_error(self, message: str) -> None:
        self.events.append(("error", {"msg": message}))

    def on_stream_finished(self, *, final_content: str) -> None:
        self.events.append(("stream_finished", {"final": final_content}))


def test_recording_sink_conforms_to_protocol():
    """Sanity: our test sink satisfies the runtime_checkable Protocol."""
    assert isinstance(RecordingSink(), StreamSink)


def test_stream_state_defaults():
    s = StreamState()
    assert s.accumulated == ""
    assert s.stalled is False
    assert s.collected_tool_calls == []


class _FakeAgent:
    """Minimal agent that yields a scripted list of Agno events."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.run_output = None

    def arun(self, *args, **kwargs):
        events = self._events

        async def gen():
            for ev in events:
                yield ev

        return gen()


@pytest.mark.asyncio
async def test_run_stream_empty_run_publishes_nothing():
    """A run that yields no events should still invoke on_stream_finished."""
    from agno.run.agent import RunOutput

    sink = RecordingSink()
    fake = _FakeAgent([RunOutput()])
    published: list[tuple[str, dict]] = []

    async def publish(evt_type, data):
        published.append((evt_type, data))

    def prep_recovery(**kw):
        raise AssertionError("should not be called — stop_reason != max_tokens")

    # get_last_stop_reason returns None by default — run_stream should exit loop.
    state = await run_stream(
        fake,
        "hello",
        sink=sink,
        assistant_blocks=[],
        tool_result_msgs=[],
        pending_tool_uses={},
        flush_pending_text=lambda _a: None,
        publish_event=publish,
        prepare_recovery_input=prep_recovery,
    )

    names = [e[0] for e in sink.events]
    assert "stream_finished" in names
    assert state.accumulated == ""
    assert state.stalled is False


@pytest.mark.asyncio
async def test_run_stream_handles_tool_events():
    """Tool started + completed should update assistant_blocks + tool_result_msgs."""
    from agno.run.agent import (
        RunContentEvent,
        RunOutput,
        ToolCallCompletedEvent,
        ToolCallStartedEvent,
    )

    class FakeTool:
        def __init__(self, tid, name, args=None, result=None):
            self.tool_call_id = tid
            self.tool_name = name
            self.tool_args = args or {}
            self.result = result

    sink = RecordingSink()
    started = ToolCallStartedEvent(tool=FakeTool("t1", "read_file", {"path": "a.py"}))
    completed = ToolCallCompletedEvent(
        tool=FakeTool("t1", "read_file", {"path": "a.py"}, result="file contents")
    )
    fake = _FakeAgent([started, completed, RunOutput()])

    async def publish(evt_type, data):
        pass

    def prep_recovery(**kw):
        raise AssertionError()

    assistant_blocks: list[dict] = []
    tool_result_msgs: list[dict] = []
    pending: dict[str, dict] = {}

    state = await run_stream(
        fake,
        "user message",
        sink=sink,
        assistant_blocks=assistant_blocks,
        tool_result_msgs=tool_result_msgs,
        pending_tool_uses=pending,
        flush_pending_text=lambda _a: None,
        publish_event=publish,
        prepare_recovery_input=prep_recovery,
    )

    names = [e[0] for e in sink.events]
    assert "tool_started" in names
    assert "tool_completed" in names
    assert "tool_batch_finished" in names
    # Assistant blocks should carry the tool_use entry.
    tool_use_blocks = [b for b in assistant_blocks if b.get("type") == "tool_use"]
    assert len(tool_use_blocks) == 1
    assert tool_use_blocks[0]["name"] == "read_file"
    # Pending cleared.
    assert pending == {}
    # Tool result message created + closed.
    assert len(tool_result_msgs) == 1
    assert tool_result_msgs[0]["_open"] is False
    # _format_tool_label renders read_file as a human-friendly label (e.g. "Read")
    assert len(state.collected_tool_calls) == 1


@pytest.mark.asyncio
async def test_run_stream_accumulates_content():
    from agno.run.agent import RunContentEvent, RunOutput

    class _E:
        def __init__(self, content):
            self.content = content

    sink = RecordingSink()
    # Use the real RunContentEvent class to satisfy isinstance
    e1 = RunContentEvent(content="Hel")
    e2 = RunContentEvent(content="lo!")
    fake = _FakeAgent([e1, e2, RunOutput()])

    async def publish(evt_type, data):
        pass

    def prep_recovery(**kw):
        raise AssertionError()

    state = await run_stream(
        fake,
        "hi",
        sink=sink,
        assistant_blocks=[],
        tool_result_msgs=[],
        pending_tool_uses={},
        flush_pending_text=lambda _a: None,
        publish_event=publish,
        prepare_recovery_input=prep_recovery,
    )

    deltas = [e for e in sink.events if e[0] == "content_delta"]
    assert len(deltas) == 2
    assert state.accumulated == "Hello!"


def test_rich_live_sink_implements_protocol():
    """RichLiveSink should satisfy the runtime_checkable StreamSink."""
    from aru.sinks import RichLiveSink
    assert isinstance(RichLiveSink(), StreamSink)
