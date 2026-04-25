"""InlineChoicePrompt — approval prompt mounted inline in the ChatPane.

Alternative to ``ChoiceModal`` for ``ask_choice`` calls that carry a
``details`` renderable (a diff preview, plan summary, etc.). The modal
version takes over the whole screen and hides the details behind itself
— which defeats the purpose of showing them. Mounting the prompt inline
lets the user scroll the ChatPane freely to review the full preview
above, then press Enter on the prompt to answer.

Contract:

* Caller passes ``on_choice`` — a callback invoked with the selected
  index (or ``cancel_value`` on Esc).
* The widget removes itself after firing the callback, so the ChatPane
  is back to its normal conversation flow.
* OptionList child handles focus + arrow-key navigation + Enter to
  select; Esc on the widget triggers ``cancel_value``.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

from aru.tui.sanitize import sanitize_for_terminal


class InlineChoicePrompt(Widget):
    """Approval prompt rendered as a bordered widget in the ChatPane flow."""

    DEFAULT_CSS = """
    InlineChoicePrompt {
        height: auto;
        border: round $primary;
        padding: 0 2;
        margin: 1 0;
        background: $surface;
    }
    InlineChoicePrompt Label.title {
        color: $accent;
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
    }
    InlineChoicePrompt Label.hint {
        color: $text-muted;
        text-style: italic;
        margin-bottom: 1;
    }
    InlineChoicePrompt OptionList {
        height: auto;
        max-height: 10;
        background: $surface;
        border: none;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        options: Sequence[str],
        *,
        title: str | None = None,
        default: int = 0,
        cancel_value: Any = None,
        on_choice: Callable[[Any], None] | None = None,
        hint: str | None = "Scroll up to review · ↑↓ to navigate · Enter to confirm · Esc to cancel",
    ) -> None:
        super().__init__()
        self._options = list(options)
        self._title = title
        self._default = max(0, min(default, max(0, len(self._options) - 1)))
        self._cancel_value = cancel_value
        self._on_choice = on_choice
        self._hint = hint
        self._fired = False

    def compose(self) -> ComposeResult:
        # Sanitise every caller-supplied string — title, hint, and option
        # labels can carry text from the agent, a tool result, or a plan
        # summary, all of which may contain raw C0 escapes that would
        # disable mouse tracking globally if they reached the terminal.
        # Same boundary as ``ChoiceModal``; see Layer 10 in chat.py.
        if self._title:
            yield Label(sanitize_for_terminal(self._title), classes="title")
        if self._hint:
            yield Label(sanitize_for_terminal(self._hint), classes="hint")
        yield OptionList(
            *[
                Option(sanitize_for_terminal(label), id=str(i))
                for i, label in enumerate(self._options)
            ],
        )

    def on_mount(self) -> None:
        opts = self.query_one(OptionList)
        try:
            opts.highlighted = self._default
        except Exception:
            pass
        opts.focus()
        # Claude-Code parity: hide the text input while the user is
        # making a decision so the approval options are the only
        # interactive target. Restored in on_unmount.
        self._toggle_input(hidden=True)

    def on_unmount(self) -> None:
        # Bring the text input back when the prompt goes away (either
        # the user answered or the widget was programmatically removed).
        self._toggle_input(hidden=False)

    def _toggle_input(self, *, hidden: bool) -> None:
        try:
            inp = self.app.query_one("#input")
        except Exception:
            return
        try:
            if hidden:
                inp.add_class("-hidden")
            else:
                inp.remove_class("-hidden")
                inp.focus()
        except Exception:
            pass

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        idx = int(event.option.id) if event.option.id else 0
        self._fire(idx)

    def action_cancel(self) -> None:
        self._fire(self._cancel_value)

    def _fire(self, value: Any) -> None:
        """Invoke the callback once and unmount the widget.

        ``_fired`` guards against double-dispatch (e.g. Esc right after
        Enter). Callback is cleared before being invoked so a callback
        that re-enters this widget cannot loop.
        """
        if self._fired:
            return
        self._fired = True
        callback = self._on_choice
        self._on_choice = None
        try:
            if callback is not None:
                callback(value)
        finally:
            try:
                self.remove()
            except Exception:
                pass
