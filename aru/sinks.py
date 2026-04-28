"""StreamSink implementations (E3a).

- ``RichLiveSink`` — REPL sink wrapping Rich ``Live`` + ``StreamingDisplay``.
  Used by ``runner.run_agent_capture``. Preserves the exact visual
  behaviour of the legacy inline implementation.

- ``TextualBusSink`` lives in ``aru.tui.sinks`` (created in E3b).
"""

from __future__ import annotations

from typing import Any

from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from aru.display import StatusBar, StreamingDisplay
from aru.display import console as _default_console


class RichLiveSink:
    """StreamSink backed by Rich ``Live`` + ``StreamingDisplay``.

    Takes ownership of the Live context: ``enter()`` starts the Live
    render, ``exit()`` stops it. Between, the Agno stream loop drives
    presentation by calling ``on_tool_started`` / ``on_content_delta`` etc.

    The sink also installs itself on ``RuntimeContext`` so downstream code
    (permission prompts, plan rendering) can reach ``ctx.live`` and
    ``ctx.display`` as it did before E3a. ``exit()`` restores the
    previous values so nested runs don't clobber the parent.
    """

    def __init__(self, console=None) -> None:
        self.console = console or _default_console
        self.status = StatusBar(interval=3.0)
        self.display = StreamingDisplay(self.status)
        self.tracker = self.display.tool_tracker
        self._live: Live | None = None
        self._live_cm: Any = None
        # Saved parent values so nested runs restore cleanly.
        self._parent_live: Any = None
        self._parent_display: Any = None
        # Last label registered per tool id so on_tool_completed can
        # print a human-friendly line even when the Agno event only
        # carries the tool_name.
        self._tool_labels: dict[str, str] = {}

    # ── Lifecycle ────────────────────────────────────────────────────

    def enter(self) -> None:
        from aru.runtime import get_ctx

        # Snapshot parent live/display BEFORE installing ours — a nested
        # run_agent_capture (e.g. build agent calling enter_plan_mode)
        # must not clobber the outer Live handle or downstream permission
        # prompts hang.
        try:
            ctx = get_ctx()
            self._parent_live = getattr(ctx, "live", None)
            self._parent_display = getattr(ctx, "display", None)
        except LookupError:
            ctx = None

        self._live_cm = Live(
            self.display, console=self.console, refresh_per_second=10
        )
        self._live = self._live_cm.__enter__()
        if ctx is not None:
            ctx.live = self._live
            ctx.display = self.display

    def exit(self, exc: BaseException | None = None) -> None:
        from aru.runtime import get_ctx

        # Clear live content before the Live context exits so its final
        # render doesn't duplicate text that runner prints explicitly.
        try:
            self.display.content = None
        except Exception:
            pass

        if self._live_cm is not None:
            try:
                self._live_cm.__exit__(
                    type(exc) if exc else None,
                    exc,
                    exc.__traceback__ if exc else None,
                )
            except Exception:
                pass
            self._live_cm = None
            self._live = None

        try:
            ctx = get_ctx()
            ctx.live = self._parent_live
            ctx.display = self._parent_display
        except LookupError:
            pass

    # ── Convenience accessors for callers that need Rich handles ─────

    @property
    def live(self) -> Live | None:
        return self._live

    # ── Sink protocol implementation ─────────────────────────────────

    def on_tool_started(
        self,
        *,
        tool_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        label: str,
        accumulated: str,
    ) -> None:
        self._tool_labels[tool_id] = label
        # If there's un-rendered text accumulated, stop+flush it through
        # the console so the tool indicator lands after the markdown block.
        live = self._live
        if live is None:
            return
        if accumulated[self.display._flushed_len:]:
            self.display.content = None
            live.stop()
            self.display.flush()
            live.start()
            # Reset render shape — Rich caches layout sizes across stops.
            live._live_render._shape = None
        self.tracker.start(tool_id, label)
        self.status.set_text(f"{label}...")
        live.update(self.display)

    def on_tool_completed(
        self,
        *,
        tool_id: str,
        tool_name: str,
        result: Any,
        duration_ms: float,
        label: str,
    ) -> None:
        live = self._live
        if live is None:
            return
        # Resolve the better label we cached at start
        cached_label = self._tool_labels.pop(tool_id, label)
        self.tracker.complete(tool_id)
        for completed_label, duration in self.tracker.pop_completed():
            dur_str = f" {duration:.1f}s" if duration >= 0.5 else ""
            live.console.print(
                Text.assemble(
                    ("  ", ""),
                    ("✓ ", "bold green"),
                    (completed_label, "dim"),
                    (dur_str, "dim cyan"),
                )
            )
        live.update(self.display)
        # Best-effort — also silence lint about unused vars
        _ = cached_label, result, duration_ms

    def on_tool_batch_finished(self, *, session: Any) -> None:
        live = self._live
        if live is None:
            return
        if not self.tracker.active_labels:
            self.status.resume_cycling()
            # Flush coalesced plan-panel render (enter_plan_mode /
            # update_plan_step batching) — one panel per batch.
            try:
                from aru.tools.tasklist import flush_plan_render
                flush_plan_render(session)
            except Exception:
                pass

    def on_content_delta(self, *, delta: str, accumulated: str) -> None:
        live = self._live
        if live is None:
            return
        unflushed = accumulated[self.display._flushed_len:]
        # Long streams: when the un-rendered region grows past ~15 newlines,
        # stop the Live, print that chunk as a Markdown block, and resume.
        # Only breaks on an even number of ``` fences so we never split a
        # code block mid-way.
        if unflushed.count("\n") > 15:
            break_point = unflushed.rfind("\n\n")
            if break_point == -1:
                break_point = unflushed.rfind("\n")
            if break_point != -1:
                chunk = unflushed[: break_point + 1]
                if chunk.count("```") % 2 == 0:
                    self.display.content = None
                    live.stop()
                    self.console.print(Markdown(chunk))
                    self.display._flushed_len += len(chunk)
                    live.start()
                    live._live_render._shape = None
        self.display.set_content(accumulated)
        live.update(self.display)

    def on_stall(self) -> None:
        live = self._live
        if live is None:
            return
        live.console.print(
            "[yellow]Agent stalled (tool call limit likely reached). "
            "Moving on.[/yellow]"
        )

    def on_retry(self, *, attempt: int, max_attempts: int) -> None:
        live = self._live
        if live is None:
            return
        live.console.print(
            f"[dim]Output truncated at cap — resuming "
            f"({attempt}/{max_attempts})...[/dim]"
        )

    def on_retry_exhausted(self, *, max_attempts: int) -> None:
        live = self._live
        if live is None:
            return
        live.console.print(
            f"[yellow]Output still truncated after {max_attempts} recovery "
            f"attempts. Persisting the turn as-is.[/yellow]"
        )

    def notify(self, message: str, style: str = "") -> None:
        # Best-effort notification routed through Rich. Messages during
        # Live context go through ``live.console``; otherwise the default.
        target = self._live.console if self._live is not None else self.console
        if style:
            target.print(f"[{style}]{message}[/{style}]")
        else:
            target.print(message)

    def on_error(self, message: str) -> None:
        from rich.markup import escape
        target = self._live.console if self._live is not None else self.console
        target.print(f"[red]Error: {escape(message)}[/red]")

    def on_stream_finished(self, *, final_content: str) -> None:
        # Nothing to do here — runner flushes trailing markdown after exit()
        # using the accumulated content + display._flushed_len.
        pass
