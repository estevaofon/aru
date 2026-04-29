"""PromptArea — multi-line prompt input for the TUI shell.

Replaces the single-line ``PromptInput(Input)`` so users can paste
multi-line content visibly, edit it, and only submit when they're
ready. Enter submits; multi-line input is composed via:

* **Ctrl+J** — inserts a literal LF. Works in every terminal because
  ``ctrl+j`` is the C0 control byte for line-feed.
* **Trailing ``\\``** — typing ``\\<Enter>`` inserts a newline and
  *does not* submit. Mirrors shell line-continuation; this is the
  fallback Claude Code uses for the same reason.
* **Shift+Enter** / **Alt+Enter** — accepted when the terminal is
  capable of transmitting the modifier (kitty keyboard protocol or
  similar). Most legacy terminals drop the shift bit and only deliver
  ``enter``, which is why we cannot rely on this binding alone.

Why not stick with ``Input``: pasting a multi-line stack trace, diff,
or log block landed as a hidden stash with no way to edit it before
sending. The workaround was a stash + system-message preview, which
the user couldn't proofread or amend without resetting the input.
``TextArea`` natively handles multi-line editing — paste lands in the
visible buffer, the user adjusts, then Enter sends.

Integration:

* Emits ``PromptArea.Submitted`` (carries ``text``) on Enter — the App's
  ``on_prompt_area_submitted`` handler is the single dispatch point.
* Emits ``PromptArea.Changed`` on every keystroke (forwarded from
  ``TextArea.Changed``) so ``SlashCompleter`` keeps tracking
  ``/`` and ``@`` prefixes on the *current line only*.
* Emits ``PromptArea.HistoryPrev`` / ``HistoryNext`` when the user
  presses Up/Down at the first/last row with no completer open. The
  App rolls those into its existing ``_history`` cycle.

Visible behaviour:

* Single content row at rest. Grows up to 8 rows as the user types or
  pastes. Above that, the inner scrollbar takes over so a giant paste
  doesn't push the chat off-screen.
* Border + placeholder remain identical to the previous PromptInput so
  the visual style of the bottom-of-screen input bar is unchanged.
"""

from __future__ import annotations

from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea


