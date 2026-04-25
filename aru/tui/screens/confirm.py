"""ConfirmModal — yes/no for TuiUI.confirm (E7)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from aru.tui.sanitize import sanitize_for_terminal


class ConfirmModal(ModalScreen[bool]):
    """Yes / No dialog. ``dismiss(True)`` on yes, ``dismiss(False)`` on no.

    Keyboard shortcuts: ``y`` / ``n``, ``Enter`` = default choice,
    ``Esc`` = negate default (cancel).
    """

    CSS = """
    ConfirmModal {
        align: center middle;
    }
    #confirm-box {
        background: $panel;
        border: round $primary;
        padding: 1 2;
        width: auto;
        max-width: 70;
        height: auto;
    }
    #confirm-prompt {
        margin-bottom: 1;
    }
    #confirm-buttons {
        height: auto;
        align-horizontal: center;
    }
    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("y", "choose(True)", "Yes", show=True),
        Binding("n", "choose(False)", "No", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, prompt: str, *, default: bool = False) -> None:
        super().__init__()
        self._prompt = prompt
        self._default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            # Strip C0 controls — see Layer 10 in chat.py post-mortem.
            yield Label(sanitize_for_terminal(self._prompt), id="confirm-prompt")
            with Horizontal(id="confirm-buttons"):
                yield Button(
                    "Yes",
                    id="yes",
                    variant="primary" if self._default else "default",
                )
                yield Button(
                    "No",
                    id="no",
                    variant="primary" if not self._default else "default",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_choose(self, value: bool) -> None:
        self.dismiss(value)

    def action_cancel(self) -> None:
        # Esc returns the opposite of the default — matches "I changed my mind".
        self.dismiss(not self._default)
