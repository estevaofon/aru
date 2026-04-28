"""StreamSink protocol + shared stream loop (E3a).

The Agno stream loop (``async for event in agent.arun(...)``) used to be
inline in ``runner.run_agent_capture`` and tightly coupled to Rich Live /
StreamingDisplay. This module factors the loop out so the same recovery /
stall / tool-batch logic is shared by:

* ``RichLiveSink`` (REPL mode — wraps Rich ``Live`` + ``StreamingDisplay``)
* ``TextualBusSink`` (TUI mode — publishes typed events on the plugin bus)

The sink knows how to render; the loop knows what the Agno events mean.
Runner-side bookkeeping (assistant_blocks / tool_result_msgs / session
persistence) stays in ``runner.py`` because it is session state, not
presentation.

Contract:

* ``sink.enter()`` / ``sink.exit(exc)`` bracket a whole run (sink may hold
  a Rich ``Live`` context; TUI sink no-ops).
* ``on_tool_started`` / ``on_tool_completed`` fire per Agno tool event.
* ``on_tool_batch_finished`` fires when the round's tools are all closed.
* ``on_content_delta`` fires on each ``RunContentEvent`` chunk.
* ``on_stall`` / ``on_retry`` / ``on_retry_exhausted`` bracket recovery.
* ``on_stream_finished`` fires once the entire run (including recovery
  passes) is done.
* ``notify`` is a best-effort sideband message (warnings, etc.).

The loop keeps a minimal ``StreamState`` it returns; the runner reads
``assistant_blocks`` / ``tool_result_msgs`` directly through the mutable
containers it passed in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ── State returned by run_stream ──────────────────────────────────────


@dataclass
class StreamState:
    accumulated: str = ""  # all RunContentEvent text joined in order
    stalled: bool = False
    collected_tool_calls: list[str] = field(default_factory=list)
    # Last RunOutput captured from the Agno stream. The runner reads
    # ``run_output.metrics`` for token accounting (``session.track_tokens``).
    # Without this the StatusPane stays at 0/0 forever.
    run_output: Any = None


# ── Sink protocol ─────────────────────────────────────────────────────


@runtime_checkable
class StreamSink(Protocol):
    """Presentation adapter for ``run_stream``.

    The concrete implementations (``RichLiveSink`` in REPL mode,
    ``TextualBusSink`` in TUI mode) handle how events are rendered.
    """

    def enter(self) -> None: ...
    def exit(self, exc: BaseException | None = None) -> None: ...

    def on_tool_started(
        self,
        *,
        tool_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        label: str,
        accumulated: str,
    ) -> None:
        """A new tool_use was emitted.

        ``accumulated`` is the full running text so far. The sink may
        flush un-rendered markdown before displaying the tool indicator
        (REPL) or publish a ToolCalledEvent (TUI).
        """
        ...

    def on_tool_completed(
        self,
        *,
        tool_id: str,
        tool_name: str,
        result: Any,
        duration_ms: float,
        label: str,
    ) -> None:
        """Tool finished — emit check mark / ToolCompletedEvent."""
        ...

    def on_tool_batch_finished(self, *, session: Any) -> None:
        """All active tools finished — sink may flush plan panel etc."""
        ...

    def on_content_delta(self, *, delta: str, accumulated: str) -> None:
        """A ``RunContentEvent`` chunk arrived.

        The sink is responsible for any incremental rendering (REPL flushes
        markdown on long newline runs; TUI appends to ChatPane buffer).
        """
        ...

    def on_stall(self) -> None:
        """Stall counter hit limit — sink should show a warning."""
        ...

    def on_retry(self, *, attempt: int, max_attempts: int) -> None:
        """About to retry after a max_tokens truncation."""
        ...

    def on_retry_exhausted(self, *, max_attempts: int) -> None:
        """All retry attempts consumed — persisting truncated turn."""
        ...

    def notify(self, message: str, style: str = "") -> None:
        """Best-effort sideband user message (warnings etc.)."""
        ...

    def on_error(self, message: str) -> None:
        """Terminal error — runner caught an exception from the agent run.

        REPL renders via Rich console; TUI must route to the ChatPane so
        the user actually sees it (Textual hijacks stderr/stdout).
        """
        ...

    def on_stream_finished(self, *, final_content: str) -> None:
        """Run finished — sink may render any trailing markdown."""
        ...


# ── run_stream ────────────────────────────────────────────────────────


async def run_stream(
    agent: Any,
    agent_input: Any,
    *,
    sink: StreamSink,
    session: Any = None,
    images: list | None = None,
    history_messages: list | None = None,
    user_message: str = "",
    assistant_blocks: list[dict],
    tool_result_msgs: list[dict],
    pending_tool_uses: dict[str, dict],
    flush_pending_text,
    publish_event,
    max_recovery_attempts: int = 3,
    recovery_prompt: str = "",
    prepare_recovery_input,
) -> StreamState:
    """Run the Agno stream loop with max-tokens recovery.

    Shared by ``run_agent_capture`` (REPL) and ``run_agent_capture_tui`` (TUI).
    Runner-side bookkeeping (the three mutable containers) stays owned by
    the caller; the sink renders presentation; this function drives the
    Agno event loop and recovery retries.

    Returns a ``StreamState`` describing the run outcome. The caller uses
    it + the mutated containers to persist to the session.
    """
    from agno.models.message import Message
    from agno.run.agent import (
        RunContentEvent,
        RunOutput,
        ToolCallCompletedEvent,
        ToolCallStartedEvent,
    )
    from aru.cache_patch import get_last_stop_reason, reset_last_stop_reason
    from aru.display import _format_tool_label

    state = StreamState()
    accumulated = ""
    run_output = None
    current_input = agent_input
    recovery_attempts_left = max_recovery_attempts
    _STALL_LIMIT = 20

    arun_kwargs = dict(stream=True, stream_events=True, yield_run_output=True)
    if isinstance(agent_input, str) and images:
        arun_kwargs["images"] = images

    # Track tool start times so the sink gets a duration on completion.
    tool_start_times: dict[str, float] = {}
    import time as _time

    while True:
        reset_last_stop_reason()
        _stall_counter = 0

        async for event in agent.arun(current_input, **arun_kwargs):
            if isinstance(event, RunOutput):
                run_output = event
                state.run_output = event
                break

            if isinstance(event, ToolCallStartedEvent):
                _stall_counter = 0
                if hasattr(event, "tool") and event.tool:
                    tool_name = event.tool.tool_name or "tool"
                    tool_args = event.tool.tool_args or None
                    tool_id = (
                        getattr(event.tool, "tool_call_id", None) or tool_name
                    )
                else:
                    tool_name = getattr(event, "tool_name", "tool")
                    tool_args = getattr(event, "tool_args", None)
                    tool_id = getattr(event, "tool_call_id", None) or tool_name
                label = _format_tool_label(tool_name, tool_args)
                state.collected_tool_calls.append(label)

                # Runner-side bookkeeping: flush any accumulated text into a
                # text block, then append the tool_use block.
                flush_pending_text(accumulated)
                from aru.history_blocks import tool_use_block
                assistant_blocks.append(
                    tool_use_block(
                        tool_id,
                        tool_name,
                        tool_args if isinstance(tool_args, dict) else {},
                    )
                )
                pending_tool_uses[tool_id] = assistant_blocks[-1]

                tool_start_times[tool_id] = _time.monotonic()
                sink.on_tool_started(
                    tool_id=tool_id,
                    tool_name=tool_name,
                    tool_args=tool_args if isinstance(tool_args, dict) else {},
                    label=label,
                    accumulated=accumulated,
                )
                await publish_event(
                    "tool.called",
                    {
                        "tool_id": tool_id,
                        "tool_name": tool_name,
                        "args": tool_args if isinstance(tool_args, dict) else {},
                    },
                )

            elif isinstance(event, ToolCallCompletedEvent):
                _stall_counter = 0
                if hasattr(event, "tool") and event.tool:
                    tool_id = getattr(
                        event.tool, "tool_call_id", None
                    ) or getattr(event.tool, "tool_name", "tool")
                    tool_name = getattr(event.tool, "tool_name", "tool")
                    tool_result_text = getattr(event.tool, "result", None)
                else:
                    tool_id = getattr(event, "tool_call_id", None) or getattr(
                        event, "tool_name", "tool"
                    )
                    tool_name = getattr(event, "tool_name", "tool")
                    tool_result_text = getattr(event, "content", None)

                # Runner-side bookkeeping: bundle tool_result into round msg.
                if tool_id in pending_tool_uses:
                    from aru.history_blocks import tool_result_block
                    result_str = (
                        str(tool_result_text) if tool_result_text is not None else ""
                    )
                    tr_block = tool_result_block(tool_id, result_str)
                    if tool_result_msgs and tool_result_msgs[-1]["_open"]:
                        tool_result_msgs[-1]["content"].append(tr_block)
                    else:
                        tool_result_msgs.append({
                            "role": "tool",
                            "content": [tr_block],
                            "_open": True,
                        })
                    pending_tool_uses.pop(tool_id, None)

                await publish_event(
                    "tool.completed",
                    {
                        "tool_id": tool_id,
                        "tool_name": tool_name,
                        "result_length": (
                            len(str(tool_result_text)) if tool_result_text else 0
                        ),
                    },
                )

                duration_ms = 0.0
                if tool_id in tool_start_times:
                    duration_ms = (
                        _time.monotonic() - tool_start_times.pop(tool_id)
                    ) * 1000.0
                # Best-effort label: the sink may have cached one via
                # on_tool_started; we pass tool_name as a fallback.
                sink.on_tool_completed(
                    tool_id=tool_id,
                    tool_name=tool_name,
                    result=tool_result_text,
                    duration_ms=duration_ms,
                    label=tool_name,  # sink caches its own label if needed
                )

                # When the last active tool in the round completed, close it
                # and let the sink flush deferred renders (plan panel etc.).
                if not pending_tool_uses:
                    if tool_result_msgs and tool_result_msgs[-1]["_open"]:
                        tool_result_msgs[-1]["_open"] = False
                    sink.on_tool_batch_finished(session=session)

            elif isinstance(event, RunContentEvent):
                _stall_counter = 0
                if hasattr(event, "content") and event.content:
                    delta = event.content
                    accumulated += delta
                    sink.on_content_delta(delta=delta, accumulated=accumulated)

            else:
                _stall_counter += 1
                if _stall_counter >= _STALL_LIMIT:
                    state.stalled = True
                    sink.on_stall()
                    break

        # Stream for this attempt finished. Decide on recovery.
        if get_last_stop_reason() != "max_tokens":
            break
        if state.stalled:
            break
        if recovery_attempts_left <= 0:
            sink.on_retry_exhausted(max_attempts=max_recovery_attempts)
            break

        current_input = prepare_recovery_input(
            agent=agent,
            prior_history=history_messages or [],
            user_message=user_message,
            assistant_blocks=assistant_blocks,
            tool_result_msgs=tool_result_msgs,
            pending_tool_uses=pending_tool_uses,
            accumulated_text=accumulated,
            flush_pending_text=flush_pending_text,
            images=images,
        )
        recovery_attempts_left -= 1
        attempt_no = max_recovery_attempts - recovery_attempts_left
        sink.on_retry(attempt=attempt_no, max_attempts=max_recovery_attempts)
        run_output = None

    state.accumulated = accumulated
    sink.on_stream_finished(final_content=accumulated)
    return state
