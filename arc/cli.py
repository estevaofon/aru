"""Interactive CLI for arc - a Claude Code clone."""

import asyncio
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.markdown import Markdown
from rich.measure import Measurement
from rich.panel import Panel
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.text import Text

from arc.agents.executor import create_executor
from arc.agents.planner import create_planner

console = Console()

AVAILABLE_MODELS = {
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-20250514",
    "haiku": "claude-haiku-3-5-20241022",
}
DEFAULT_MODEL = "sonnet"


def _sanitize_input(text: str) -> str:
    """Remove lone UTF-16 surrogates that Windows clipboard can introduce."""
    return text.encode("utf-8", errors="replace").decode("utf-8")


WELCOME = """\
# arc

A coding agent powered by Claude + Agno.

**Commands:**
- `/plan <task>` — Create an implementation plan
- `/exec [task]` — Execute the current plan, or a specific task
- `/model [sonnet|opus|haiku]` — Switch model (default: sonnet)
- `/sessions` — List recent sessions
- `! <command>` — Run a shell command directly
- `/quit` — Exit

**CLI flags:** `--resume [id]` to continue a session, `--list` to show sessions.

Or just type naturally — arc will decide whether to plan or execute.
Paste code freely — multi-line paste is detected automatically. Type a message about the paste, then Enter to send.
"""


SLASH_COMMANDS = [
    ("/plan", "Create an implementation plan", "/plan <task>"),
    ("/exec", "Execute the current plan, or a specific task", "/exec [task]"),
    ("/model", "Switch model (sonnet, opus, haiku)", "/model [name]"),
    ("/sessions", "List recent sessions", "/sessions"),
    ("/quit", "Exit arc", "/quit"),
]


class SlashCommandCompleter(Completer):
    """Show slash commands only when '/' is typed as the first character."""

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        # Only complete when '/' is the first character
        if not text.startswith("/"):
            return
        # Match commands that start with what the user typed
        for cmd, description, usage in SLASH_COMMANDS:
            if cmd.startswith(text):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=HTML(f"<b>{cmd}</b>"),
                    display_meta=description,
                )


class PasteState:
    """Tracks pasted content so the user can annotate it."""

    def __init__(self):
        self.pasted_content: str | None = None
        self.line_count: int = 0

    def set(self, content: str):
        lines = content.splitlines()
        self.pasted_content = content
        self.line_count = len(lines)

    def clear(self):
        self.pasted_content = None
        self.line_count = 0

    def build_message(self, user_text: str) -> str:
        """Combine user annotation with pasted content."""
        if self.pasted_content and user_text.strip():
            return f"{user_text.strip()}\n\n```\n{self.pasted_content}\n```"
        if self.pasted_content:
            return self.pasted_content
        return user_text


def _create_prompt_session(paste_state: PasteState) -> PromptSession:
    """Create a prompt_toolkit session with smart paste detection."""
    bindings = KeyBindings()

    @bindings.add(Keys.Escape, Keys.Enter)
    def _newline(event):
        """Escape+Enter inserts a newline for manual multi-line editing."""
        event.current_buffer.insert_text("\n")

    session = PromptSession(
        key_bindings=bindings,
        multiline=False,
        enable_open_in_editor=False,
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
    )

    @bindings.add(Keys.BracketedPaste)
    def _handle_paste(event):
        """Intercept multi-line pastes: store content and show line count."""
        data = event.data
        lines = data.splitlines()
        if len(lines) > 1:
            paste_state.set(data)
            event.current_buffer.reset()
            # Dynamically enable toolbar now that paste exists
            session.bottom_toolbar = HTML(
                f'  <b><style bg="ansiblue" fg="ansiwhite"> {paste_state.line_count} lines pasted </style></b>'
                f'  <i><style fg="ansigray">Type a message about this paste, or press Enter to send as-is</style></i>'
            )
            event.app.invalidate()
        else:
            event.current_buffer.insert_text(data)

    return session

