"""TasklistPanel — sidebar view of plan steps and subtask list.

Mounts inside the right ``#sidebar`` (under ContextPane / LoadedPane)
and stays hidden when both lists are empty. Subscribes to
``tasklist.updated`` and ``plan.updated`` events emitted by
``aru.tools.tasklist`` and renders a Rich panel per list.

Why a sidebar instead of inline-in-chat: the previous approach mounted
each new tasklist render via ``ChatPane.add_renderable`` — over a long
turn that produced 6-8 stacked tasklist boxes, each a slightly newer
snapshot, all visible in the chat history. The sidebar keeps the
current snapshot fresh in one place and the chat clean.

Events consumed:

* ``tasklist.updated`` — payload ``{"tasks": [{index, description,
  status}, ...]}``. ``tasks=[]`` clears the panel.
* ``plan.updated`` — payload ``{"steps": [{index, description,
  status}, ...]}``. ``steps=[]`` clears the panel.

The panel can be hidden / shown via Ctrl+T (mounted as a binding on
the App). When hidden, ``tasklist.py:_show`` falls back to printing
the panel into the ChatPane so the user still sees it.
"""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static


class TasklistPanel(VerticalScroll):
    """Sidebar with two stacked panels: macro plan + executor tasklist."""

    DEFAULT_CSS = """
    TasklistPanel {
        display: none;
        height: auto;
        max-height: 15;
        background: $boost;
        border-left: solid $primary;
        border-bottom: solid $primary;
        padding: 0 1;
        overflow-y: auto;
        overflow-x: hidden;
        scrollbar-size-vertical: 1;
    }
    TasklistPanel.-busy {
        display: block;
    }
    TasklistPanel.-hidden {
        display: none;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._tasks: list[dict] = []
        self._plan: list[dict] = []
        self._content: Static | None = None
        # When True the user explicitly toggled the panel off via Ctrl+T;
        # the auto-show on event arrival is suppressed until they toggle
        # it back on.
        self._user_hidden: bool = False

    def on_mount(self) -> None:
        # Single Static child for the whole panel content. Cheaper than
        # mounting one widget per task row — the Group renderable inside
        # already lays out lines correctly, and re-rendering on every
        # update is O(n) over a small list.
        self._content = Static("")
        self.mount(self._content)
        self._refresh_render()

    # ── Bus callbacks ────────────────────────────────────────────────

    def on_tasklist_updated(self, payload: dict) -> None:
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, list):
            tasks = []
        self._tasks = [dict(t) for t in tasks if isinstance(t, dict)]
        self._refresh_render()

    def on_plan_updated(self, payload: dict) -> None:
        steps = payload.get("steps") if isinstance(payload, dict) else None
        if not isinstance(steps, list):
            steps = []
        self._plan = [dict(s) for s in steps if isinstance(s, dict)]
        self._refresh_render()

    # ── Toggle (Ctrl+T binding from AruApp) ─────────────────────────

    def toggle_visibility(self) -> bool:
        """Flip user-hidden state and update CSS classes. Returns new state."""
        self._user_hidden = not self._user_hidden
        if self._user_hidden:
            self.add_class("-hidden")
        else:
            self.remove_class("-hidden")
            self._refresh_render()
        return self._user_hidden

    # ── Render ──────────────────────────────────────────────────────

    def _refresh_render(self) -> None:
        if self._content is None:
            return
        groups = []
        if self._plan:
            groups.append(self._render_plan_panel())
        if self._tasks:
            groups.append(self._render_tasks_panel())
        if not groups:
            self.remove_class("-busy")
            try:
                self._content.update("")
            except Exception:
                pass
            return
        if self._user_hidden:
            # Don't auto-show when the user explicitly hid the panel,
            # but keep the content fresh so flip-show is instantaneous.
            try:
                self._content.update(Group(*groups))
            except Exception:
                pass
            return
        self.add_class("-busy")
        try:
            self._content.update(Group(*groups))
        except Exception:
            pass

    def _render_tasks_panel(self) -> Panel:
        lines = []
        for t in self._tasks:
            status = t.get("status", "pending")
            idx = t.get("index", 0)
            desc = t.get("description", "")
            if status == "completed":
                icon = "[bold green]✓[/bold green]"
                style = "dim"
            elif status == "in_progress":
                icon = "[bold yellow]~[/bold yellow]"
                style = "bold"
            elif status == "failed":
                icon = "[bold red]✗[/bold red]"
                style = "red"
            else:
                icon = "[dim]○[/dim]"
                style = "dim"
            lines.append(
                Text.from_markup(f"{icon} {idx}. {desc}", style=style)
            )
        return Panel(
            Group(*lines),
            title="[bold cyan]Subtasks[/bold cyan]",
            border_style="cyan",
            expand=True,
        )

    def _render_plan_panel(self) -> Panel:
        icons = {
            "completed": "[bold green]✓[/bold green]",
            "in_progress": "[bold yellow]~[/bold yellow]",
            "failed": "[bold red]✗[/bold red]",
            "skipped": "[dim]·[/dim]",
        }
        styles = {
            "completed": "dim",
            "in_progress": "bold",
            "failed": "red",
            "skipped": "dim italic",
        }
        lines = []
        for s in self._plan:
            status = s.get("status", "pending")
            idx = s.get("index", 0)
            desc = s.get("description", "")
            icon = icons.get(status, "[dim]○[/dim]")
            style = styles.get(status, "dim")
            lines.append(
                Text.from_markup(f"{icon} {idx}. {desc}", style=style)
            )
        return Panel(
            Group(*lines),
            title="[bold cyan]Plan steps[/bold cyan]",
            border_style="cyan",
            expand=True,
        )

    # ── Test / introspection helpers ─────────────────────────────────

    def has_content(self) -> bool:
        return bool(self._tasks or self._plan)

    def task_count(self) -> int:
        return len(self._tasks)

    def plan_step_count(self) -> int:
        return len(self._plan)
