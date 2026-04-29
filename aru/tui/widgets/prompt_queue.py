"""PromptQueueWidget — visible queue of prompts waiting for the agent.

Mounts above the input bar and stays hidden when empty. While the
agent is busy on a turn, additional ``Enter``-submitted prompts are
queued instead of dropped or dispatched in parallel — this widget is
the visible record so the user can:

* See in order what's pending.
* Cancel any entry with the ``[x]`` affordance (click or focus + Enter)
  before it runs.
* Reorder by removing then re-typing (no in-place reorder UI yet).

Drain rule: ``AruApp._drain_prompt_queue()`` is called from
``_run_turn``'s finally block. It pops the oldest pending prompt and
dispatches it as a normal user turn. The widget refreshes as items
disappear; once empty, it auto-hides.

Parity note: this matches the ``useCommandQueue.ts`` affordance in
Claude Code. OpenCode silently blocks during a busy turn, so the user
has no visible record of what they queued — Aru sits between the two
behaviours: queue + visibility + cancel.
"""

from __future__ import annotations

import itertools
from typing import Callable

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static


class PromptQueueWidget(VerticalScroll):
    """Stacked rows representing pending user prompts.

    The widget is its own layout container so each row can be queried,
    refreshed, or removed independently. Hidden via ``display: none``
    when the queue is empty so the input bar sits flush against the
    status pane on the happy path.
    """

    DEFAULT_CSS = """
    PromptQueueWidget {
        display: none;
        max-height: 8;
        background: $boost;
        border-top: solid $primary;
        padding: 0 1;
    }
    PromptQueueWidget.-busy {
        display: block;
    }
    .queue-row {
        height: 1;
        padding: 0;
    }
    """

    # Generates monotonically-increasing IDs for cancel routing. We
    # don't use the user text because the same prompt could appear
    # twice in the queue and we'd lose the order.
    _id_seq = itertools.count(1)

    def __init__(self) -> None:
        super().__init__()
        # Ordered list of (queue_id, prompt_text). Tail = oldest.
        self._items: list[tuple[int, str]] = []
        # Optional drain callback (set by AruApp). Receives no args —
        # the App walks ``items()`` directly when consuming.
        self._on_change: Callable[[], None] | None = None

    # ── App-facing API ──────────────────────────────────────────────

    def enqueue(self, text: str) -> int:
        """Append a prompt to the queue. Returns the row id for cancel."""
        qid = next(self._id_seq)
        self._items.append((qid, text))
        self._refresh_rows()
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass
        return qid

    def pop_next(self) -> str | None:
        """Pop the oldest queued prompt, or ``None`` if empty."""
        if not self._items:
            return None
        _qid, text = self._items.pop(0)
        self._refresh_rows()
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass
        return text

    def cancel(self, queue_id: int) -> bool:
        """Remove ``queue_id`` from the queue; True if found."""
        before = len(self._items)
        self._items = [
            (qid, text) for qid, text in self._items if qid != queue_id
        ]
        if len(self._items) == before:
            return False
        self._refresh_rows()
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass
        return True

    def items(self) -> list[tuple[int, str]]:
        return list(self._items)

    def is_empty(self) -> bool:
        return not self._items

    # ── Rendering ───────────────────────────────────────────────────

    def _refresh_rows(self) -> None:
        # Wipe and rebuild — small lists, simpler than diffing rows.
        for child in list(self.children):
            try:
                child.remove()
            except Exception:
                pass
        if not self._items:
            self.remove_class("-busy")
            return
        self.add_class("-busy")
        for idx, (_qid, text) in enumerate(self._items, start=1):
            preview = text.strip().split("\n", 1)[0][:80]
            if "\n" in text:
                preview = preview + " …"
            label = Text.assemble(
                ("  > ", "bold yellow"),
                (f"#{idx} ", "dim"),
                (preview, "white"),
            )
            row = Static(label, classes="queue-row")
            self.mount(row)
