"""Terminal display components: logo, status bar, streaming display, tool tracking."""

from __future__ import annotations

import os
import random
import time

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown
from rich.measure import Measurement
from rich.rule import Rule
from rich.spinner import Spinner
from rich.text import Text

console = Console()

# Arte ASCII original mantida
aru_logo = """
     ██████▖  ██▗████  ██    ██
          ██  ██       ██    ██
    ▗███████  ██       ██    ██
    ██    ██  ██       ██    ██
    ▝████▘██████       ▝████▘██
"""

neon_green = "#39ff14"
shadow_green = "#073e00"


def _build_logo_with_shadow(logo_text: str) -> Text:
    """Build logo Text with a drop-shadow effect for depth."""
    lines = logo_text.strip("\n").split("\n")
    max_width = max(len(l) for l in lines)
    lines = [l.ljust(max_width) for l in lines]

    rows = len(lines)
    cols = max_width

    shadow_dy, shadow_dx = 1, 1
    out_rows = rows + shadow_dy
    out_cols = cols + shadow_dx

    grid = [[" "] * out_cols for _ in range(out_rows)]
    cell_type = [["empty"] * out_cols for _ in range(out_rows)]

    for r, line in enumerate(lines):
        for c, ch in enumerate(line):
            if ch != " ":
                sr, sc = r + shadow_dy, c + shadow_dx
                if cell_type[sr][sc] == "empty":
                    grid[sr][sc] = ch
                    cell_type[sr][sc] = "shadow"

    for r, line in enumerate(lines):
        for c, ch in enumerate(line):
            if ch != " ":
                grid[r][c] = ch
                cell_type[r][c] = "main"

    result = Text("\n")
    for r in range(out_rows):
        result.append("  ")
        for c in range(out_cols):
            ch = grid[r][c]
            ct = cell_type[r][c]
            if ct == "main":
                result.append(ch, style=f"bold {neon_green}")
            elif ct == "shadow":
                result.append(ch, style=shadow_green)
            else:
                result.append(ch)
        result.append("\n")
    return result


def format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _sanitize_input(text: str) -> str:
    """Remove lone UTF-16 surrogates that Windows clipboard can introduce."""
    return text.encode("utf-8", errors="replace").decode("utf-8")


def _render_input_separator() -> None:
    """Print a green separator line above the input prompt."""
    console.print(Rule(style=f"dim {neon_green}"))


def _render_home(session, skip_permissions: bool) -> None:
    """Render a clean home screen inspired by Claude Code."""
    import os

    from rich.table import Table

    from aru import __version__

    logo = _build_logo_with_shadow(aru_logo)
    console.print(logo)
    console.print(
        Text.from_markup(f"  [dim]A coding agent powered by OpenSource[/dim]  [bold {neon_green}]v{__version__}[/bold {neon_green}]"),
    )
    console.print()

    cmds = Table(show_header=False, box=None, padding=(0, 2), expand=False)
    cmds.add_column(style="bold cyan", min_width=12)
    cmds.add_column(style="dim")
    cmds.add_row("/help", "Show all commands")
    console.print(cmds)
    console.print()

    mode_label = "[bold red]🔥 YOLO mode — permissions bypassed[/bold red]" if skip_permissions else "[green]safe mode[/green]"
    console.print(
        Text.from_markup(
            f"  [dim]model:[/dim] [bold]{session.model_display}[/bold] [dim]({session.model_id})[/dim]"
            f"  [dim]|[/dim]  {mode_label}"
        )
    )
    # Prefer ctx.cwd so the "cwd:" line reflects the active worktree (Tier 3 #2).
    # Falls back to os.getcwd() when no ctx is installed (pre-init, tests).
    try:
        from aru.runtime import get_cwd as _get_cwd
        _cwd_display = _get_cwd()
    except Exception:
        _cwd_display = os.getcwd()
    console.print(
        Text.from_markup(f"  [dim]cwd:[/dim]   {_cwd_display}")
    )
    console.print()


THINKING_PHRASES = [
    "Thinking...",
    "Cooking...",
    "Working...",
    "Making magic...",
    "Brewing ideas...",
    "Crunching code...",
    "Connecting the dots...",
    "Crafting a plan...",
    "On it...",
    "Diving deep...",
    "Almost there...",
    "Putting pieces together...",
    "Wiring things up...",
    "Spinning up neurons...",
    "Loading creativity...",
]


class StatusBar:
    """A bottom status bar that cycles through fun phrases."""

    def __init__(self, interval: float = 3.0):
        self._interval = interval
        self._phrases = list(THINKING_PHRASES)
        random.shuffle(self._phrases)
        self._index = 0
        self._last_switch = time.monotonic()
        self._override: str | None = None
        # A single persistent Spinner — Rich's Spinner tracks frames via
        # (time - start_time), so instantiating a new one per render would
        # reset start_time each frame and make the animation look frozen.
        self._spinner = Spinner("dots", text="", style="cyan")

    @property
    def current_text(self) -> str:
        if self._override is not None:
            return self._override
        return self._phrases[self._index % len(self._phrases)]

    def set_text(self, text: str):
        self._override = text

    def resume_cycling(self):
        self._override = None
        self._last_switch = time.monotonic()

    def _maybe_rotate(self):
        now = time.monotonic()
        if now - self._last_switch >= self._interval:
            self._last_switch = now
            self._index += 1
            if self._index >= len(self._phrases):
                random.shuffle(self._phrases)
                self._index = 0
            self._override = None

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        self._maybe_rotate()
        self._spinner.update(text=Text(self.current_text, style="dim"))
        yield from self._spinner.__rich_console__(console, options)

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(1, options.max_width)


