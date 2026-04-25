"""TextInputModal — free-form text prompt (E7)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label

from aru.tui.sanitize import sanitize_for_terminal


class TextInputModal(ModalScreen[str | None]):
    """Single-line text prompt. ``dismiss(str)`` on Enter, ``dismiss(None)``
    on Esc. Used for rejection feedback / free-form answers.
    """

    CSS = """
    TextInputModal {
        align: center middle;
    }
    #text-box {
        background: $panel;
        border: round $primary;
        padding: 1 2;
        width: 80;
        max-width: 80%;
        height: auto;
    }
    #text-prompt {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(
        self,
        prompt: str,
        *,
        default: str = "",
        placeholder: str = "",
    ) -> None:
        super().__init__()
        self._prompt = prompt
        self._default = default
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="text-box"):
            # Strip C0 controls — see Layer 10 in chat.py post-mortem.
            yield Label(sanitize_for_terminal(self._prompt), id="text-prompt")
            yield Input(
                value=self._default,
                placeholder=self._placeholder,
                id="text-input",
            )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)