GENERAL_INSTRUCTIONS = """\
You are arc, an AI coding assistant. You help users with software engineering tasks.

You have access to tools for reading, writing, and editing files, searching the codebase, running shell commands, fetching web content, and delegating subtasks to sub-agents.

Use delegate_task when you can split work into independent subtasks that benefit from parallel execution. \
For example, researching one part of the codebase while modifying another, or implementing changes in \
unrelated files simultaneously. You can call delegate_task multiple times in a single response to run sub-agents in parallel.

Be concise and direct. Focus on doing the work, not explaining what you'll do.
When creating or updating multiple independent files, use write_files to batch them in a single call instead of calling write_file repeatedly.
When making independent edits across files, use edit_files to batch them in a single call instead of calling edit_file repeatedly.
NEVER create documentation files (*.md) unless the user explicitly asks for them. This includes README.md, CHANGELOG.md, CONTRIBUTING.md, SETUP.md, and any other markdown files. A single README.md with basic usage is acceptable only when creating a new project from scratch — nothing more. Focus on writing working code, not documentation.
The current working directory is: {cwd}

{context}
"""


class PlanStep:
    """A single step in a structured plan."""

    def __init__(self, index: int, description: str):
        self.index = index
        self.description = description
        self.status: str = "pending"  # pending | in_progress | completed | failed

    @property
    def checkbox(self) -> str:
        if self.status == "completed":
            return "[bold green]\\[x][/bold green]"
        elif self.status == "in_progress":
            return "[bold yellow]\\[~][/bold yellow]"
        elif self.status == "failed":
            return "[bold red]\\[!][/bold red]"
        return "[dim]\\[ ][/dim]"

    def __str__(self) -> str:
        return f"Step {self.index}: {self.description}"

    def to_dict(self) -> dict:
        return {"index": self.index, "description": self.description, "status": self.status}

    @classmethod
    def from_dict(cls, data: dict) -> "PlanStep":
        step = cls(data["index"], data["description"])
        step.status = data.get("status", "pending")
        return step


def parse_plan_steps(plan_text: str) -> list[PlanStep]:
    """Extract structured steps from a plan markdown output.

    Matches lines like:
    - [ ] Step 1: Do something
    - [ ] 1. Do something
    - Step 1: Do something
    - 1. Do something (at start of line or after whitespace)
    """
    steps = []
    # Match checkbox items: - [ ] description
    checkbox_pattern = re.compile(r"^\s*-\s*\[[ x]\]\s*(.+)$", re.MULTILINE)
    # Match numbered items: 1. description or Step 1: description
    numbered_pattern = re.compile(r"^\s*(?:step\s*)?\d+[.:]\s*(.+)$", re.MULTILINE | re.IGNORECASE)

    # Try checkbox format first
    matches = checkbox_pattern.findall(plan_text)
    if matches:
        for i, desc in enumerate(matches, 1):
            # Clean up step prefix if present (e.g., "Step 1: ...")
            cleaned = re.sub(r"^(?:step\s*)?\d+[.:]\s*", "", desc, flags=re.IGNORECASE).strip()
            steps.append(PlanStep(i, cleaned or desc.strip()))
        return steps

    # Fallback: look for numbered items under a "steps" heading
    # Find section that likely contains steps
    sections = re.split(r"^#{1,3}\s+", plan_text, flags=re.MULTILINE)
    for section in sections:
        section_matches = numbered_pattern.findall(section)
        if len(section_matches) >= 2:  # At least 2 steps to be a plan
            for i, desc in enumerate(section_matches, 1):
                cleaned = re.sub(r"^(?:step\s*)?\d+[.:]\s*", "", desc, flags=re.IGNORECASE).strip()
                steps.append(PlanStep(i, cleaned or desc.strip()))
            return steps

    # Last resort: any numbered items in the whole text
    matches = numbered_pattern.findall(plan_text)
    if len(matches) >= 2:
        for i, desc in enumerate(matches, 1):
            cleaned = re.sub(r"^(?:step\s*)?\d+[.:]\s*", "", desc, flags=re.IGNORECASE).strip()
            steps.append(PlanStep(i, cleaned or desc.strip()))

    return steps


