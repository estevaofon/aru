"""ChoiceModal — selection menu for TuiUI.ask_choice (E7)."""

from __future__ import annotations

from typing import Any, Sequence

from rich.console import RenderableType
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from aru.tui.sanitize import SanitizedRenderable, sanitize_for_terminal


class ChoiceModal(ModalScreen[int | None]):
    """Numbered option menu. ``dismiss(int)`` returns the chosen index.

    Dismiss with ``cancel_value`` (default ``None``) on Esc / Ctrl+C so
    behaviour mirrors :func:`aru.select.select_option`.
    """

    CSS = """
    ChoiceModal {
        align: center middle;
    }
    #choice-box {
        background: $panel;
        border: round $primary;
        padding: 1 2;
        width: auto;
        max-width: 90;
        height: auto;
        max-height: 80%;
    }
    #choice-title {
        color: $accent;
        text-style: bold;
        margin-bottom: 1;
    }
    #choice-details {
        color: $text;
        margin-bottom: 1;
        max-height: 20;
        overflow-y: auto;
    }
    OptionList {
        height: auto;
        max-height: 12;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        options: Sequence[str],
        *,
        title: str | None = None,
        default: int = 0,
        cancel_value: int | None = None,
        details: RenderableType | None = None,
    ) -> None:
        super().__init__()
        self._options = list(options)
        self._title = title
        self._default = max(0, min(default, len(self._options) - 1))
        self._cancel_value = cancel_value
        self._details = details

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-box"):
            if self._title:
                # Sanitise: title may include agent-generated text (plan name,
                # rejection reason, tool label) which can carry C0 escapes
                # that would disable mouse tracking globally — see Layer 10
                # in the chat.py post-mortem.
                yield Label(
                    sanitize_for_terminal(self._title),
                    id="choice-title",
                )
            if self._details is not None:
                # ``details`` is the diff preview / plan summary panel.
                # Diffs over file content readily contain escape bytes when
                # the file does (colored scripts, captured terminal output),
                # making this the most likely entry point for the bug
                # the periodic re-enable timer recovers from.
                yield Static(
                    SanitizedRenderable(self._details),
                    id="choice-details",
                )
            yield OptionList(
                *[
                    Option(sanitize_for_terminal(label), id=str(i))
                    for i, label in enumerate(self._options)
                ],
                id="choice-options",
            )

    def on_mount(self) -> None:
        opts = self.query_one(OptionList)
        opts.focus()
        try:
            opts.highlighted = self._default
        except Exception:
            pass

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        idx = int(event.option.id) if event.option.id else 0
        self.dismiss(idx)

    def action_cancel(self) -> None:
        self.dismiss(self._cancel_value)
