"""KeymapScreen — modal overlay listing all TUI keybindings.

Triggered by F1 from anywhere in the app. Groups bindings by context so
the user can find what they need fast: input, chat, mode, search, queue.
ESC or any key closes it.
"""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


# Group → list of (key, description). Mirrors the BINDINGS in AruApp +
# adds context the binding system can't express (e.g. "click + drag in
# chat to select"). Keep this curated — auto-generating from BINDINGS
# would lose grouping and the implicit selection/copy info.
_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Session",
        [
            ("Ctrl+Q", "Save and quit"),
            ("Ctrl+C", "Copy selection / interrupt turn / quit at idle"),
            ("Ctrl+L", "Clear chat pane"),
            ("Ctrl+R", "Recover terminal (mouse / focus / paste)"),
        ],
    ),
    (
        "Modes",
        [
            ("Ctrl+A", "Cycle permission mode (default → acceptEdits → yolo)"),
            ("Ctrl+P", "Toggle plan mode"),
            ("Ctrl+B", "Toggle right sidebar (more chat width)"),
        ],
    ),
    (
        "Input",
        [
            ("Enter", "Send the prompt to the agent"),
            ("Shift+Enter", "Insert a newline (multi-line prompt)"),
            ("Up / Down", "Cycle prior submitted prompts (when input empty)"),
            ("Tab", "Accept slash/file completer suggestion"),
            ("Esc", "Close the completer dropdown"),
            ("! <cmd>", "Run a shell command locally (output streams to chat)"),
        ],
    ),
    (
        "Chat",
        [
            ("Click + drag", "Select text — Ctrl+C copies the selection"),
            ("Ctrl+Y", "Copy last assistant reply (no selection needed)"),
            ("Ctrl+K", "Copy last fenced code block from any assistant reply"),
            ("Ctrl+Shift+Y", "Copy full transcript"),
            ("Ctrl+F", "Search through chat history"),
            ("Mouse wheel", "Scroll the chat pane"),
        ],
    ),
    (
        "Sessions",
        [
            ("Ctrl+S", "Open session picker (resume / delete saved sessions)"),
            ("/sessions", "Same as Ctrl+S — opens the session picker"),
            ("/cost", "Show token usage and estimated cost"),
            ("/compact", "Compact (summarise) the conversation"),
            ("/undo", "Undo the last turn (when supported)"),
        ],
    ),
    (
        "Help",
        [
            ("F1", "Show this keymap overlay"),
            ("/help", "Quick text help in the chat pane"),
        ],
    ),
]


class KeymapScreen(ModalScreen[None]):
    """Modal listing every TUI shortcut and slash command.

    Bound at the App level via F1. Any printable key, ESC, or click
    outside dismisses. The screen is read-only — no actions, no state.
    """

    CSS = """
    KeymapScreen {
        align: center middle;
    }
    #keymap-box {
        background: $panel;
        border: round $primary;
        padding: 1 2;
        width: 78;
        max-width: 90%;
        height: auto;
        max-height: 90%;
    }
    #keymap-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #keymap-body {
        height: auto;
        max-height: 30;
    }
    #keymap-hint {
        margin-top: 1;
        color: $text-muted;
        text-style: italic;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Close", show=True),
        Binding("q", "dismiss_screen", "Close", show=False),
        Binding("f1", "dismiss_screen", "Close", show=False),
        Binding("?", "dismiss_screen", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="keymap-box"):
            yield Static("Aru — keymap & shortcuts", id="keymap-title")
            yield Static(self._render_table(), id="keymap-body")
            yield Static(
                "Press Esc, q, ?, or F1 to close.",
                id="keymap-hint",
            )

    def _render_table(self) -> Table:
        table = Table.grid(padding=(0, 2), expand=True)
        table.add_column(justify="right", style="bold cyan", no_wrap=True)
        table.add_column(justify="left", style="white")

        for i, (group, rows) in enumerate(_GROUPS):
            if i > 0:
                table.add_row("", "")
            table.add_row(
                Text(group, style="bold yellow"),
                Text("", style="dim"),
            )
            for key, desc in rows:
                table.add_row(Text(key), Text(desc))
        return table

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)

    def on_click(self, event) -> None:
        # Click outside the box closes the modal.
        try:
            if event.widget is self:
                self.dismiss(None)
        except Exception:
            pass
