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

**Fix, six layers (layers 5–6 added in ``fix/tui-freezing2``):**

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
5. **Incremental prefix cache** (``_render_incremental``). Once the
   buffer crosses ``_INCREMENTAL_MIN_BYTES`` (8 KB) and contains no
   reference-link definitions, each flush splits at the last blank
   line outside any open fence (from ``_scan_fences``), re-uses the
   cached ``Text`` rendered from the stable prefix, and parses only
   the tail paragraph + any newly-stabilised delta. Per-flush parse
   cost drops from O(len(buffer)) to O(len(tail)) — typically ~1 KB
   instead of ~40 KB. ``Text.append_text`` concatenates without
   re-flattening.
6. **Escape hatch for giant unclosed fences** (``_render_tail_escape``).
   Layer 5 still re-parses the tail on every flush; when the tail
   contains an unclosed fenced code block larger than
   ``_ESCAPE_HATCH_FENCE_BYTES`` (4 KB), that fence content is
   emitted as a flat ``Text`` — no ``markdown-it`` parse, no Pygments
   pass — so the per-flush cost stays roughly constant regardless of
   how much the fence grows. Triggers in two sites: inside
   ``_render_incremental`` when the open fence lives inside the tail,
   and in the main loop when the whole buffer sits inside an open
   fence from the start (``_find_last_stable_split`` returns ``-1``
   and the incremental path can't engage). Mid-stream the code shows
   as un-highlighted monospace; ``finalize_render`` then paints the
   authoritative Pygments-highlighted markdown once the fence closes.

**Correctness guards for the incremental path:**

* **G1 — Reference definitions.** ``markdown-it`` collects ``[ref]:
  url`` into ``state.env["references"]`` and resolves ``[text][ref]``
  against it across blocks. An isolated-prefix render would miss a
  def that arrives in the tail, so we probe ``_REF_DEF_RE`` on the
  whole snapshot and skip the incremental path when any such def
  exists.
* **G2 — Snapshot must extend the cache.** If ``buffer`` ever stops
  starting with ``_cache_prefix_src`` (non-append mutation, edited
  prior message, stream reset) we clear the cache and fall back.
* **Width changes** invalidate the cache (wrap decisions bake in at
  render time). ``on_resize`` clears proactively; the main loop also
  self-invalidates when it sees a changed width; and
  ``_render_incremental`` has a belt-and-suspenders local check for
  concurrent invalidation during its own awaits.
* **``finalize_render``** always runs a synchronous whole-buffer
  parse via ``_compose_renderable``, so the finished frame is
  pristine even if an edge case slipped past G1/G2 or stayed in the
  escape-hatch code path. Also clears the cache to avoid retaining
  ~40 KB of ``Text`` per closed bubble.

----

Post-mortem — "only the scroll froze" (2026-04-22, second half of
``fix/tui-freezing2``)
--------------------------------------------------------------
**Symptom:** every scrollable area in the app — ``ChatPane``,
``LoadedPane`` sidebar, any modal with overflow — stopped responding
to the mouse wheel simultaneously. Keyboard input, Enter, streaming,
and Ctrl+C continued to work, so it felt like a scroll-specific
freeze rather than an app hang. More common in long sessions.

**Cause:** Rich's ``Text`` passes raw bytes through to the terminal
without sanitising C0 control characters. When a streamed model
reply (Qwen was the main offender — it will talk *about* terminal
escape sequences without escaping the ESC byte itself) or a tool
label contained ``\\x1b[?1000l``, the terminal received it and
switched off X10 mouse reporting globally. Once mouse reporting is
off, the terminal stops forwarding wheel events to Textual at all —
no scroll area in the app can receive them, and the app has no way
to notice the state change because nothing on its side failed.
Keyboard uses a different channel, hence the asymmetry.

**Fix — ``_sanitize_for_terminal``.** Strip 0x00–0x1F (except
``\\n`` and ``\\t``) and 0x7F from every string that becomes a Rich
renderable in this module: ``_markdown_to_text`` sanitises its
``raw`` input on entry, ``_compose_renderable`` sanitises
``self.buffer`` before any role-specific branching, and
``_render_tail_escape`` sanitises the fence content before wrapping
it in a flat ``Text`` (the one path that doesn't go through
``_markdown_to_text``). A rogue ``\\x1b[?1000l`` now renders as
literal ``[?1000l`` text on screen — visible, harmless.

**Why this wasn't in any of layers 1–6:** those layers attack parse
cost and layout cost on the Aru side. This bug is about the
terminal's own private-mode state being corrupted from outside —
entirely unrelated to how fast we render, and invisible to any
latency benchmark. Treat as a seventh layer: output-hygiene.

----

Post-mortem — "mouse wheel dead during heavy streaming" (2026-04-23,
``fix/scroll-refinement``)
---------------------------------------------------------------------
**Symptom:** mouse wheel over the ChatPane does nothing while the
agent is actively streaming / running tool batches. TAB to focus
the pane + arrow keys / PgUp / PgDn works fine. Asymmetric enough
that it felt like "the mouse lost focus" — not a freeze.

Reported against session ``final-fantasy-battle/.aru/sessions/
e9397dc3.json``.

**Original (incorrect) theory:** interaction between
``self.anchor()`` (layer 4) and ``_scroll_up_for_pointer``. This
was based on a misreading of Textual's source — see the Layer 9
correction below. ``_scroll_up_for_pointer`` does *not* pass
``release_anchor=False``; it defaults to ``True`` (``widget.py:3378``
→ ``widget.py:2730``), so wheel-up already releases the anchor via
the framework. The ``on_mouse_scroll_up`` handler we added
(``ChatPane.on_mouse_scroll_up``) is therefore redundant with the
framework's own behaviour — a no-op on the happy path. It is kept
as defensive redundancy because removing it is the same shape of
change as keeping it, but it should not be credited for "fixing"
anything.

**What the bug probably was:** the same Layer-7 class of issue
that the next session surfaced again — a rogue DEC private-mode
escape reaching the terminal and disabling X10 mouse reporting.
See Layer 9 for the real signature and the robust fix.

----

Post-mortem — "wheel globally dead at end of stream" (2026-04-24,
``fix/scroll-refinement`` continued)
---------------------------------------------------------------------
**Symptom:** immediately after a long streaming turn concluded,
mouse wheel stopped working on *every* scrollable surface in the
app — ChatPane, sidebars, modals — simultaneously. TAB to walk
focus into a scrollbar and arrow-key scrolling from there worked.
Classic Layer-7 fingerprint: terminal-level mouse reporting got
turned off.

Reported against session ``final-fantasy-battle3/.aru/sessions/
7e9e4549.json``: one mega-turn with 120 tool calls interleaved
with 66 text blocks, 31 plan-panel mounts via
``add_renderable(scrollable=True)``, ~245 widgets in the pane.

**What we could prove:** a byte-level scan of the saved session
for C0 control chars turned up zero ``\\x1b`` bytes. The leak is
either (a) from a path that isn't persisted to ``session.json``
(tool ``stdout``/``stderr`` never reaches the chat directly but
transient UI strings, skill output, or reasoning tokens might),
or (b) a Windows ConPTY quirk during high-volume redraw where the
driver's mouse-enable state drops without us emitting anything
hostile. Chasing the exact source is caça ao fantasma; the
mitigation is structural.

**Two-prong fix:**

1. **Close the last unsanitised content path —
   ``_SanitizedRenderable``.** ``ChatMessageWidget`` already
   sanitises everything that goes through its ``buffer``. Arbitrary
   Rich renderables handed to ``add_renderable`` (plan panels, task
   lists, diff previews, the logo) bypass that path and mount as
   ``Static(renderable)``. The wrapper sits between the renderable
   and Rich's console, filtering C0 bytes out of every segment's
   ``.text`` before it reaches Textual's compositor. Matches the
   Layer 7 sanitisation boundary for the unchecked route.

2. **Self-heal at turn boundary —
   ``AruApp._run_turn`` finally clause.** Call the driver's
   ``_enable_mouse_support()`` after each turn finishes. That
   re-emits Textual's own four mouse-enable sequences (``?1000h``,
   ``?1003h``, ``?1015h``, ``?1006h`` — see
   ``textual/drivers/windows_driver.py:56``). Cost is four short
   writes; benefit is full recovery of wheel input regardless of
   what corrupted the terminal state mid-turn. Idempotent: a no-op
   when mouse tracking was never disabled.

Treat as a ninth layer: defence-in-depth against terminal-state
corruption. Prong 1 plugs the last known-possible leak inside our
code; prong 2 recovers even if something outside our reach drops
the state anyway.

----

Post-mortem — "wheel still dies after edits / option prompts" (2026-04-24,
``fix/scroll-analysis`` continued)
---------------------------------------------------------------------
**Symptom:** users reported that the wheel-dead bug reproduces most
often **right after a file edit was approved** or **while picking an
option in a modal**, rather than only at end-of-turn. Layers 7 + 9
already cover ``ChatMessageWidget.buffer`` and ``add_renderable``,
but the bug clearly fired through some content path neither of those
guarded.

**Cause:** the Layer 9 audit had a blind spot — the *modal screens*.
``ChoiceModal`` (the approval prompt for plan / edit / permission),
``ConfirmModal``, and ``TextInputModal`` all built their visible
content from raw caller-supplied strings:

* ``aru/tui/screens/choice.py:77`` — ``Label(self._title)``
* ``aru/tui/screens/choice.py:79`` — ``Static(self._details)``
  (the **diff preview** for edit approvals)
* ``aru/tui/screens/confirm.py:56`` — ``Label(self._prompt)``
* ``aru/tui/screens/text_input.py:52`` — ``Label(self._prompt)``

The ``details`` panel of ``ChoiceModal`` is the obvious gun — it's
where the unified diff goes when the user is asked to approve an
``edit_file``. Diffs over file content faithfully reproduce whatever
bytes were in the file; a colored shell script, a captured terminal
recording, or any binary-ish artifact saved as text trivially carries
``\\x1b[?1000l`` straight into the diff and onto the terminal. That
matches the user's reported pattern exactly: "wheel dies after I
approve an edit".

**Two-prong fix:**

1. **Lift the Layer 7/9 helpers into a shared module.**
   ``aru/tui/sanitize.py`` now exports ``sanitize_for_terminal`` and
   ``SanitizedRenderable``; ``chat.py`` imports them with the same
   names it had locally so nothing inside this file changes
   semantically. The four modal compose sites now apply the same
   barrier — ``Label(sanitize_for_terminal(self._prompt))`` for
   plain-text prompts and ``Static(SanitizedRenderable(self._details))``
   for arbitrary renderables. Any future modal added to the TUI
   should follow this convention; the helper module is the canonical
   location.

2. **Periodic mouse-tracking re-emit (``AruApp._reenable_mouse_tracking``).**
   The Layer 9 turn-boundary recovery only fires after the agent
   finishes. A diff preview shown mid-turn can disable the wheel for
   minutes if the agent is doing a long batch of edits. The new
   ``set_interval(_MOUSE_REENABLE_INTERVAL=8s, ...)`` in
   ``on_mount`` re-emits the four enable sequences every eight
   seconds regardless of turn state — ~24 bytes per tick, idempotent
   on a healthy terminal. Worst-case time-to-recover is bounded at 8s
   instead of "until the agent stops working".

Treat as a tenth layer. Layer 10 differs from 9 in scope: 9 plugs
known leaks at known boundaries, 10 assumes leaks will keep being
found (Textual stack, plugin renderables, a future modal) and recovers
on a clock independently of any code path noticing. The pair is the
intended steady state — not a bug-of-the-week, a structural answer to
a class of bug we cannot fully prevent without rewriting how arbitrary
renderables reach Rich's console.

----

Post-mortem — "input loses focus mid-stream in YOLO" (2026-04-25,
``fix/scroll-analysis`` continued)
---------------------------------------------------------------------
**Symptom:** during long YOLO-mode runs (no permission prompts, no
modals), the user reported that the input box stops accepting
keystrokes mid-implementation. Often coincident with the wheel-dead
signature, but distinct: typing goes nowhere even before any visible
panel suggests focus moved.

**What it was not:** the Layer 10 audit was scoped to *terminal-state*
corruption (mouse tracking turned off by stray escape bytes). The
focus issue is a separate failure mode — the same content paths that
leak C0 bytes also mount focusable widgets, and Textual's default
focus chain happily includes them.

**Two compounding causes:**

1. **``add_renderable(scrollable=True)`` mounted a focus-eligible
   ``VerticalScroll``.** ``VerticalScroll.can_focus`` defaults to
   ``True`` so users can Tab into a panel for keyboard scrolling.
   Inside the chat flow, where every plan/task/diff render adds a
   wrapper, this turns content panels into focus competitors with the
   ``Input``. A single Tab during streaming, a focus restoration
   after a modal closes, or any Textual-internal focus rotation could
   land on a panel and leave the input dead. Fix: ``wrapper.can_focus
   = False`` on every scrollable wrapper. Mouse-wheel scrolling inside
   the panel still works because Textual routes wheel events via the
   pointer, not the focus chain.

2. **``InlineChoicePrompt`` had no recovery if its callback raised.**
   The widget hides ``#input`` on mount and restores it on unmount.
   If the ``on_choice`` callback throws (or the widget is removed by a
   parent before lifecycle fires), ``-hidden`` stays applied and the
   input is invisible until the next mount/unmount cycle — possibly
   never. Layer 11 doesn't fix the underlying lifecycle issue
   (callbacks should be exception-safe in their own right) but adds a
   recovery loop: the periodic tick checks for stuck state and clears
   it.

**Layer 11 — input watchdog, sharing the Layer 10 timer.**
``AruApp._self_heal_terminal_state`` extends ``_reenable_mouse_tracking``
to also enforce input invariants when the inline-prompt path is not
legitimately active:

* If a modal screen is on top → skip (modal owns input).
* If an ``InlineChoicePrompt`` is mounted → skip (it owns focus by
  design while waiting for a choice).
* Otherwise: clear stuck ``-hidden`` from ``#input`` and refocus it
  iff ``screen.focused is None``. The ``focused is None`` guard is
  intentional — it does NOT fight legitimate Tab navigation to the
  sidebar / scrollback / search screen. We only recover from the
  ghost-focus state where Textual's chain has nobody.

Layer 11 also extends the modal-sanitisation pattern to
``InlineChoicePrompt``, which had been overlooked by Layer 10 — its
``Label(self._title)`` / ``Option(label)`` calls now go through
``sanitize_for_terminal`` before reaching the widget tree.

**Why we keep adding layers and not rewriting the architecture:** each
layer addresses a distinct *signal* the user reported — and each one
has narrow, idempotent recovery semantics. Rewriting the chat to use
a single virtualised text buffer (à la Textual's recent Markdown
virtualisation experiments) would close some of these by structure,
but at the cost of every other property the chat currently has
(selection, copy, mid-stream insertion of arbitrary Rich panels, plan
mounts). Layered defences are cheap and additive; the rewrite is not.
"""

from __future__ import annotations

import asyncio
import io
import re
from typing import Any

# ``rich.console`` + ``rich.markdown`` are only needed once the first
# assistant message starts streaming. Deferring them saves ~310 ms on
# TUI cold start (markdown-it parser + Pygments lexer registry load on
# import of ``rich.markdown``). The first ``_markdown_to_text`` call
# pays a one-shot cost but by then the UI is already interactive.
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from aru.tui.sanitize import (
    SanitizedRenderable as _SanitizedRenderable,
    sanitize_for_terminal as _sanitize_for_terminal,
)


# Reference-definition line: ``[label]: href`` (optional leading 0–3 spaces).
# Presence of *any* reference definition anywhere in the snapshot disables the
# incremental prefix cache for that render — see G1 in the post-mortem. The
# whole-buffer path still handles references correctly because ``markdown-it``
# sees the full ``state.env`` in one parse.
_REF_DEF_RE = re.compile(r"^[ ]{0,3}\[[^\]\n]+\]:\s", re.MULTILINE)


def _scan_fences(text: str) -> tuple[int, int]:
    """One-pass fence scanner. Returns ``(last_stable_split, open_fence_start)``.

    * ``last_stable_split``: offset right after the last blank line *outside*
      any open fence (the boundary used by the incremental prefix cache).
      ``-1`` if no such point exists.
    * ``open_fence_start``: if the buffer currently ends *inside* an open
      fence, the byte offset of that opener line's first character.
      ``-1`` if no fence is open at end-of-text.

    A "blank line" is an empty or whitespace-only line. A line with ≥4 leading
    spaces cannot open a fence (indented-code-block territory per CommonMark).
    Opening fences are runs of ≥3 backticks or tildes at line-start (after
    ≤3 leading spaces); a closer must use the same character with at least
    the same run length and no trailing content beyond whitespace.

    Pure function — no class state — unit-testable in isolation. Computing
    both boundaries in a single scan avoids walking the buffer twice on the
    hot path.
    """
    if not text:
        return (-1, -1)

    lines = text.split("\n")
    # Start offset of each line in the *original* string. Using `len(lines[i])`
    # preserves any trailing ``\r`` in CRLF inputs so offsets stay byte-exact.
    offsets: list[int] = [0]
    for ln in lines[:-1]:
        offsets.append(offsets[-1] + len(ln) + 1)  # +1 for '\n'

    in_fence = False
    fence_char: str | None = None  # '`' or '~'
    fence_len = 0
    fence_start_offset = -1
    last_split = -1

    for i, raw in enumerate(lines):
        # Tolerate CRLF: strip a trailing '\r' for the semantic check while
        # keeping ``raw`` (and therefore ``len(lines[i])`` in offsets) intact.
        line = raw.rstrip("\r")
        stripped = line.lstrip(" ")
        leading = len(line) - len(stripped)

        if leading < 4 and stripped and stripped[0] in ("`", "~"):
            c = stripped[0]
            run = 0
            while run < len(stripped) and stripped[run] == c:
                run += 1
            if run >= 3:
                if in_fence and fence_char == c and run >= fence_len:
                    # Closers allow only trailing whitespace (CommonMark).
                    if stripped[run:].strip() == "":
                        in_fence = False
                        fence_char = None
                        fence_len = 0
                        fence_start_offset = -1
                elif not in_fence:
                    in_fence = True
                    fence_char = c
                    fence_len = run
                    fence_start_offset = offsets[i]

        # A blank line is a candidate split point only if it's outside any
        # open fence *and* a '\n' actually terminates it (so the last entry
        # in `lines`, which has no trailing newline, never qualifies).
        if not in_fence and line.strip() == "" and i < len(lines) - 1:
            last_split = offsets[i] + len(raw) + 1

    return (last_split, fence_start_offset if in_fence else -1)


def _find_last_stable_split(text: str) -> int:
    """Return the offset right after the last blank line *outside* any fence.

    Thin wrapper around ``_scan_fences`` kept as a public helper for tests
    and callers that only need the split point.
    """
    return _scan_fences(text)[0]


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
        # Incremental-render cache. Holds the rendered ``Text`` of the
        # *stable* prefix of the buffer (everything up to the last blank
        # line outside a fence) so the hot path re-parses only the tail
        # paragraph being typed instead of the whole buffer. Read and
        # written exclusively from the ``_do_markdown_render`` coroutine
        # on the event-loop task — the ``asyncio.to_thread`` worker only
        # sees immutable strings and returns a fresh ``Text``, so no lock
        # is needed. Cleared on width change, on any non-append snapshot,
        # and on ``finalize_render`` (the last one both for hygiene and
        # to avoid retaining ~40 KB of ``Text`` per closed bubble).
        self._cache_prefix_src: str = ""
        self._cache_prefix_text: Text | None = None
        self._cache_width: int = 0
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
        # Sanitize C0 control chars before any branch — see
        # ``_sanitize_for_terminal`` docstring. Cheap enough to apply
        # universally and avoids any role-specific path forgetting it.
        text = _sanitize_for_terminal(self.buffer)
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

    # Buffer size at which the incremental prefix-cache path kicks in.
    # Below this threshold the whole-buffer parse is cheap enough (~20 ms
    # for 8 KB with one or two code blocks) that the bookkeeping cost of
    # finding a stable split + copying the cached Text doesn't pay for
    # itself. Above it, re-parsing the whole buffer on every flush is
    # what cumulative GIL starvation comes from — incremental caps the
    # per-flush cost at "parse the current paragraph only".
    _INCREMENTAL_MIN_BYTES: int = 8192

    # When an open fenced code block grows past this size, we stop
    # re-running ``markdown-it`` + Pygments on it every flush and render
    # its content as a flat ``Text`` until it closes. Pygments on 4 KB is
    # ~10 ms (tolerable per flush); on 20 KB it's ~50 ms (starves the UI
    # loop); on 40 KB ~100–200 ms (severe). 4 KB catches the first tier
    # before it hurts while still keeping syntax highlighting for the
    # common case of short code snippets. ``finalize_render`` does one
    # authoritative whole-buffer parse so the closed-bubble frame always
    # has proper highlighting regardless of what streamed through.
    _ESCAPE_HATCH_FENCE_BYTES: int = 4096

    async def _do_markdown_render(self) -> None:
        """Background loop: render the current buffer off-thread, apply on return.

        Loops while ``_pending_md_render`` keeps flipping on — this handles
        the bursty streaming case where new deltas land while we're mid-
        render. We always apply the freshest snapshot we actually rendered,
        and insert a cooldown before re-rendering so a fast stream doesn't
        pin a worker at 100 % and starve the event loop via GIL pressure.

        Above ``_INCREMENTAL_MIN_BYTES`` this routes through
        ``_render_incremental`` which re-parses only the tail paragraph
        and reuses a cached ``Text`` for the stable prefix — keeping the
        per-flush parse cost O(len(tail)) instead of O(len(buffer)) once
        a reply grows past the point where the whole-buffer parse itself
        starts to starve the UI loop.
        """
        try:
            while True:
                snapshot = self.buffer
                if not snapshot:
                    return
                width = max(self.size.width or 100, 20)

                # Cache invalidation: width drift (wrap decisions depend on
                # it) or any non-append snapshot (buffer reset, edit of a
                # prior message, …). Both force the prefix cache to be
                # rebuilt on the next incremental render.
                if width != self._cache_width or (
                    self._cache_prefix_src
                    and not snapshot.startswith(self._cache_prefix_src)
                ):
                    self._cache_prefix_src = ""
                    self._cache_prefix_text = None
                    self._cache_width = width

                # Pick the render path for this flush. Small buffers and
                # any buffer containing a reference-link definition go
                # through the unchanged whole-buffer path — the latter
                # because markdown-it collects ``[label]: href`` into
                # ``state.env["references"]`` and resolves cross-block, so
                # an isolated-prefix render would miss a def that arrives
                # later in the tail (G1 in the post-mortem).
                use_incremental = (
                    len(snapshot) >= self._INCREMENTAL_MIN_BYTES
                    and _REF_DEF_RE.search(snapshot) is None
                )
                split_idx, open_fence_idx = (
                    _scan_fences(snapshot) if use_incremental else (-1, -1)
                )

                try:
                    if split_idx > 0:
                        # Normal incremental path — escape-hatch handling
                        # for a giant open fence inside the tail lives
                        # inside ``_render_incremental``.
                        result = await self._render_incremental(
                            snapshot, split_idx, width
                        )
                    elif (
                        open_fence_idx >= 0
                        and len(snapshot) - open_fence_idx
                        >= self._ESCAPE_HATCH_FENCE_BYTES
                    ):
                        # No stable split (the whole buffer sits inside an
                        # open fence that started early) AND the fence is
                        # big enough to make Pygments the bottleneck. Skip
                        # the markdown parse on the fence content entirely
                        # and render it as a flat ``Text`` — O(1) parse
                        # cost per flush. The closing fence (or
                        # ``finalize_render``) triggers a proper markdown
                        # re-render via the naïve path or the full parse
                        # in ``_compose_renderable``.
                        result = await self._render_tail_escape(
                            snapshot, open_fence_idx, width
                        )
                    else:
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

    async def _render_incremental(
        self, snapshot: str, split_idx: int, width: int
    ) -> Text:
        """Parse only the delta prefix + current tail; reuse the cached prefix Text.

        The split point is guaranteed (by ``_find_last_stable_split``) to sit
        between two markdown blocks, so each of the three pieces — the cached
        prefix, the newly-stabilised delta, and the still-growing tail — is a
        self-contained markdown document and renders identically whether it's
        parsed alone or as part of the whole buffer (with the one exception
        of reference-link definitions, which the caller already screened out
        via ``_REF_DEF_RE`` before entering this path).

        The method holds a *local* ``prefix_text`` across every await so
        concurrent cache invalidation (``on_resize``, ``finalize_render``,
        or the outer loop's width-mismatch clear) cannot leave the return
        value undefined. The cache field is the long-lived optimisation
        for future renders; the local is the authoritative output for
        this one.
        """
        new_prefix = snapshot[:split_idx]
        tail = snapshot[split_idx:]

        # Defensive cache-coherence check: only reuse the cached prefix
        # when (a) it exists, (b) it was rendered at the same width (wrap
        # decisions bake in at render time), and (c) it's actually a
        # prefix of ``new_prefix``. The outer loop also invalidates on
        # width drift, but ``on_resize`` can fire between the outer
        # check and this call, so we re-check locally.
        cache_usable = (
            self._cache_prefix_text is not None
            and self._cache_width == width
            and new_prefix.startswith(self._cache_prefix_src)
        )

        if not cache_usable:
            # Cold cache (or invalidated): render the whole stable prefix.
            prefix_text = await asyncio.to_thread(
                _markdown_to_text, new_prefix, width
            )
        elif new_prefix != self._cache_prefix_src:
            # Warm cache, split advanced: parse only the newly-stabilised
            # delta (one or more freshly-terminated blocks) and extend a
            # copy of the cached Text. We work on a copy so the cache
            # stays mutation-free — cleaner invariants for the concurrent
            # ``on_resize`` / ``finalize_render`` paths that may clear it.
            delta = new_prefix[len(self._cache_prefix_src):]
            delta_text = (
                await asyncio.to_thread(_markdown_to_text, delta, width)
                if delta
                else Text("")
            )
            # Re-check the cache after the await — a concurrent clear
            # (on_resize mid-parse) would leave us without a base to
            # extend. In that case, fall back to a full prefix parse.
            base = self._cache_prefix_text
            if base is None:
                prefix_text = await asyncio.to_thread(
                    _markdown_to_text, new_prefix, width
                )
            else:
                prefix_text = base.copy()
                if delta:
                    _append_with_block_seam(prefix_text, delta_text)
        else:
            # Exact prefix hit — zero parse work for the prefix.
            prefix_text = self._cache_prefix_text

        # Install the freshly-computed prefix as the cache for the next
        # render. Even if a later await or external invalidation clears
        # ``self._cache_prefix_text``, ``prefix_text`` is our local
        # authoritative output for THIS call.
        self._cache_prefix_text = prefix_text
        self._cache_prefix_src = new_prefix
        self._cache_width = width

        # Tail: re-parsed every flush. By construction it's small (the
        # current paragraph being typed, typically < 2 KB) *unless* an
        # unclosed fenced code block has grown inside it — in that case
        # Pygments would re-highlight the whole block per flush. The
        # escape hatch renders the fence content as flat ``Text`` so the
        # per-flush parse cost stays O(tail_markdown), not O(fence_size).
        _, tail_fence_idx = _scan_fences(tail)
        if (
            tail_fence_idx >= 0
            and len(tail) - tail_fence_idx >= self._ESCAPE_HATCH_FENCE_BYTES
        ):
            tail_text = await self._render_tail_escape(
                tail, tail_fence_idx, width
            )
        else:
            tail_text = await asyncio.to_thread(
                _markdown_to_text, tail, width
            )

        # Build the result from the local prefix — immune to concurrent
        # cache clears that may have fired during the tail parse.
        result = prefix_text.copy()
        _append_with_block_seam(result, tail_text)
        return result

    async def _render_tail_escape(
        self, tail: str, fence_start: int, width: int
    ) -> Text:
        """Render ``tail`` = pre-fence markdown + fence content as flat ``Text``.

        The escape hatch for unclosed fenced code blocks. Skipping the
        Pygments pass keeps the per-flush cost roughly constant no matter
        how much the fence grows — which is what the user experiences as
        "doesn't freeze". Syntax highlighting resumes automatically once
        the fence closes (next flush no longer triggers the escape) and
        is applied authoritatively by ``finalize_render`` regardless of
        how much streamed through the escape path.

        Called from two sites: ``_render_incremental`` (when a big open
        fence lives inside the tail) and ``_do_markdown_render`` (when
        no stable split exists and the whole buffer is inside an open
        fence). Same shape either way — pre-fence is treated as markdown,
        fence content as flat ``Text``.
        """
        pre_fence = tail[:fence_start]
        fence_content = tail[fence_start:]

        if pre_fence.strip():
            pre_text = await asyncio.to_thread(
                _markdown_to_text, pre_fence, width
            )
        else:
            pre_text = Text("")

        # Plain ``Text(fence_content)`` — no Pygments, no markdown parse.
        # Construction is O(len) but trivial compared to either pass.
        # Sanitize here because this bypasses ``_markdown_to_text``, which
        # is the other site that strips control bytes before hitting the
        # terminal.
        fence_text = Text(_sanitize_for_terminal(fence_content))

        if pre_text.plain and fence_content:
            result = pre_text
            _append_with_block_seam(result, fence_text)
        elif pre_text.plain:
            result = pre_text
        else:
            result = fence_text
        return result

    def finalize_render(self) -> None:
        """Cancel any pending async render and paint the final markdown sync.

        Called by ChatPane when the stream closes. Guarantees the last
        frame the user sees is the authoritative markdown of the full
        buffer — ``_compose_renderable`` parses the whole buffer from
        scratch, so even if an incremental edge case slipped through the
        guards, the finished bubble the user reads is the pristine naïve
        render.

        Also clears the prefix cache so a ~40 KB ``Text`` isn't retained
        per closed bubble for the life of the ``ChatPane``.
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
        self._cache_prefix_src = ""
        self._cache_prefix_text = None
        self._cache_width = 0

    def on_resize(self, event) -> None:
        """Re-render the assistant bubble so markdown wrap follows width.

        Without this, segments are frozen at whatever width was live when
        the buffer last changed — a subsequent resize (toggling sidebar,
        making the terminal wider) would leave wrap decisions stale.

        The prefix cache is width-dependent (wrap decisions bake in at
        render time), so we invalidate it here; ``_do_markdown_render``
        will also self-invalidate on its next pass when it sees the new
        width, but clearing proactively keeps the state obvious.
        """
        if self.role == "assistant" and self.buffer:
            self._cache_prefix_src = ""
            self._cache_prefix_text = None
            self._cache_width = 0
            # Width changed → schedule a fresh render at the new width.
            # Goes through the async path so the (potentially expensive)
            # re-layout doesn't stall the resize animation itself.
            self._schedule_markdown_render()


def _append_with_block_seam(base: Text, extra: Text) -> None:
    """Append ``extra`` onto ``base`` reconstructing the inter-block blank line.

    Rich's Markdown renderer emits a blank line between adjacent blocks
    when it sees them in the same pass, but an isolated ``_markdown_to_text``
    call has no knowledge of what came before or what will come after, so it
    produces only the block's own trailing newline. Naively concatenating
    two such renders drops one newline at the seam — visible as "missing
    blank line between blocks" in the incremental render vs. the whole-
    buffer render.

    We insert that newline here so incremental + naive produce identical
    ``plain`` output (spans already match — ``Text.append_text`` adjusts
    offsets correctly). No-ops when ``base`` is empty (cold seam at start
    of buffer) or already ends with a blank line.
    """
    if base.plain and not base.plain.endswith("\n\n"):
        base.append("\n")
    base.append_text(extra)


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

    Input is passed through ``_sanitize_for_terminal`` first so rogue
    C0 escapes in streamed model output can't leak to the tty and turn
    off mouse tracking (which breaks scroll across the whole app).
    """
    if not raw:
        return Text("")
    raw = _sanitize_for_terminal(raw)
    # Lazy imports — see module-level comment on deferred ``rich.markdown``.
    # The first call after process start pays ~310 ms; subsequent calls are
    # free (Python import cache).
    from rich.console import Console
    from rich.markdown import Markdown
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
        # when the user manually scrolls — wheel, keyboard, or drag all go
        # through ``_scroll_to`` which releases by default (widget.py:2730);
        # and (c) re-engages automatically when they return to the bottom
        # via ``_check_anchor``. Matches Textual's own "streaming Markdown"
        # recipe (see ``Markdown.get_stream`` docstring).
        self.anchor()
        # Periodic flush; cheap because the reactive watcher already
        # debounces repaints when buffer doesn't actually change.
        self.set_interval(self.DEBOUNCE_SEC, self._flush_pending_delta)

    def on_mouse_scroll_up(self, event) -> None:
        """Defensive redundancy — explicitly release the anchor on wheel-up.

        Originally added under a misreading of Textual's source (see the
        Layer 8 correction in the module post-mortem). The framework's
        ``_scroll_up_for_pointer`` calls ``_scroll_to`` *without*
        ``release_anchor``, which defaults to ``True`` in
        ``widget.py:2730`` — so Textual already releases the anchor on
        wheel-up. This handler does the same thing one beat earlier and
        is effectively a no-op on the normal path.

        Kept because (a) removing it has the same shape of change as
        keeping it and (b) if some future Textual refactor ever flips
        the default, this keeps wheel-up behaving the way ChatPane
        needs. No ``event.stop()`` — the framework handler still runs
        after this and does the actual scroll.
        """
        if self._anchored and not self._anchor_released:
            self.release_anchor()

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
        # Sanitise the renderable's segment stream — see
        # ``_SanitizedRenderable`` docstring. This is the only content path
        # into the ChatPane that doesn't go through ``ChatMessageWidget``,
        # so it needs its own Layer-7 barrier.
        widget = Static(_SanitizedRenderable(renderable))
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
            # ``VerticalScroll`` is focusable by default so users can Tab
            # into it for keyboard scrolling. Inside the chat flow that's
            # the wrong default — the user navigates via the outer
            # ``ChatPane`` (whole-conversation scroll) and the wheel works
            # over an inner panel without focus thanks to Textual's
            # pointer-based wheel routing. Leaving these focusable makes
            # them race the ``Input`` for focus during plan/task/diff
            # mounts, with the symptom that typing stops reaching the
            # prompt mid-stream. ``can_focus = False`` removes them from
            # the focus chain entirely. (Layer 11 in the chat.py post-mortem.)
            wrapper.can_focus = False
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