class Session:
    """Holds shared state across the conversation."""

    def __init__(self, session_id: str | None = None):
        self.session_id: str = session_id or _generate_session_id()
        self.history: list[dict[str, str]] = []
        self.current_plan: str | None = None
        self.plan_task: str | None = None
        self.plan_steps: list[PlanStep] = []
        self.model_key: str = DEFAULT_MODEL
        self.cwd: str = os.getcwd()
        self.created_at: str = time.strftime("%Y-%m-%d %H:%M:%S")
        self.updated_at: str = self.created_at

    @property
    def model_id(self) -> str:
        return AVAILABLE_MODELS[self.model_key]

    @property
    def title(self) -> str:
        """Generate a short title from the first user message or plan task."""
        if self.plan_task:
            return self.plan_task[:60]
        for msg in self.history:
            if msg["role"] == "user":
                text = msg["content"][:60]
                return text.split("\n")[0]
        return "(empty session)"

    def set_plan(self, task: str, plan_content: str):
        """Store a plan and parse its steps."""
        self.current_plan = plan_content
        self.plan_task = task
        self.plan_steps = parse_plan_steps(plan_content)

    def clear_plan(self):
        """Clear the active plan."""
        self.current_plan = None
        self.plan_task = None
        self.plan_steps = []

    def add_message(self, role: str, content: str):
        self.history.append({"role": role, "content": content})
        if len(self.history) > 40:
            self.history = self.history[-40:]
        self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "history": self.history,
            "current_plan": self.current_plan,
            "plan_task": self.plan_task,
            "plan_steps": [s.to_dict() for s in self.plan_steps],
            "model_key": self.model_key,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        session = cls(session_id=data["session_id"])
        session.history = data.get("history", [])
        session.current_plan = data.get("current_plan")
        session.plan_task = data.get("plan_task")
        session.plan_steps = [PlanStep.from_dict(s) for s in data.get("plan_steps", [])]
        session.model_key = data.get("model_key", DEFAULT_MODEL)
        session.cwd = data.get("cwd", os.getcwd())
        session.created_at = data.get("created_at", "")
        session.updated_at = data.get("updated_at", "")
        return session

    def get_context_summary(self) -> str:
        """Build context string from conversation history and active plan."""
        parts = []
        if self.current_plan:
            parts.append(f"## Active Plan\nTask: {self.plan_task}\n\n{self.current_plan}")
        if self.history:
            parts.append("## Conversation History")
            for msg in self.history[-10:]:
                prefix = "User" if msg["role"] == "user" else "Assistant"
                content = msg["content"]
                if len(content) > 500:
                    content = content[:500] + "..."
                parts.append(f"**{prefix}:** {content}")
        return "\n\n".join(parts)

    def render_plan_progress(self) -> str:
        """Render the plan steps with checkbox status for display."""
        if not self.plan_steps:
            return ""
        lines = []
        completed = sum(1 for s in self.plan_steps if s.status == "completed")
        total = len(self.plan_steps)
        lines.append(f"[bold]Plan Progress ({completed}/{total}):[/bold]")
        for step in self.plan_steps:
            style = ""
            if step.status == "completed":
                style = "green"
            elif step.status == "in_progress":
                style = "yellow"
            elif step.status == "failed":
                style = "red"
            desc = f"[{style}]{step.description}[/{style}]" if style else step.description
            lines.append(f"  {step.checkbox} {desc}")
        return "\n".join(lines)


SESSIONS_DIR = os.path.join(".arc", "sessions")


def _generate_session_id() -> str:
    """Generate a short, unique session ID like 'a3f7b2'."""
    raw = f"{time.time()}-{os.getpid()}-{random.randint(0, 999999)}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