TOOL_DISPLAY_NAMES = {
    "read_file": "Read",
    "read_files": "ReadBatch",
    "write_file": "Write",
    "edit_file": "Edit",
    "glob_search": "Glob",
    "grep_search": "Grep",
    "list_directory": "List",
    "bash": "Bash",
    "rank_files": "Rank",
    "delegate_task": "Agent",
}

TOOL_PRIMARY_ARG = {
    "read_file": "file_path",
    "read_files": "paths",
    "write_file": "file_path",
    "edit_file": "file_path",
    "glob_search": "pattern",
    "grep_search": "pattern",
    "list_directory": "directory",
    "bash": "command",
    "rank_files": "task",
    "delegate_task": "task",
}

# Agent type display names for delegate_task
_AGENT_TYPE_LABELS = {
    "explorer": "Explorer",
}


def _format_tool_label(tool_name: str, tool_args: dict | None) -> str:
    """Format a tool call into a Claude Code-style label like Read(file_path)."""
    display = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
    if not tool_args:
        return display

    # Special handling for delegate_task — show agent type and task summary
    if tool_name == "delegate_task":
        agent = str(tool_args.get("agent_name", "") or tool_args.get("agent", ""))
        agent_label = _AGENT_TYPE_LABELS.get(agent, agent.title() if agent else "SubAgent")
        task = str(tool_args.get("task", ""))
        # Extract first meaningful line/sentence as summary
        summary = task.split("\n")[0].strip()
        if len(summary) > 60:
            summary = summary[:57] + "..."
        return f"{agent_label}({summary})" if summary else agent_label

    primary_key = TOOL_PRIMARY_ARG.get(tool_name)
    if primary_key and primary_key in tool_args:
        value = str(tool_args[primary_key])
        if len(value) > 60:
            value = value[:57] + "..."
        return f"{display}({value})"

    return display


def subagent_progress(label: str, tool_name: str, tool_args: dict | None,
                      duration: float | None = None):
    """Print sub-agent tool completion into the active Live context (or console).

    Only called on tool completion — shows a single ✓ line per tool call,
    keeping the output compact (no duplicate start/complete lines).
    """
    from aru.runtime import get_ctx
    try:
        ctx = get_ctx()
    except LookupError:
        return
    tool_label = _format_tool_label(tool_name, tool_args)
    dur_str = f" {duration:.1f}s" if duration and duration >= 0.5 else ""
    line = Text.assemble(
        ("    ", ""),
        ("\u2713 ", "bold green"),
        (f"[{label}] ", "dim"),
        (tool_label, "dim"),
        (dur_str, "dim cyan"),
    )
    target = ctx.live if ctx.live else None
    if target:
        target.console.print(line)
    else:
        ctx.console.print(line)


class ToolTracker:
    """Tracks active tool calls with timing, displayed inside the Live area."""

    def __init__(self):
        self._active: dict[str, tuple[str, float]] = {}  # id -> (label, start_time)
        self._completed: list[tuple[str, float]] = []      # (label, duration)

    def start(self, tool_id: str, label: str):
        self._active[tool_id] = (label, time.monotonic())

    def complete(self, tool_id: str) -> tuple[str, float] | None:
        entry = self._active.pop(tool_id, None)
        if entry:
            label, start = entry
            duration = time.monotonic() - start
            self._completed.append((label, duration))
            return label, duration
        return None

    @property
    def active_labels(self) -> list[tuple[str, float]]:
        """Return (label, elapsed_seconds) for each active tool."""
        now = time.monotonic()
        return [(label, now - start) for label, start in self._active.values()]

    def pop_completed(self) -> list[tuple[str, float]]:
        """Drain and return completed tools since last call."""
        items = self._completed[:]
        self._completed.clear()
        return items


class StreamingDisplay:
    """Shows un-flushed streaming content + active tool indicators + status bar."""

    def __init__(self, status_bar: StatusBar):
        self.status_bar = status_bar
        self.tool_tracker = ToolTracker()
        self._flushed_len: int = 0
        self._accumulated: str = ""
        self._content: Markdown | None = None

    def set_content(self, accumulated: str):
        self._accumulated = accumulated
        delta = accumulated[self._flushed_len:]
        self.content = Markdown(delta) if delta else None

    def flush(self):
        delta = self._accumulated[self._flushed_len:]
        if delta:
            console.print(Markdown(delta))
        self._flushed_len = len(self._accumulated)
        self.content = None

    @property
    def content(self) -> Markdown | None:
        return self._content

    @content.setter
    def content(self, value: Markdown | None):
        self._content = value

    def __rich_console__(self, rconsole: Console, options: ConsoleOptions) -> RenderResult:
        if self._content is not None:
            yield self._content
            yield Text()

        active = self.tool_tracker.active_labels
        if active:
            for label, elapsed in active:
                elapsed_str = f"{elapsed:.1f}s" if elapsed >= 1.0 else ""
                tool_line = Text.assemble(
                    ("  ", ""),
                    ("↻ ", "bold cyan"),
                    (label, "bold"),
                    (f"  {elapsed_str}" if elapsed_str else "", "dim"),
                )
                yield tool_line
            yield Text()

        yield self.status_bar

    def __rich_measure__(self, rconsole: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(1, options.max_width)
