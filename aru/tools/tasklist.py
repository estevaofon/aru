"""Task list tools for structured step execution.

Provides create_task_list and update_task tools that the executor must call
to plan and track subtasks within each plan step. Inspired by Claude Code
and Antigravity's task management approach.
"""

from __future__ import annotations

import threading

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

_console = Console()
_live = None
_display = None

MAX_SUBTASKS = 10


def set_live(live):
    global _live
    _live = live


def set_display(display):
    global _display
    _display = display


class _TaskStore:
    """Thread-safe store for the current step's subtask list."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: list[dict] = []  # {"index": int, "description": str, "status": str}
        self._created = False

    def create(self, tasks: list[str]) -> list[dict]:
        with self._lock:
            self._tasks = [
                {"index": i + 1, "description": desc, "status": "pending"}
                for i, desc in enumerate(tasks)
            ]
            self._created = True
            return list(self._tasks)

    def update(self, index: int, status: str) -> dict | None:
        with self._lock:
            for task in self._tasks:
                if task["index"] == index:
                    task["status"] = status
                    return dict(task)
            return None

    def get_all(self) -> list[dict]:
        with self._lock:
            return list(self._tasks)

    @property
    def is_created(self) -> bool:
        with self._lock:
            return self._created

    def reset(self):
        with self._lock:
            self._tasks = []
            self._created = False


# Global singleton per executor step (reset between steps)
_store = _TaskStore()


def reset_task_store():
    """Reset the task store between executor steps."""
    _store.reset()


def get_task_store() -> _TaskStore:
    """Get the current task store for inspection."""
    return _store


def _render_task_list(tasks: list[dict]) -> Panel:
    """Render the task list as a Rich panel."""
    lines = []
    for t in tasks:
        if t["status"] == "completed":
            icon = "[bold green]✓[/bold green]"
            style = "dim"
        elif t["status"] == "in_progress":
            icon = "[bold yellow]~[/bold yellow]"
            style = "bold"
        elif t["status"] == "failed":
            icon = "[bold red]✗[/bold red]"
            style = "red"
        else:
            icon = "[dim]○[/dim]"
            style = "dim"
        lines.append(Text.from_markup(f"  {icon} {t['index']}. {t['description']}", style=style))

    return Panel(
        Group(*lines),
        title="[bold cyan]Subtasks[/bold cyan]",
        border_style="cyan",
        expand=True,
    )


def _show(panel: Panel):
    """Display panel using the active display or console."""
    if _display and hasattr(_display, "show_permission"):
        _display.show_permission(panel)
    elif _live:
        _live.console.print(panel)
    else:
        _console.print(panel)


def create_task_list(tasks: list[str]) -> str:
    """Create a subtask list for the current step. MUST be called before any other tool.

    Define 1-10 concrete subtasks that you will execute in order.
    Each subtask should be a single action (Read, Write, Edit, Run).

    Args:
        tasks: List of subtask descriptions. Min 1, max 10.
               Example: ["Read backend/models.py", "Write backend/auth.py", "Edit backend/main.py — add import", "Run pytest"]
    """
    if _store.is_created:
        return "Error: Task list already created for this step. Use update_task to update subtask status."

    if len(tasks) < 1:
        return "Error: Minimum 1 subtask required."

    if len(tasks) > MAX_SUBTASKS:
        return f"Error: Maximum {MAX_SUBTASKS} subtasks allowed. Got {len(tasks)}. Simplify your plan."

    created = _store.create(tasks)
    panel = _render_task_list(created)
    _show(panel)

    task_lines = "\n".join(f"  {t['index']}. {t['description']}" for t in created)
    return f"Task list created ({len(created)} subtasks):\n{task_lines}\n\nNow execute subtask 1."


def update_task(index: int, status: str) -> str:
    """Update the status of a subtask. Call this as you complete each subtask.

    Args:
        index: Subtask number (1-based).
        status: New status — one of: "in_progress", "completed", "failed".
    """
    if not _store.is_created:
        return "Error: No task list exists. Call create_task_list first."

    if status not in ("in_progress", "completed", "failed"):
        return f"Error: Invalid status '{status}'. Use: in_progress, completed, failed."

    updated = _store.update(index, status)
    if not updated:
        return f"Error: Subtask {index} not found."

    # Show updated task list
    all_tasks = _store.get_all()
    panel = _render_task_list(all_tasks)
    _show(panel)

    # Check if all done
    completed_count = sum(1 for t in all_tasks if t["status"] == "completed")
    failed_count = sum(1 for t in all_tasks if t["status"] == "failed")
    total = len(all_tasks)

    if completed_count + failed_count == total:
        return f"All subtasks finished ({completed_count} completed, {failed_count} failed). Step done. Output a brief summary of what was created/changed."

    # Find next pending subtask
    next_task = next((t for t in all_tasks if t["status"] == "pending"), None)
    if next_task:
        return f"Subtask {index} → {status}. Next: subtask {next_task['index']} — {next_task['description']}"

    return f"Subtask {index} → {status}."