class SessionStore:
    """Persist and load sessions from .arc/sessions/."""

    def __init__(self, base_dir: str | None = None):
        self.base_dir = base_dir or os.path.join(os.getcwd(), SESSIONS_DIR)
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, session_id: str) -> str:
        return os.path.join(self.base_dir, f"{session_id}.json")

    def save(self, session: Session):
        """Save session state to disk."""
        session.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self._path(session.session_id), "w", encoding="utf-8") as f:
            json.dump(session.to_dict(), f, indent=2, ensure_ascii=False)

    def load(self, session_id: str) -> Session | None:
        """Load a session by ID (full or prefix match)."""
        # Try exact match first
        path = self._path(session_id)
        if os.path.isfile(path):
            return self._read(path)

        # Try prefix match
        for filename in os.listdir(self.base_dir):
            if filename.startswith(session_id) and filename.endswith(".json"):
                return self._read(os.path.join(self.base_dir, filename))

        return None

    def _read(self, path: str) -> Session | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Session.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError):
            return None

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """List recent sessions, newest first."""
        sessions = []
        if not os.path.isdir(self.base_dir):
            return sessions

        for filename in os.listdir(self.base_dir):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(self.base_dir, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sessions.append({
                    "session_id": data["session_id"],
                    "title": data.get("plan_task") or self._first_user_msg(data),
                    "model": data.get("model_key", "?"),
                    "messages": len(data.get("history", [])),
                    "updated_at": data.get("updated_at", ""),
                    "cwd": data.get("cwd", ""),
                })
            except (json.JSONDecodeError, OSError, KeyError):
                continue

        sessions.sort(key=lambda s: s["updated_at"], reverse=True)
        return sessions[:limit]

    def _first_user_msg(self, data: dict) -> str:
        for msg in data.get("history", []):
            if msg["role"] == "user":
                return msg["content"][:60].split("\n")[0]
        return "(empty session)"

    def load_last(self) -> Session | None:
        """Load the most recently updated session."""
        sessions = self.list_sessions(limit=1)
        if sessions:
            return self.load(sessions[0]["session_id"])
        return None


def create_general_agent(session: Session):
    """Create the general-purpose agent."""
    from agno.agent import Agent
    from agno.models.anthropic import Claude

    from arc.tools.codebase import ALL_TOOLS

    return Agent(
        name="Arc",
        model=Claude(id=session.model_id),
        tools=ALL_TOOLS,
        instructions=GENERAL_INSTRUCTIONS.format(
            cwd=os.getcwd(),
            context=session.get_context_summary(),
        ),
        markdown=True,
    )


def run_shell(command: str):
    """Run a shell command directly, streaming output to the terminal."""
    console.print()
    console.print(Panel(
        Syntax(command, "bash", theme="monokai"),
        title="[bold]Shell[/bold]",
        border_style="dim",
        expand=False,
    ))
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=os.getcwd(),
            bufsize=1,
        )
        for line in process.stdout:
            console.print(Text(line.rstrip()))
        process.wait()
        if process.returncode != 0:
            console.print(f"[red]Exit code: {process.returncode}[/red]")
    except KeyboardInterrupt:
        process.kill()
        console.print("\n[yellow]Interrupted.[/yellow]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
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
    """A bottom status bar that cycles through fun phrases.

    Renders as a thin separator line + spinner text.  Rich's Live calls
    ``__rich_console__`` on every refresh tick, so we rotate the phrase
    based on wall-clock time — no extra threads needed.
    """

    def __init__(self, interval: float = 3.0):
        self._interval = interval
        self._phrases = list(THINKING_PHRASES)
        random.shuffle(self._phrases)
        self._index = 0
        self._last_switch = time.monotonic()
        self._override: str | None = None

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
        spinner = Spinner("dots", text=f"[dim]{self.current_text}[/dim]", style="cyan")
        yield from spinner.__rich_console__(console, options)

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(1, options.max_width)


TOOL_DISPLAY_NAMES = {
    "read_file": "Read",
    "write_file": "Write",
    "write_files": "Write",
    "edit_file": "Edit",
    "edit_files": "Edit",
    "glob_search": "Glob",
    "grep_search": "Grep",
    "list_directory": "List",
    "bash": "Bash",
    "semantic_search": "Semantic",
    "code_structure": "Structure",
    "find_dependencies": "Deps",
    "rank_files": "Rank",
}

TOOL_PRIMARY_ARG = {
    "read_file": "file_path",
    "write_file": "file_path",
    "edit_file": "file_path",
    "glob_search": "pattern",
    "grep_search": "pattern",
    "list_directory": "directory",
    "bash": "command",
    "semantic_search": "query",
    "code_structure": "file_path",
    "find_dependencies": "file_path",
    "rank_files": "task",
}


def _format_tool_label(tool_name: str, tool_args: dict | None) -> str:
    """Format a tool call into a Claude Code-style label like Read(file_path)."""
    display = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
    if not tool_args:
        return display

    # Batch tools: show count
    if tool_name == "write_files":
        files = tool_args.get("files", [])
        return f"{display}({len(files)} files)"
    if tool_name == "edit_files":
        edits = tool_args.get("edits", [])
        return f"{display}({len(edits)} edits)"

    # Single-arg tools: show the primary arg value
    primary_key = TOOL_PRIMARY_ARG.get(tool_name)
    if primary_key and primary_key in tool_args:
        value = str(tool_args[primary_key])
        # Truncate long values
        if len(value) > 60:
            value = value[:57] + "..."
        return f"{display}({value})"

    return display


class StreamingDisplay:
    """Shows only un-flushed streaming content + status bar.

    Tool activity is printed as static output (above Live), not inside Live.
    When a permission prompt pauses Live, flush() is called to mark current
    content as already printed — so Live doesn't re-render it when it resumes.
    """

    def __init__(self, status_bar: StatusBar):
        self.status_bar = status_bar
        self._flushed_len: int = 0       # how much of accumulated was already printed
        self._accumulated: str = ""       # full accumulated content
        self._content: Markdown | None = None

    def set_content(self, accumulated: str):
        """Update with the full accumulated content; only the un-flushed part is displayed."""
        self._accumulated = accumulated
        delta = accumulated[self._flushed_len:]
        self.content = Markdown(delta) if delta else None

    def flush(self):
        """Print current un-flushed content statically and mark it as flushed."""
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
        yield self.status_bar

    def __rich_measure__(self, rconsole: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(1, options.max_width)


async def run_agent_capture(agent, message: str) -> str | None:
    """Run agent with async streaming display and parallel tool execution."""
    from agno.run.agent import (
        RunCompletedEvent,
        RunContentEvent,
        ToolCallCompletedEvent,
        ToolCallStartedEvent,
    )

    console.print()
    final_content = None

    try:
        from arc.tools.codebase import set_display, set_live

        status = StatusBar(interval=3.0)
        display = StreamingDisplay(status)
        current_tool_label: str | None = None

        with Live(display, console=console, refresh_per_second=10) as live:
            set_live(live)
            set_display(display)
            accumulated = ""
            async for event in agent.arun(message, stream=True):
                if isinstance(event, ToolCallStartedEvent):
                    tool_name = event.tool_name if hasattr(event, "tool_name") else "tool"
                    tool_args = event.tool_args if hasattr(event, "tool_args") else None
                    current_tool_label = _format_tool_label(tool_name, tool_args)
                    # Flush any accumulated content before tool runs
                    if accumulated[display._flushed_len:]:
                        live.stop()
                        display.flush()
                        live.start()
                    status.set_text(f"{current_tool_label}...")
                    live.update(display)

                elif isinstance(event, ToolCallCompletedEvent):
                    if current_tool_label:
                        # Print completed tool as static output above Live
                        live.console.print(Text.assemble(
                            ("  ", ""),
                            ("\u2713 ", "bold green"),
                            (current_tool_label, "dim"),
                        ))
                        current_tool_label = None
                    status.resume_cycling()
                    live.update(display)

                elif isinstance(event, RunContentEvent):
                    if hasattr(event, "content") and event.content:
                        accumulated += event.content
                        display.set_content(accumulated)
                        live.update(display)

                elif isinstance(event, RunCompletedEvent):
                    if hasattr(event, "content") and event.content:
                        final_content = event.content

        set_live(None)
        set_display(None)

        # Print only un-flushed content
        if final_content:
            # RunCompletedEvent returns full content — only print the un-flushed tail
            if display._flushed_len > 0:
                remaining = final_content[display._flushed_len:]
                if remaining:
                    console.print(Markdown(remaining))
            else:
                console.print(Markdown(final_content))
        elif accumulated[display._flushed_len:]:
            final_content = accumulated
            console.print(Markdown(accumulated[display._flushed_len:]))

    except KeyboardInterrupt:
        set_live(None)
        set_display(None)
        console.print("\n[yellow]Interrupted.[/yellow]")
    except Exception as e:
        set_live(None)
        set_display(None)
        console.print(f"[red]Error: {e}[/red]")

    console.print()
    return final_content


def ask_yes_no(prompt: str) -> bool:
    """Ask the user a yes/no question."""
    try:
        answer = console.input(f"[bold yellow]{prompt} (y/n):[/bold yellow] ").strip().lower()
        return answer in ("y", "yes", "s", "sim")
    except (EOFError, KeyboardInterrupt):
        return False


async def execute_plan_steps(session: Session, executor_factory) -> str | None:
    """Execute plan steps one by one with live progress tracking.

    Shows a checkbox progress panel that updates as each step completes.
    Each step runs as a separate executor call with full context.
    """
    if not session.plan_steps:
        # No structured steps — fall back to single execution
        executor = executor_factory()
        exec_prompt = (
            f"Execute the following plan step by step.\n\n"
            f"## Task\n{session.plan_task}\n\n"
            f"## Plan\n{session.current_plan}"
        )
        return await run_agent_capture(executor, exec_prompt)

    all_results = []
    completed_context = ""

    for step in session.plan_steps:
        # Show current progress
        console.print()
        console.print(Panel(
            Text.from_markup(session.render_plan_progress()),
            title="[bold]Plan Progress[/bold]",
            border_style="blue",
            padding=(0, 1),
        ))
        console.print()

        # Mark step as in progress
        step.status = "in_progress"
        console.print(f"[bold yellow]>>> Step {step.index}:[/bold yellow] {step.description}")

        # Build step-specific prompt with context of completed steps
        step_prompt = (
            f"You are executing step {step.index} of a plan.\n\n"
            f"## Overall Task\n{session.plan_task}\n\n"
            f"## Current Step\n{step.description}\n\n"
            f"## Full Plan\n{session.current_plan}\n"
        )
        if completed_context:
            step_prompt += (
                f"\n## Completed Steps Context\n"
                f"These steps have already been done:\n{completed_context}\n"
                f"\nDo NOT repeat work from completed steps. Focus only on the current step."
            )

        # Execute this step
        executor = executor_factory()
        try:
            result = await run_agent_capture(executor, step_prompt)
            if result:
                step.status = "completed"
                all_results.append(f"### Step {step.index}: {step.description}\n{result}")
                completed_context += f"\n- Step {step.index} ({step.description}): Done"
            else:
                step.status = "completed"
                completed_context += f"\n- Step {step.index} ({step.description}): Done (no output)"
        except KeyboardInterrupt:
            step.status = "failed"
            console.print(f"\n[yellow]Step {step.index} interrupted.[/yellow]")
            # Ask if user wants to continue with remaining steps
            if not ask_yes_no("Continue with remaining steps?"):
                break
        except Exception as e:
            step.status = "failed"
            console.print(f"\n[red]Step {step.index} failed: {e}[/red]")
            if not ask_yes_no("Continue with remaining steps?"):
                break

    # Final progress display
    console.print()
    console.print(Panel(
        Text.from_markup(session.render_plan_progress()),
        title="[bold]Plan Complete[/bold]",
        border_style="green" if all(s.status == "completed" for s in session.plan_steps) else "yellow",
        padding=(0, 1),
    ))

    return "\n\n".join(all_results) if all_results else None


async def run_cli(skip_permissions: bool = False, resume_id: str | None = None):
    """Main REPL loop."""
    from arc.tools.codebase import set_console, set_model_id, set_skip_permissions, reset_allowed_actions
    set_console(console)
    set_skip_permissions(skip_permissions)

    store = SessionStore()

    # Resume or create session
    if resume_id:
        if resume_id == "last":
            session = store.load_last()
        else:
            session = store.load(resume_id)
        if session is None:
            console.print(f"[red]Session not found: {resume_id}[/red]")
            return
        console.print(Markdown(f"# arc - Resuming session `{session.session_id}`"))
        console.print(f"[dim]Title: {session.title}[/dim]")
        console.print(f"[dim]Messages: {len(session.history)} | Created: {session.created_at}[/dim]")
        if session.current_plan:
            console.print(f"[dim]Active plan: {session.plan_task}[/dim]")
            if session.plan_steps:
                completed = sum(1 for s in session.plan_steps if s.status == "completed")
                console.print(f"[dim]Steps: {completed}/{len(session.plan_steps)} completed[/dim]")
        # Restore model
        if session.model_key in AVAILABLE_MODELS:
            set_model_id(AVAILABLE_MODELS[session.model_key])
    else:
        console.print(Markdown(WELCOME))
        session = Session()

    console.print(Panel(
        Text(f"Working directory: {os.getcwd()}", style="dim"),
        border_style="blue",
    ))
    mode = "[bold red]skip permissions[/bold red]" if skip_permissions else "[bold green]safe mode[/bold green]"
    model_key = session.model_key
    console.print(f"[dim]Model: [bold]{model_key}[/bold] ({AVAILABLE_MODELS[model_key]}) | {mode} | Session: [bold]{session.session_id}[/bold][/dim]\n")

    planner = None
    executor = None
    paste_state = PasteState()
    prompt_session = _create_prompt_session(paste_state)

    while True:
        try:
            paste_state.clear()
            prompt_session.bottom_toolbar = None
            user_text = (
                await asyncio.to_thread(
                    prompt_session.prompt,
                    HTML(f"<b><cyan>arc</cyan></b> <style fg='ansigray'>({session.model_key})</style><b><cyan>&gt;</cyan></b> "),
                    multiline=False,
                )
            ).strip()
        except (EOFError, KeyboardInterrupt):
            store.save(session)
            console.print(f"\n[dim]Session saved: {session.session_id}[/dim]")
            console.print(f"[dim]Resume with:[/dim] [bold cyan]arc --resume {session.session_id}[/bold cyan]")
            console.print("[dim]Bye![/dim]")
            break

        user_input = _sanitize_input(paste_state.build_message(user_text))

        if paste_state.pasted_content and user_text:
            console.print(
                f"[dim] {paste_state.line_count} lines pasted[/dim]  [cyan]{user_text}[/cyan]"
            )
        elif paste_state.pasted_content:
            console.print(
                f"[dim] {paste_state.line_count} lines pasted[/dim]"
            )

        if not user_input:
            continue

        # Reset "allow all" approvals for each new user message
        reset_allowed_actions()

        if user_input.lower() in ("/quit", "/exit", "quit", "exit"):
            store.save(session)
            console.print(f"[dim]Session saved: {session.session_id}[/dim]")
            console.print(f"[dim]Resume with:[/dim] [bold cyan]arc --resume {session.session_id}[/bold cyan]")
            console.print("[dim]Bye![/dim]")
            break

        if user_input.startswith("/model"):
            arg = user_input[6:].strip().lower()
            if not arg:
                console.print(f"[bold]Current model:[/bold] {session.model_key} ({session.model_id})")
                console.print(f"[dim]Available: {', '.join(AVAILABLE_MODELS.keys())}[/dim]")
            elif arg in AVAILABLE_MODELS:
                session.model_key = arg
                set_model_id(session.model_id)
                planner = None
                executor = None
                console.print(f"[bold green]Switched to {arg}[/bold green] ({AVAILABLE_MODELS[arg]})")
            else:
                console.print(f"[yellow]Unknown model '{arg}'. Available: {', '.join(AVAILABLE_MODELS.keys())}[/yellow]")
            continue

        if user_input.lower() in ("/sessions", "/list"):
            sessions = store.list_sessions()
            if not sessions:
                console.print("[dim]No saved sessions.[/dim]")
            else:
                console.print("[bold]Recent sessions:[/bold]\n")
                for s in sessions:
                    sid = s["session_id"]
                    title = s["title"][:50]
                    msgs = s["messages"]
                    updated = s["updated_at"]
                    model = s["model"]
                    is_current = " [green](current)[/green]" if sid == session.session_id else ""
                    console.print(f"  [bold cyan]{sid}[/bold cyan]  {title}  [dim]({msgs} msgs, {model}, {updated})[/dim]{is_current}")
                console.print(f"\n[dim]Resume with: arc --resume <id>[/dim]")
            continue

        if user_input.startswith("! "):
            cmd = user_input[2:].strip()
            if not cmd:
                console.print("[yellow]Usage: ! <command>[/yellow]")
                continue
            run_shell(cmd)

        elif user_input.startswith("/plan "):
            task = user_input[6:].strip()
            if not task:
                console.print("[yellow]Usage: /plan <task description>[/yellow]")
                continue

            console.print("[bold magenta]Planning...[/bold magenta]")
            if planner is None:
                planner = create_planner(session.model_id)

            context = session.get_context_summary()
            prompt = task
            if context:
                prompt = f"{task}\n\n---\nContext from this session:\n{context}"

            plan_content = await run_agent_capture(planner, prompt)

            if plan_content:
                session.set_plan(task, plan_content)
                session.add_message("user", f"/plan {task}")
                session.add_message("assistant", f"[Plan]\n{plan_content}")

                # Show parsed steps
                if session.plan_steps:
                    console.print(f"\n[bold]{len(session.plan_steps)} steps detected.[/bold]")

                if ask_yes_no("Execute this plan?"):
                    console.print("[bold green]Executing plan...[/bold green]")

                    def make_executor():
                        return create_executor(session.model_id)

                    result = await execute_plan_steps(session, make_executor)
                    if result:
                        session.add_message("assistant", f"[Execution]\n{result}")

        elif user_input.startswith("/exec"):
            task = user_input[5:].strip()

            if not task and session.current_plan:
                console.print(f"[bold green]Executing current plan:[/bold green] [dim]{session.plan_task}[/dim]")

                # Re-parse steps if needed (e.g., after model switch)
                if not session.plan_steps:
                    session.plan_steps = parse_plan_steps(session.current_plan)

                # Reset step statuses for re-execution
                for step in session.plan_steps:
                    step.status = "pending"

                def make_executor():
                    return create_executor(session.model_id)

                result = await execute_plan_steps(session, make_executor)
                if result:
                    session.add_message("user", "/exec (current plan)")
                    session.add_message("assistant", f"[Execution]\n{result}")
            elif not task:
                console.print("[yellow]No active plan. Usage: /exec <task> or /plan first.[/yellow]")
            else:
                console.print("[bold green]Executing...[/bold green]")

                executor = create_executor(session.model_id)
                context = session.get_context_summary()
                prompt = task
                if context:
                    prompt = f"{task}\n\n---\nContext from this session:\n{context}"

                result = await run_agent_capture(executor, prompt)
                if result:
                    session.add_message("user", f"/exec {task}")
                    session.add_message("assistant", f"[Execution]\n{result}")

        else:
            agent = create_general_agent(session)
            session.add_message("user", user_input)
            result = await run_agent_capture(agent, user_input)
            if result:
                session.add_message("assistant", result)

        # Auto-save session after each interaction
        store.save(session)


def _list_sessions_and_exit():
    """Print saved sessions and exit."""
    store = SessionStore()
    sessions = store.list_sessions()
    if not sessions:
        console.print("[dim]No saved sessions.[/dim]")
        return
    console.print("[bold]Recent sessions:[/bold]\n")
    for s in sessions:
        sid = s["session_id"]
        title = s["title"][:50]
        msgs = s["messages"]
        updated = s["updated_at"]
        model = s["model"]
        console.print(f"  [bold cyan]{sid}[/bold cyan]  {title}  [dim]({msgs} msgs, {model}, {updated})[/dim]")
    console.print(f"\n[dim]Resume with: arc --resume <id>[/dim]")


def main():
    """Entry point for the arc CLI."""
    from dotenv import load_dotenv

    load_dotenv()
    args = sys.argv[1:]
    skip_permissions = "--dangerously-skip-permissions" in args

    # --list: show sessions and exit
    if "--list" in args:
        _list_sessions_and_exit()
        return

    # --resume [id]: resume a session (or "last" if no id given)
    resume_id = None
    if "--resume" in args:
        idx = args.index("--resume")
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            resume_id = args[idx + 1]
        else:
            resume_id = "last"

    try:
        asyncio.run(run_cli(skip_permissions=skip_permissions, resume_id=resume_id))
    except KeyboardInterrupt:
        # Catch Ctrl+C that escapes the REPL (e.g., during y/n prompts)
        # Try to save and show resume hint
        try:
            store = SessionStore()
            last = store.load_last()
            if last:
                console.print(f"\n[dim]Session saved: {last.session_id}[/dim]")
                console.print(f"[dim]Resume with:[/dim] [bold cyan]arc --resume {last.session_id}[/bold cyan]")
        except Exception:
            pass
        console.print("[dim]Bye![/dim]")
