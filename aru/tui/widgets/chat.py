"""ChatPane — streaming chat view for the TUI (E3b).

Renders user + assistant messages as stacked ``ChatMessageWidget``s inside
a scrollable container. Each assistant message has a ``reactive`` buffer
that is updated incrementally by the ``TextualBusSink`` as content
deltas arrive from the Agno stream.

Design (per plan-reviewer):

* NO mutation of ``RichLog.lines[-1]`` — that is Textual internal state.
* Each assistant message is its own widget with a reactive ``buffer``;
  Textual's reactive system re-renders when it changes.
* ``set_interval(0.1, _flush)`` debounces rapid content deltas so we
  don't re-render on every single token.
* Tool calls show inline with a cycling indicator that flips to a check
  when the tool completes.

----

Post-mortem — scroll freeze (2026-04-22, ``fix/tui-freezing``)
--------------------------------------------------------------
**Symptom:** vertical scroll froze for seconds mid-stream while the
agent kept producing tokens.

**Cause:** ``watch_buffer`` re-parsed the whole (growing) buffer
through Rich.Markdown on the UI thread every flush — O(N²) over the
turn. Past ~5 KB (52 ms/parse at 20 Hz) the loop had no budget left
for scroll / input / paint. ``scroll_end(animate=False)`` compounded
it by queuing behind ``call_after_refresh``.

**Fix, four layers:**

1. ``asyncio.to_thread`` in ``_schedule_markdown_render`` moves the
   parse off the UI thread. ``_markdown_to_text`` still flattens to
   one ``Text`` so mouse selection + Ctrl+C keep working (Textual's
   native ``Markdown`` widget was rejected: its composite blocks
   break selection).
2. Coalescing: one render task per widget; newer deltas cause the
   in-flight result to be discarded and re-rendered on the freshest
   snapshot — avoids N intermediate ``update`` + layout passes.
3. 250 ms cooldown between renders: pure-Python parse still contends
   with the UI thread for the GIL; cooldown cut "notable freeze"
   iterations from 55 % to 15 % in the adversarial bench.
4. ``self.anchor()`` replaces manual ``scroll_end`` on every event —
   compositor auto-follows when anchored, respects manual scroll-up,
   re-engages on scroll-to-bottom. ``add_user_message`` keeps an
   explicit ``scroll_end(immediate=True)`` so submitted messages are
   always visible.

**If the freeze comes back** (very large + code-block-dense replies
>30 KB), next lever is incremental rendering: split at the last
stable markdown boundary (blank line outside a fence), cache the
prefix ``Text``, re-render only the tail. Rich has no incremental
API but the split is tractable.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static


class ChatMessageWidget(Static):
    """A single chat message — user, assistant, system, or tool call.

    Inherits from ``Static`` (not ``Widget``) so Textual's native text
    selection path works: ``Static`` participates in the selection
    traversal that ``Screen.get_selected_text`` uses. Click + drag to
    select, Ctrl+C to copy.
    """

    # Explicit — any refactor that disables this would silently break
    # copy-via-mouse.
    ALLOW_SELECT: bool = True

    DEFAULT_CSS = """
    ChatMessageWidget {
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
    }
    ChatMessageWidget.user {
        color: $accent;
        background: $boost;
        padding: 0 1;
    }
    ChatMessageWidget.assistant {
        color: $text;
        /* No inner max-height — long code blocks and replies flow into
           the ChatPane's own scroll, matching OpenCode's behaviour.
           The whole conversation scrolls as one, so the user can see
           every line of a big file dump without hunting for a nested
           scrollbar inside an assistant bubble. */
    }
    ChatMessageWidget.system {
        color: $text-muted;
        text-style: italic;
        margin-top: 0;
        margin-bottom: 0;
    }
    ChatMessageWidget.tool {
        color: $success;
        padding-top: 0;
        padding-bottom: 0;
        padding-left: 3;
        padding-right: 0;
        margin-top: 0;
        margin-bottom: 0;
        margin-left: 0;
        margin-right: 0;
        height: 1;
    }
    ChatMessageWidget.tool.pending {
        color: $warning;
    }
    """

    buffer: reactive[str] = reactive("", layout=True)

    def __init__(
        self,
        *,
        role: str = "assistant",
        initial: str = "",
        tool_state: str = "",
    ) -> None:
        # Pass empty renderable to Static; watch_buffer does the real work.
        super().__init__("")
        self.role = role
        self.tool_state = tool_state
        # Async markdown render state — see _schedule_markdown_render. Keeps
        # the Rich.Markdown parse off the UI thread so the event loop can
        # process scroll / keyboard / paint events even while a 20KB+
        # assistant reply is streaming in.
        self._md_render_task: asyncio.Task | None = None
        self._pending_md_render: bool = False
        self.set_reactive(ChatMessageWidget.buffer, initial)
        if role in ("user", "assistant", "system", "tool"):
            self.add_class(role)
        if role == "tool" and tool_state == "pending":
            self.add_class("pending")
        # Paint the initial render so the widget shows something before
        # the first reactive watcher fires.
        self.update(self._compose_renderable())

    def watch_buffer(self, _old: str, _new: str) -> None:
        # Reactive watcher — repaint when the buffer changes.
        # Assistant bubbles get their markdown rendered off-thread so the
        # UI stays responsive; everything else renders synchronously
        # because the cost is trivial (plain Text, ~O(len)).
        if self.role == "assistant":
            if _new:
                self._schedule_markdown_render()
            else:
                self.update(Text(""))
            return
        self.update(self._compose_renderable())

    def _compose_renderable(self) -> Any:
        text = self.buffer
        if self.role == "user":
            return Text(f"> {text}", style="bold")
        if self.role == "assistant":
            # Render markdown through Rich's real engine, then flatten
            # the resulting Segment stream into one Text so Textual's
            # selection traversal (click+drag, Ctrl+C) still walks the
            # characters. Rich's ``Markdown`` used directly is a Group
            # composite that selection skips — flattening preserves the
            # visual polish (bold, headings, bullets, code highlight)
            # without that limitation.
            if not text:
                return Text("")
            width = max(self.size.width or 100, 20)
            return _markdown_to_text(text, width=width)
        if self.role == "tool":
            icon = "✓" if self.tool_state == "done" else "↻"
            return Text(f"{icon} {text}")
        # system
        return Text(text)

    def _schedule_markdown_render(self) -> None:
        """Queue an off-thread markdown re-render of the assistant buffer.

        Why: Rich's ``Markdown`` engine parses the whole document on every
        call and — with code blocks / Pygments highlighting — costs
        ~100 ms for a 10 KB buffer and ~400 ms for a 40 KB buffer.
        Running that on the UI thread at 20 Hz (the debounce rate) used to
        starve scroll / mouse / keyboard events once a reply grew past a
        few KB, making the chat pane feel frozen even though the agent
        loop was still progressing.

        We hand the parse off to ``asyncio.to_thread``. The render output
        is byte-identical (``_markdown_to_text`` flattens to a single
        ``Text`` so click-and-drag text selection still walks characters,
        preserving Ctrl+C copy UX).

        Coalescing rule: if a render is still in flight, we flip
        ``_pending_md_render`` and let the running task loop back with
        the freshest buffer when it finishes — so the executor never sees
        more than one render in flight per widget even under a burst of
        deltas, and the most recent buffer always wins.
        """
        if self._md_render_task is not None and not self._md_render_task.done():
            self._pending_md_render = True
            return
        try:
            self._md_render_task = asyncio.create_task(self._do_markdown_render())
        except RuntimeError:
            # No running loop (shouldn't happen inside a Textual app, but
            # defensive) — fall back to a synchronous render so the widget
            # still paints *something*. The event loop starvation issue
            # this method exists to solve is moot without a loop anyway.
            self.update(self._compose_renderable())

    # Minimum interval between successive markdown renders of the same
    # bubble. Even off-thread the Rich.Markdown parse competes with the
    # UI thread for the GIL (pure-Python work), so two renders running
    # back-to-back while deltas stream in can still starve the loop.
    # 250 ms is below the human streaming-text reading rate (text flows
    # faster than the eye tracks individual updates past ~3 Hz anyway)
    # and cuts per-widget render CPU by 3–4× compared to running
    # flat-out on every flush.
    _MD_RENDER_COOLDOWN_SEC: float = 0.25

    async def _do_markdown_render(self) -> None:
        """Background loop: render the current buffer off-thread, apply on return.

        Loops while ``_pending_md_render`` keeps flipping on — this handles
        the bursty streaming case where new deltas land while we're mid-
        render. We always apply the freshest snapshot we actually rendered,
        and insert a cooldown before re-rendering so a fast stream doesn't
        pin a worker at 100 % and starve the event loop via GIL pressure.
        """
        try:
            while True:
                snapshot = self.buffer
                if not snapshot:
                    return
                width = max(self.size.width or 100, 20)
                try:
                    result = await asyncio.to_thread(
                        _markdown_to_text, snapshot, width
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return
                # We may have been superseded (e.g. finalize_render cancelled
                # us and painted synchronously); drop the stale result.
                if self._md_render_task is not asyncio.current_task():
                    return
                # If more deltas landed while we were rendering, skip
                # applying this already-stale snapshot and loop back to
                # render the newer one — each ``self.update`` triggers a
                # layout pass in the compositor, and painting an
                # intermediate frame that will be overwritten in ~30 ms
                # is pure waste at the heart of the UI thread. This is
                # the coalescing step that keeps a 30-delta burst down
                # to one or two real paints.
                if self._pending_md_render and self.buffer != snapshot:
                    self._pending_md_render = False
                    # Cooldown: let the UI thread drain scroll / input
                    # events before we kick off another expensive parse.
                    try:
                        await asyncio.sleep(self._MD_RENDER_COOLDOWN_SEC)
                    except asyncio.CancelledError:
                        raise
                    continue
                self.update(result)
                break
        finally:
            if self._md_render_task is asyncio.current_task():
                self._md_render_task = None
                self._pending_md_render = False

    def finalize_render(self) -> None:
        """Cancel any pending async render and paint the final markdown sync.

        Called by ChatPane when the stream closes. Guarantees the last
        frame the user sees is the authoritative markdown of the full
        buffer — no flicker between the last async render and the bubble
        being considered "done".
        """
        if self._md_render_task is not None:
            self._md_render_task.cancel()
            self._md_render_task = None
        self._pending_md_render = False
        if self.role == "assistant" and self.buffer:
            try:
                self.update(self._compose_renderable())
            except Exception:
                pass

    def on_resize(self, event) -> None:
        """Re-render the assistant bubble so markdown wrap follows width.

        Without this, segments are frozen at whatever width was live when
        the buffer last changed — a subsequent resize (toggling sidebar,
        making the terminal wider) would leave wrap decisions stale.
        """
        if self.role == "assistant" and self.buffer:
            # Width changed → schedule a fresh render at the new width.
            # Goes through the async path so the (potentially expensive)
            # re-layout doesn't stall the resize animation itself.
            self._schedule_markdown_render()


def _markdown_to_text(raw: str, width: int = 100) -> Text:
    """Render markdown via Rich, flatten the output into a selectable ``Text``.

    Uses Rich's real ``Markdown`` engine (bold, headings, bullets, fenced
    code with syntax highlighting, links), then walks the resulting
    ``Segment`` stream and rebuilds a single flat ``Text``. The flat
    shape is what Textual's ``get_selected_text`` path traverses, so
    click+drag inside an assistant bubble still selects arbitrary
    ranges. Rich's ``Markdown`` as a renderable is a composite
    (``Group`` of ``Panel``/``Syntax``/…) that bypasses that traversal —
    this function avoids that by converting structure→style while
    keeping the text linear.

    Width is passed explicitly so wrap decisions match the current pane
    width. On resize, ``ChatMessageWidget.on_resize`` re-calls this so
    the styling follows.
    """
    if not raw:
        return Text("")
    console = Console(
        file=io.StringIO(),
        width=max(width, 20),
        color_system="truecolor",
        force_terminal=True,
        legacy_windows=False,
    )
    try:
        options = console.options.update(width=max(width, 20))
        segments = list(console.render(Markdown(raw), options))
    except Exception:
        # Defensive: Rich should never raise on arbitrary text, but if
        # streaming ever hands us something pathological mid-delta, fall
        # back to plain so the stream keeps flowing.
        return Text(raw)
    out = Text()
    for seg in segments:
        if seg.text:
            out.append(seg.text, style=seg.style or "")
    return out


class ChatPane(VerticalScroll):
    """Scrollable container of chat messages (E3b)."""

    ALLOW_SELECT: bool = True

    DEFAULT_CSS = """
    ChatPane {
        background: $surface;
        /* Top/right/left keep the original 1-cell padding; the bottom
           is 2 so the last message always has breathing room above the
           ThinkingIndicator (or the status bar when idle) — moved here
           from ThinkingIndicator.margin-top so the gap is visible all
           the time, not only while the agent is busy. */
        padding: 1 1 2 1;
    }
    """

    # Debounce window for content-delta flushing (seconds). 10 Hz is
    # already visually fluid for streaming text (humans read at far
    # less) and it halves the GIL contention between the UI thread and
    # the off-thread Rich.Markdown parse compared to a 20 Hz flush —
    # which is the difference that matters once an assistant reply
    # crosses a few KB and each parse pass itself takes ~100 ms.
    DEBOUNCE_SEC: float = 0.1

    def __init__(self) -> None:
        super().__init__()
        self._active_assistant: ChatMessageWidget | None = None
        self._pending_delta: str = ""
        # Tool widgets keyed by tool_id so completion flips the same line.
        self._tool_widgets: dict[str, ChatMessageWidget] = {}

    def on_mount(self) -> None:
        # Engage Textual's anchor: the compositor will auto-scroll this
        # container to the bottom whenever its virtual size grows, without
        # us enqueuing a ``scroll_end`` after every delta / tool event.
        # (a) kills the ``call_after_refresh`` backlog that piled up when
        # the UI thread was busy rendering markdown; (b) releases the anchor
        # when the user manually scrolls up, so they can read history
        # mid-stream without the viewport snapping back every 50 ms; and
        # (c) re-engages automatically when they return to the bottom via
        # ``_check_anchor``. Matches Textual's own "streaming Markdown"
        # recipe (see ``Markdown.get_stream`` docstring).
        self.anchor()
        # Periodic flush; cheap because the reactive watcher already
        # debounces repaints when buffer doesn't actually change.
        self.set_interval(self.DEBOUNCE_SEC, self._flush_pending_delta)

    # ── API used by TextualBusSink and the App ────────────────────────

    def add_user_message(self, text: str) -> None:
        self._close_active_assistant()
        self._active_assistant = None
        self.mount(ChatMessageWidget(role="user", initial=text))
        # Force-scroll on user-initiated events even if the anchor was
        # released (user scrolled up to read history, then hit Enter).
        # ``immediate=True`` bypasses ``call_after_refresh`` so we don't
        # queue behind a busy render pipeline. Textual's scroll_end also
        # clears ``_anchor_released`` when ``_anchored`` is set.
        self.scroll_end(immediate=True, animate=False)

    def add_system_message(self, text: str) -> None:
        self._close_active_assistant()
        self.mount(ChatMessageWidget(role="system", initial=text))
        # No forced scroll — anchor handles auto-follow when engaged; if
        # the user scrolled up to read older content, a passive system
        # line shouldn't yank them back to the bottom.

    def add_renderable(
        self,
        renderable: Any,
        *,
        scrollable: bool = False,
        max_height: int = 20,
    ) -> None:
        """Mount an arbitrary Rich renderable (e.g. the ASCII logo).

        When ``scrollable=True``, the renderable is wrapped in a
        ``VerticalScroll`` that grows with its content up to
        ``max_height`` lines and only engages the inner scrollbar when
        the panel overflows. Using a hard ``height`` instead would
        reserve 20 blank rows under every small panel (task list with 2
        subtasks, one-liner plan status, etc.), which is the "giant
        margin above and below" the user sees.
        """
        from textual.widgets import Static
        self._close_active_assistant()
        widget = Static(renderable)
        if scrollable:
            from textual.containers import VerticalScroll
            wrapper = VerticalScroll()
            # Auto height so a 3-line panel occupies 3 rows, not 20.
            # The cap only kicks in for truly big content (long diffs,
            # file previews) where the scrollbar is the whole point.
            wrapper.styles.height = "auto"
            wrapper.styles.max_height = max_height
            # Kill any container padding/margin — the Rich panel already
            # has its own border+padding and we don't want an extra blank
            # row bleeding outside the box.
            wrapper.styles.padding = 0
            wrapper.styles.margin = 0
            self.mount(wrapper)
            wrapper.mount(widget)
        else:
            self.mount(widget)
        # Anchor handles follow-on scrolling; no explicit scroll here.

    def start_assistant_message(self) -> None:
        """Open a new assistant message to accumulate deltas into."""
        self._close_active_assistant()
        self._active_assistant = ChatMessageWidget(role="assistant", initial="")
        self.mount(self._active_assistant)
        # Anchor handles scrolling as the bubble fills; no explicit scroll.

    def append_assistant_delta(self, delta: str) -> None:
        """Accumulate content into the active assistant message (debounced)."""
        if self._active_assistant is None:
            self.start_assistant_message()
        self._pending_delta += delta

    def finalize_assistant_message(self, final: str | None = None) -> None:
        """Flush any buffered delta and close the message."""
        self._flush_pending_delta()
        widget = self._active_assistant
        if widget is not None:
            if final is not None:
                widget.buffer = final
            # Cancel any in-flight async markdown render and paint the
            # authoritative final markdown synchronously, so the last
            # frame the user sees is the finished bubble — no flash
            # between the async render that was mid-flight and the close.
            widget.finalize_render()
        self._active_assistant = None

    def add_tool_call(self, *, tool_id: str, label: str) -> None:
        """Emit an inline 'in-progress' tool entry."""
        widget = ChatMessageWidget(role="tool", initial=label, tool_state="pending")
        self._tool_widgets[tool_id] = widget
        self.mount(widget)

    def complete_tool_call(
        self, *, tool_id: str, label: str | None = None, duration_ms: float = 0.0
    ) -> None:
        """Flip a previously-emitted tool entry to the 'done' state."""
        widget = self._tool_widgets.pop(tool_id, None)
        if widget is None:
            # We never saw the start event — emit a retroactive complete line
            widget = ChatMessageWidget(
                role="tool", initial=(label or tool_id), tool_state="done"
            )
            self.mount(widget)
            return
        # Update label if caller gave a richer one; flip state classes.
        if label:
            widget.buffer = (
                f"{label} ({duration_ms/1000:.1f}s)"
                if duration_ms >= 500
                else label
            )
        widget.tool_state = "done"
        widget.remove_class("pending")
        widget.refresh(layout=True)

    # ── internals ─────────────────────────────────────────────────────

    def _flush_pending_delta(self) -> None:
        if self._pending_delta and self._active_assistant is not None:
            self._active_assistant.buffer = (
                self._active_assistant.buffer + self._pending_delta
            )
            self._pending_delta = ""

    def _close_active_assistant(self) -> None:
        self._flush_pending_delta()
        if self._active_assistant is not None:
            # Finalize the previous bubble before losing the reference —
            # otherwise its async render task may linger and try to
            # update an orphaned widget (no visible failure, just wasted
            # work and a lingering warning in the debug log).
            self._active_assistant.finalize_render()
        self._active_assistant = None
