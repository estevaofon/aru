"""SessionPickerScreen — modal for resuming, deleting, or starting sessions.

Triggered by ``Ctrl+S`` or the ``/sessions`` slash. Lists every JSON
under ``.aru/sessions/`` (newest first) with a one-line preview built
from the first user message; the user picks one to resume in place.

Resume flow:

1. Save the current session.
2. Load the chosen session from disk (``SessionStore.load``).
3. Replace ``app.session`` and ``app.ctx.session``.
4. Clear the chat pane and replay the new history.
5. Refresh status / context panes.

This keeps the user inside the same Textual App — no restart, no
``aru --resume <id>`` round-trip.

Delete flow:

* Confirm via ``ConfirmModal``.
* Remove the JSON file.
* Re-render the picker so the deleted entry is gone.
"""

from __future__ import annotations

import os
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from aru.tui.sanitize import sanitize_for_terminal


class SessionPickerScreen(ModalScreen[str | None]):
    """Modal that lists saved sessions and dismisses with the chosen id.

    ``dismiss(<session_id>)`` resumes; ``dismiss(None)`` cancels. The
    App handles the actual swap so this screen stays presentation-only.
    """

    CSS = """
    SessionPickerScreen {
        align: center middle;
    }
    #picker-box {
        background: $panel;
        border: round $primary;
        padding: 1 2;
        width: 90;
        max-width: 95%;
        height: auto;
        max-height: 30;
    }
    #picker-title {
        color: $accent;
        text-style: bold;
        margin-bottom: 1;
    }
    #picker-empty {
        color: $text-muted;
        text-style: italic;
        margin: 1 0;
    }
    #picker-list {
        height: auto;
        max-height: 22;
    }
    #picker-hint {
        margin-top: 1;
        color: $text-muted;
        text-style: italic;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Cancel", show=True),
        Binding("delete", "delete_selected", "Delete", show=True),
        Binding("d", "delete_selected", "Delete", show=False),
    ]

    def __init__(self, store: Any, current_id: str | None = None) -> None:
        super().__init__()
        self._store = store
        self._current_id = current_id
        self._sessions: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Label("Sessions", id="picker-title")
            try:
                self._sessions = list(self._store.list_sessions(limit=50))
            except Exception:
                self._sessions = []
            if not self._sessions:
                yield Static(
                    "No saved sessions yet. Send a message to start one.",
                    id="picker-empty",
                )
            else:
                opts: list[Option] = []
                for s in self._sessions:
                    opts.append(
                        Option(self._format_row(s), id=s.get("session_id", ""))
                    )
                yield OptionList(*opts, id="picker-list")
            yield Static(
                "Enter resume · D / Del delete · Esc cancel",
                id="picker-hint",
            )

    def on_mount(self) -> None:
        if self._sessions:
            try:
                opts = self.query_one(OptionList)
                opts.focus()
                opts.highlighted = 0
            except Exception:
                pass

    def _format_row(self, s: dict) -> str:
        sid = (s.get("session_id") or "?")[:8]
        title = (s.get("title") or "(empty session)").strip()
        title = sanitize_for_terminal(title.replace("\n", " "))[:60]
        msgs = s.get("messages", 0) or 0
        updated = s.get("updated_at") or ""
        marker = "● " if s.get("session_id") == self._current_id else "  "
        return f"{marker}{sid}  {title:<60s}  {msgs:>3d} msg  {updated}"

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        sid = event.option.id
        if sid:
            self.dismiss(sid)

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)

    def action_delete_selected(self) -> None:
        try:
            opts = self.query_one(OptionList)
        except Exception:
            return
        cur = opts.highlighted
        if cur is None or cur < 0 or cur >= opts.option_count:
            return
        opt = opts.get_option_at_index(cur)
        if opt is None or not opt.id:
            return
        sid = opt.id

        from aru.tui.screens.confirm import ConfirmModal

        def _confirmed(yes: bool | None) -> None:
            if not yes:
                return
            self._delete_session(sid)

        self.app.push_screen(
            ConfirmModal(f"Delete session {sid[:8]}? This cannot be undone."),
            _confirmed,
        )

    def _delete_session(self, sid: str) -> None:
        try:
            base_dir = getattr(self._store, "base_dir", "")
            if not base_dir:
                return
            path = os.path.join(base_dir, f"{sid}.json")
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            return
        # Drop the row from the OptionList in place.
        try:
            opts = self.query_one(OptionList)
            for i in range(opts.option_count):
                option = opts.get_option_at_index(i)
                if option and option.id == sid:
                    opts.remove_option(option.id)
                    break
        except Exception:
            pass
