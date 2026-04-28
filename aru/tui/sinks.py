"""TextualBusSink — StreamSink implementation for TUI mode (E3b).

Bridges the Agno stream events to the ChatPane widget. The stream loop
runs in the App's event loop (inside a ``run_worker`` coroutine), so the
sink can touch widgets directly — but we prefer ``call_from_thread``
even when we're already on the loop, so the path is identical regardless
of how tools were scheduled.

No Rich Live involved — in TUI mode the sink is purely event-forward:
runner bookkeeping (history blocks, tool_result rounds) happens in
``streaming.run_stream`` exactly as in REPL; the difference is *where*
the rendering lands.
"""

from __future__ import annotations

from typing import Any


class TextualBusSink:
    """StreamSink that forwards events to a ChatPane via the Textual App.

    Invoked from within ``run_agent_capture_tui`` which itself runs on
    the App's event loop; updates are scheduled on the App via
    ``app.call_from_thread`` so they remain consistent whether the
    originating event was produced directly or surfaced from a tool
    thread.
    """

    def __init__(self, app: Any, chat_pane: Any) -> None:
        self.app = app
        self.chat = chat_pane
        # Track active labels so on_tool_completed can flip the right row
        # when the Agno event doesn't echo the original label.
        self._labels: dict[str, str] = {}

    # ── Lifecycle (no-op — TUI doesn't own a Rich Live context) ──────

    def enter(self) -> None:
        # Open a fresh assistant message to stream into.
        self._call(self.chat.start_assistant_message)

    def exit(self, exc: BaseException | None = None) -> None:
        # Close the streaming message.
        self._call(self.chat.finalize_assistant_message)

    # ── Tool events ───────────────────────────────────────────────────

    def on_tool_started(
        self,
        *,
        tool_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        label: str,
        accumulated: str,
    ) -> None:
        self._labels[tool_id] = label
        # Finalize the currently-streaming assistant bubble before the
        # tool indicator. Do NOT pre-open a new bubble here — back-to-back
        # tool calls would otherwise leave an empty assistant widget
        # between them that still contributes ``margin-bottom: 1``,
        # showing up as a blank row between consecutive tool rows.
        # ``append_assistant_delta`` lazily spawns a fresh bubble when
        # (and only when) post-tool text actually arrives.
        self._call(self.chat.finalize_assistant_message)
        self._call(self.chat.add_tool_call, tool_id=tool_id, label=label)

    def on_tool_completed(
        self,
        *,
        tool_id: str,
        tool_name: str,
        result: Any,
        duration_ms: float,
        label: str,
    ) -> None:
        cached = self._labels.pop(tool_id, label)
        self._call(
            self.chat.complete_tool_call,
            tool_id=tool_id,
            label=cached,
            duration_ms=duration_ms,
        )

    def on_tool_batch_finished(self, *, session: Any) -> None:
        """Flush any coalesced plan panel update into the chat.

        ``update_plan_step`` / ``create_task_list`` / ``enter_plan_mode``
        set a ``_plan_render_pending`` flag on the session so multiple
        mutations in the same tool batch collapse into a single panel.
        ``flush_plan_render`` renders that panel; ``_show`` routes it
        to the ChatPane when it detects ``ctx.tui_app``.
        """
        try:
            from aru.tools.tasklist import flush_plan_render
            flush_plan_render(session)
        except Exception:
            pass

    # ── Content / status events ──────────────────────────────────────

    def on_content_delta(self, *, delta: str, accumulated: str) -> None:
        self._call(self.chat.append_assistant_delta, delta)

    def on_stall(self) -> None:
        self._call(
            self.chat.add_system_message,
            "Agent stalled (tool call limit likely reached).",
        )

    def on_retry(self, *, attempt: int, max_attempts: int) -> None:
        self._call(
            self.chat.add_system_message,
            f"Output truncated — resuming ({attempt}/{max_attempts})…",
        )

    def on_retry_exhausted(self, *, max_attempts: int) -> None:
        self._call(
            self.chat.add_system_message,
            f"Output still truncated after {max_attempts} retries. "
            f"Persisting turn as-is.",
        )

    def notify(self, message: str, style: str = "") -> None:
        try:
            self.app.call_from_thread(
                self.app.notify, message, severity=style or "info"
            )
        except Exception:
            pass

    def on_error(self, message: str) -> None:
        # Errors are routed to the ChatPane (persistent) instead of a
        # Textual toast — under Textual, Agno's ERROR log lines never
        # reach the terminal, so this is the user's only visible signal
        # that something went wrong. Closing the active assistant bubble
        # first prevents the streamed-but-empty assistant message from
        # swallowing the error widget.
        self._call(self.chat.finalize_assistant_message)
        self._call(self.chat.add_system_message, f"Error: {message}")

    def on_stream_finished(self, *, final_content: str) -> None:
        self._call(self.chat.finalize_assistant_message, final_content or None)

    # ── Internal dispatch helper ──────────────────────────────────────

    def _call(self, fn, *args, **kwargs) -> None:
        """Dispatch a widget update onto the App's loop.

        Works from both the App's own loop (where ``call_from_thread``
        is still safe — Textual routes it internally) and from a
        ``asyncio.to_thread`` worker.
        """
        try:
            self.app.call_from_thread(fn, *args, **kwargs)
        except Exception:
            # Last resort: direct call. Safe when already on the loop
            # thread and the App is still running.
            try:
                fn(*args, **kwargs)
            except Exception:
                pass