class PromptArea(TextArea):
    """Multi-line ``TextArea`` configured as the Aru prompt bar.

    See module docstring for the overall design. The class is small —
    the work is in the bindings + four custom ``Message`` subclasses.
    """

    DEFAULT_CSS = """
    PromptArea {
        height: auto;
        /* min-height counts the border rows, so 4 = 2 visible content
           lines + top/bottom border. One row at rest hid wrapped text
           and made the trailing-backslash continuation invisible. */
        min-height: 4;
        max-height: 10;
        /* Both unfocused and focused states use a rounded border —
           Textual's TextArea defaults to ``tall $border`` on focus,
           which produces a square look that disagrees with the rest
           of Aru's panels. Pin both states to ``round`` so the prompt
           keeps its softened corners regardless of focus. */
        border: round $primary 50%;
        padding: 0 1;
        margin: 0;
    }
    PromptArea:focus {
        border: round $primary;
    }
    PromptArea.-hidden {
        /* Hidden while an InlineChoicePrompt is awaiting a decision —
           same trigger as the old ``#input.-hidden`` class. */
        display: none;
    }
    PromptArea > .text-area--cursor-line {
        background: transparent;
    }
    /* Native placeholder selector (Textual 8). Without this override the
       default ``$text 40%`` lands as dark text on the surface colour of
       light themes — barely legible. Use the muted text token, which
       resolves to a readable contrast on every shipped theme. */
    PromptArea > .text-area--placeholder {
        color: $text-muted;
        text-style: italic;
        background: transparent;
    }
    """

    PLACEHOLDER_TEXT: str = (
        "Type a message · / commands · @ files · Tab accepts · "
        "Enter sends · Ctrl+J or \\<Enter> for newline"
    )

    # Replace the default TextArea bindings for Enter / Up / Down /
    # Tab / Esc so the App can intercept them. ``priority=True`` so
    # they fire before the focused widget can absorb the key.
    BINDINGS = [
        Binding("enter", "submit_prompt", "Send", show=False, priority=True),
        # ``ctrl+j`` sends LF (0x0A) — the same byte every Unix terminal
        # uses for newline. Works on Windows Terminal, conhost, Git Bash,
        # iTerm2, alacritty, kitty, tmux/screen — anywhere ``\n`` does.
        # This is the *primary* way to insert a newline.
        Binding(
            "ctrl+j",
            "insert_newline",
            "Newline",
            show=False,
            priority=True,
        ),
        # ``shift+enter`` and ``alt+enter`` only reach the app when the
        # terminal speaks the kitty keyboard protocol (or similar). On
        # the typical Windows Terminal / conhost combo the shift bit is
        # dropped and we never see this binding fire — that's why
        # ``ctrl+j`` and the trailing-backslash fallback exist.
        Binding(
            "shift+enter",
            "insert_newline",
            "Newline",
            show=False,
            priority=True,
        ),
        Binding(
            "alt+enter",
            "insert_newline",
            "Newline",
            show=False,
            priority=True,
        ),
    ]

    # ── Custom messages ─────────────────────────────────────────────

    class Submitted(Message):
        """User pressed Enter with non-empty content — send to agent."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class HistoryPrev(Message):
        """Up at the first row → step backwards through history."""

    class HistoryNext(Message):
        """Down at the last row → step forwards through history."""

    def __init__(self, *args, **kwargs) -> None:
        # Forward the placeholder via the native ``TextArea`` constructor
        # parameter (Textual 8+). The class-level constant remains the
        # source of truth so subclasses / snapshot tests can read it.
        kwargs.setdefault("placeholder", self.PLACEHOLDER_TEXT)
        super().__init__(*args, **kwargs)

    # Public-name accessor so the App can read & clear the buffer
    # without coupling to ``TextArea.text`` everywhere.

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, new_text: str) -> None:
        # Mirrors ``Input.value = ...``. ``load_text`` resets the
        # document, places the cursor at the end, and emits ``Changed``.
        try:
            self.load_text(new_text or "")
        except Exception:
            pass

    # ── Bindings ────────────────────────────────────────────────────

    def action_submit_prompt(self) -> None:
        # Trailing-backslash continuation: if the user's *current line*
        # ends with an unescaped ``\``, treat Enter as "newline" instead
        # of "send". Mirrors shell line-continuation and gives users a
        # one-handed way to compose multi-line prompts on terminals that
        # silently drop ``shift+enter`` (the typical Windows Terminal /
        # conhost case). We strip the lone ``\`` so it doesn't end up
        # inside the message.
        try:
            row, col = self.cursor_location
            line = self.document.get_line(row)
        except Exception:
            row, col, line = 0, 0, ""
        head = line[:col] if line else ""
        # Single trailing backslash escapes the Enter; ``\\`` (two
        # backslashes) is the user explicitly meaning a literal
        # backslash, so we only intercept the *odd-count* case.
        if head.endswith("\\"):
            n = 0
            i = len(head) - 1
            while i >= 0 and head[i] == "\\":
                n += 1
                i -= 1
            if n % 2 == 1:
                # Erase the lone backslash, then insert a newline at
                # the cursor — same path Ctrl+J takes.
                try:
                    self.replace("", (row, col - 1), (row, col))
                    self.insert("\n")
                except Exception:
                    pass
                return
        text = (self.text or "").rstrip("\n")
        # Rstrip here matches the REPL's behaviour: a trailing blank
        # line from "type a sentence, Enter (sends), oops Enter" is
        # not part of the message.
        self.post_message(self.Submitted(text))

    def action_insert_newline(self) -> None:
        # ``insert`` is the public TextArea API for content mutation;
        # uses the same path as keystrokes so undo / redo / cursor
        # tracking all stay correct.
        try:
            self.insert("\n")
        except Exception:
            pass

    # ── Up / Down → history hooks (only at row boundaries) ──────────

    def action_cursor_up(self, select: bool = False) -> None:  # type: ignore[override]
        # If the cursor is on the first row, treat Up as a history
        # request. This matches readline / most REPL conventions.
        try:
            row, _col = self.cursor_location
        except Exception:
            row = 0
        if row == 0 and not select:
            self.post_message(self.HistoryPrev())
            return
        super().action_cursor_up(select=select)

    def action_cursor_down(self, select: bool = False) -> None:  # type: ignore[override]
        try:
            row, _col = self.cursor_location
        except Exception:
            row = 0
        try:
            line_count = self.document.line_count
        except Exception:
            line_count = 1
        if row >= line_count - 1 and not select:
            self.post_message(self.HistoryNext())
            return
        super().action_cursor_down(select=select)

    # ── Cursor-aware "current token" helper for SlashCompleter ─────

    def current_line_text(self) -> str:
        """Return text of the line the cursor is on.

        ``SlashCompleter`` keys completion off the *line* the user is
        editing, not the whole document — pasting a five-line diff
        and adding ``/help`` on the last line should still pop the
        slash menu.
        """
        try:
            row, _col = self.cursor_location
            return self.document.get_line(row)
        except Exception:
            return self.text or ""

    def replace_current_line_token(self, replacement: str) -> None:
        """Swap the trailing ``/...`` or ``@...`` token on the cursor's line.

        Used by ``SlashCompleter`` accept. Walks back from the cursor
        to the prefix character and replaces from there to end-of-line
        with ``replacement``. Single-line edit so any other content on
        prior lines is untouched.
        """
        try:
            row, col = self.cursor_location
            line = self.document.get_line(row)
        except Exception:
            return
        # Find the start of the trailing token. We respect the same
        # heuristic the App used for Input.value: "last whitespace-
        # separated token". For a multi-line paste with ``@file`` on
        # the last typed line, the prior lines stay intact.
        if not line:
            return
        head = line[:col]
        # Walk back to the most recent whitespace boundary.
        idx = len(head)
        while idx > 0 and not head[idx - 1].isspace():
            idx -= 1
        try:
            self.replace(replacement, (row, idx), (row, len(line)))
        except Exception:
            # Older Textual versions used a different signature; fall
            # back to load_text on a single-line buffer.
            try:
                if self.document.line_count == 1:
                    self.load_text(replacement)
            except Exception:
                pass


class PasteAwarePromptArea(PromptArea):
    """PromptArea with explicit paste annotation.

    Multi-line paste lands in the buffer normally (TextArea handles it
    natively). We surface a brief toast — same UX cue the old single-
    line stash had — so the user knows their N-line paste was received,
    even when they're looking away from the input.
    """

    async def _on_paste(self, event) -> None:
        text = getattr(event, "text", "") or ""
        line_count = text.count("\n") + 1 if text else 0
        if line_count > 1:
            try:
                self.app.notify(
                    f"{line_count} lines pasted",
                    severity="information",
                    timeout=2,
                )
            except Exception:
                pass
        # Don't intercept — let TextArea's normal paste path insert the
        # text into the buffer at the cursor.
        await super()._on_paste(event)
