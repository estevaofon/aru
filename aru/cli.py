"""Interactive CLI for aru - a Claude Code clone."""

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

from aru.agents.executor import create_executor
from aru.agents.planner import create_planner
from aru.config import AgentConfig, load_config, render_command_template
from aru.providers import (
    LEGACY_MODEL_ALIASES,
    create_model,
    get_model_display,
    list_providers,
    resolve_model_ref,
)

import io as _io

if sys.platform == "win32" and not hasattr(sys, "_called_from_test"):
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

console = Console()

# Arte ASCII original mantida
aru_logo = """
     ██████▖  ██▗████  ██    ██ 
          ██  ██       ██    ██ 
    ▗███████  ██       ██    ██ 
    ██    ██  ██       ██    ██ 
    ▝████▘██████       ▝████▘██
"""

neon_green = "#39ff14" # Um verde bem "fósforo brilhante"

# Default model reference (provider/model format)
DEFAULT_MODEL = "anthropic/claude-sonnet-4-5"


def _sanitize_input(text: str) -> str:
    """Remove lone UTF-16 surrogates that Windows clipboard can introduce."""
    return text.encode("utf-8", errors="replace").decode("utf-8")


_MENTION_RE = re.compile(r'(?<!\S)@([a-zA-Z0-9_./\\-]+)')
_MENTION_MAX_SIZE = 30_000  # bytes, same limit as read_file


def _resolve_mentions(text: str, cwd: str) -> str:
    """Resolve @file mentions by appending file contents to the message."""
    matches = list(_MENTION_RE.finditer(text))
    if not matches:
        return text

    appendix_parts = []
    seen = set()
    for m in matches:
        rel_path = m.group(1)
        if rel_path in seen:
            continue
        seen.add(rel_path)
        abs_path = os.path.join(cwd, rel_path)
        if not os.path.isfile(abs_path):
            continue
        try:
            size = os.path.getsize(abs_path)
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(_MENTION_MAX_SIZE)
            if size > _MENTION_MAX_SIZE:
                appendix_parts.append(
                    f"\n\n---\nContents of {rel_path} (truncated to {_MENTION_MAX_SIZE // 1000}KB):\n```\n{content}\n```"
                )
            else:
                appendix_parts.append(
                    f"\n\n---\nContents of {rel_path}:\n```\n{content}\n```"
                )
        except OSError:
            continue

    if appendix_parts:
        return text + "".join(appendix_parts)
    return text


TIPS = [
    "Type naturally — aru decides whether to plan or execute.",
    "Use /plan to break down complex tasks before executing.",
    "Place AGENTS.md in project root for custom instructions.",
    "Use .agents/commands/ and .agents/skills/ for extensions.",
    "Use ! <command> to run shell commands directly.",
    "Use /model to switch providers (e.g., /model ollama/llama3.1).",
    "Use /sessions to resume previous conversations.",
]


def _render_home(session: "Session", skip_permissions: bool) -> None:
    """Render a clean home screen inspired by Claude Code."""
    from rich.table import Table

    logo = Text("\n")
    for line in aru_logo.strip("\n").split("\n"):
        logo.append("  " + line + "\n", style=f"bold {neon_green}")
    console.print(logo)
    console.print(
        Text("  A coding agent powered by multiple LLM providers + Agno", style="dim"),
    )
    console.print()

    # Compact command reference
    cmds = Table(show_header=False, box=None, padding=(0, 2), expand=False)
    cmds.add_column(style="bold cyan", min_width=12)
    cmds.add_column(style="dim")
    cmds.add_row("/help", "Show all commands")
    console.print(cmds)
    console.print()

    # Status line
    mode_label = "[red]skip permissions[/red]" if skip_permissions else "[green]safe mode[/green]"
    console.print(
        Text.from_markup(
            f"  [dim]model:[/dim] [bold]{session.model_display}[/bold] [dim]({session.model_id})[/dim]"
            f"  [dim]|[/dim]  {mode_label}"
        )
    )
    console.print(
        Text.from_markup(f"  [dim]cwd:[/dim]   {os.getcwd()}")
    )
    console.print()


SLASH_COMMANDS = [
    ("/help", "Show help and available commands", "/help"),
    ("/plan", "Create an implementation plan", "/plan <task>"),
    ("/model", "Switch model/provider", "/model [provider/model]"),
    ("/sessions", "List recent sessions", "/sessions"),
    ("/commands", "List custom commands", "/commands"),
    ("/skills", "List available skills", "/skills"),
    ("/mcp", "List loaded MCP tools", "/mcp"),
    ("/quit", "Exit aru", "/quit"),
]


class SlashCommandCompleter(Completer):
    """Show slash commands only when '/' is typed as the first character."""

    def __init__(self, custom_commands: dict | None = None):
        self._custom_commands = custom_commands or {}

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        # Only complete when '/' is the first character
        if not text.startswith("/"):
            return
        # Built-in commands
        for cmd, description, usage in SLASH_COMMANDS:
            if cmd.startswith(text):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=HTML(f"<b>{cmd}</b>"),
                    display_meta=description,
                )
        # Custom commands from .agents/commands/
        for name, cmd_def in self._custom_commands.items():
            slash_name = f"/{name}"
            if slash_name.startswith(text):
                yield Completion(
                    slash_name,
                    start_position=-len(text),
                    display=HTML(f"<b>{slash_name}</b>"),
                    display_meta=cmd_def.description,
                )


class FileMentionCompleter(Completer):
    """Show file/directory suggestions when '@' is typed."""

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        # Find the last '@' that is either at start or preceded by whitespace
        idx = text.rfind("@")
        if idx < 0:
            return
        if idx > 0 and not text[idx - 1].isspace():
            return

        partial = text[idx + 1:]  # e.g. "arc/con" from "@arc/con"
        # Split into directory part and name prefix
        if "/" in partial or "\\" in partial:
            # Normalize to forward slashes
            normalized = partial.replace("\\", "/")
            dir_part, name_prefix = normalized.rsplit("/", 1)
            search_dir = os.path.join(os.getcwd(), dir_part)
            rel_prefix = dir_part + "/"
        else:
            dir_part = ""
            name_prefix = partial
            search_dir = os.getcwd()
            rel_prefix = ""

        if not os.path.isdir(search_dir):
            return

        from aru.tools.gitignore import is_ignored
        cwd = os.getcwd()

        try:
            entries = sorted(os.listdir(search_dir))
        except OSError:
            return

        count = 0
        for entry in entries:
            if count >= 50:  # limit suggestions
                break
            if not entry.lower().startswith(name_prefix.lower()):
                continue

            full_path = os.path.join(search_dir, entry)
            rel_path = os.path.relpath(full_path, cwd).replace("\\", "/")

            # Skip gitignored entries
            if is_ignored(rel_path, cwd):
                continue
            # Skip hidden files/dirs
            if entry.startswith("."):
                continue

            is_dir = os.path.isdir(full_path)
            display_text = rel_prefix + entry + ("/" if is_dir else "")
            meta = "dir" if is_dir else ""

            yield Completion(
                display_text,
                start_position=-len(partial),
                display=HTML(f"<b>@{display_text}</b>"),
                display_meta=meta,
            )
            count += 1


class AruCompleter(Completer):
    """Merges slash-command and @file completions."""

    def __init__(self, custom_commands: dict | None = None):
        self._slash = SlashCommandCompleter(custom_commands)
        self._mention = FileMentionCompleter()

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/"):
            yield from self._slash.get_completions(document, complete_event)
        elif "@" in text:
            yield from self._mention.get_completions(document, complete_event)


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


def _create_prompt_session(paste_state: PasteState, config: AgentConfig | None = None) -> PromptSession:
    """Create a prompt_toolkit session with smart paste detection."""
    bindings = KeyBindings()

    @bindings.add(Keys.Escape, Keys.Enter)
    def _newline(event):
        """Escape+Enter inserts a newline for manual multi-line editing."""
        event.current_buffer.insert_text("\n")

    custom_cmds = config.commands if config else {}
    session = PromptSession(
        key_bindings=bindings,
        multiline=False,
        enable_open_in_editor=False,
        completer=AruCompleter(custom_cmds),
        complete_while_typing=True,
    )

    @bindings.add(Keys.BracketedPaste)
    def _handle_paste(event):
        """Intercept multi-line pastes: store content and show line count."""
        data = event.data
        lines = data.splitlines()
        if len(lines) > 1:
            paste_state.set(data)
            # Preserve text typed before the paste (e.g., "/plan ")
            existing_text = event.current_buffer.text
            event.current_buffer.reset()
            if existing_text.strip():
                event.current_buffer.insert_text(existing_text)
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
You are aru, an AI coding assistant. You help users with software engineering tasks.

You have access to tools for reading, writing, and editing files, searching the codebase, running shell commands, searching the web (web_search) and fetching web pages (web_fetch), and delegating subtasks to sub-agents.

Use delegate_task when you can split work into independent subtasks that benefit from parallel execution. \
For example, researching one part of the codebase while modifying another, or implementing changes in \
unrelated files simultaneously. You can call delegate_task multiple times in a single response to run sub-agents in parallel.

Be concise and direct. Focus on doing the work, not explaining what you'll do.
When creating or updating multiple independent files, use write_files to batch them in a single call instead of calling write_file repeatedly.
When making independent edits across files, use edit_files to batch them in a single call instead of calling edit_file repeatedly.
ALWAYS read the project's README.md first if it exists to understand the project context.
NEVER create documentation files (*.md) unless the user explicitly asks for them. This includes README.md, CHANGELOG.md, CONTRIBUTING.md, SETUP.md, and any other markdown files. A single README.md with basic usage is acceptable only when creating a new project from scratch — nothing more. Focus on writing working code, not documentation.

{extra_instructions}
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
        self.model_ref: str = DEFAULT_MODEL  # provider/model format
        self.cwd: str = os.getcwd()
        self.created_at: str = time.strftime("%Y-%m-%d %H:%M:%S")
        self.updated_at: str = self.created_at
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cache_read_tokens: int = 0
        self.total_cache_write_tokens: int = 0
        self.api_calls: int = 0

    @property
    def model_id(self) -> str:
        """Resolve to the actual model ID for the API."""
        from aru.providers import _get_actual_model_id, get_provider
        provider_key, model_name = resolve_model_ref(self.model_ref)
        provider = get_provider(provider_key)
        if provider:
            return _get_actual_model_id(provider, model_name)
        return model_name

    @property
    def model_display(self) -> str:
        return get_model_display(self.model_ref)

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

    def track_tokens(self, metrics):
        """Accumulate token usage from a RunCompletedEvent.metrics."""
        if metrics is None:
            return
        self.total_input_tokens += getattr(metrics, "input_tokens", 0) or 0
        self.total_output_tokens += getattr(metrics, "output_tokens", 0) or 0
        self.total_cache_read_tokens += getattr(metrics, "cache_read_tokens", 0) or 0
        self.total_cache_write_tokens += getattr(metrics, "cache_write_tokens", 0) or 0
        self.api_calls += 1

    @property
    def token_summary(self) -> str:
        total = self.total_input_tokens + self.total_output_tokens
        if total == 0:
            return ""
        metrics_str = f"in: {self.total_input_tokens:,} / out: {self.total_output_tokens:,}"
        if self.total_cache_read_tokens > 0:
            metrics_str += f" / cached: {self.total_cache_read_tokens:,}"
        return f"tokens: {total:,} ({metrics_str}) | calls: {self.api_calls}"

    def add_message(self, role: str, content: str):
        self.history.append({"role": role, "content": content})
        if len(self.history) > 20:
            self.history = self.history[-20:]
        self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "history": self.history,
            "current_plan": self.current_plan,
            "plan_task": self.plan_task,
            "plan_steps": [s.to_dict() for s in self.plan_steps],
            "model_ref": self.model_ref,
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
        # Support both new "model_ref" and legacy "model_key" for backward compat
        model_ref = data.get("model_ref")
        if not model_ref:
            legacy_key = data.get("model_key", "sonnet")
            model_ref = LEGACY_MODEL_ALIASES.get(legacy_key, DEFAULT_MODEL)
        session.model_ref = model_ref
        session.cwd = data.get("cwd", os.getcwd())
        session.created_at = data.get("created_at", "")
        session.updated_at = data.get("updated_at", "")
        return session

    def get_context_summary(self) -> str:
        """Build compact context string from active plan status."""
        parts = []
        if self.current_plan:
            # Send only plan progress (checkboxes), not the full plan text
            parts.append(f"## Active Plan\nTask: {self.plan_task}\n\n{self.render_plan_progress()}")
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


SESSIONS_DIR = os.path.join(".aru", "sessions")


def _generate_session_id() -> str:
    """Generate a short, unique session ID like 'a3f7b2'."""
    raw = f"{time.time()}-{os.getpid()}-{random.randint(0, 999999)}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


class SessionStore:
    """Persist and load sessions from .aru/sessions/."""

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
                    "model": data.get("model_ref", data.get("model_key", "?")),
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


def create_general_agent(session: Session, config: AgentConfig | None = None):
    """Create the general-purpose agent."""
    from agno.agent import Agent

    from aru.tools.codebase import ALL_TOOLS

    extra = config.get_extra_instructions() if config else ""

    return Agent(
        name="Aru",
        model=create_model(session.model_ref, max_tokens=8192),
        tools=ALL_TOOLS,
        instructions=GENERAL_INSTRUCTIONS.format(
            extra_instructions=extra,
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
        from rich.markup import escape
        console.print(f"[red]Error: {escape(str(e))}[/red]")
    console.print()


def _show_help(config: AgentConfig | None):
    """Display help with available commands."""
    from rich.table import Table
    
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Command", style="cyan")
    table.add_column("Description", style="dim")
    
    # Built-in commands
    table.add_row("/plan <task>", "Create detailed implementation plan")
    table.add_row("/model [provider/model]", "Switch models (e.g., ollama/llama3.1, openai/gpt-4o)")
    table.add_row("/sessions", "List recent sessions")
    table.add_row("/commands", "List custom commands")
    table.add_row("/skills", "List available skills")
    table.add_row("/help", "Show this help")
    table.add_row("/quit", "Exit aru")
    table.add_row("! <cmd>", "Run shell command")
    
    # Custom commands
    if config and config.commands:
        table.add_row("", "")  # Separator
        for name, cmd_def in config.commands.items():
            table.add_row(f"/{name}", cmd_def.description)
    
    console.print(table)
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
    """Shows un-flushed streaming content + active tool indicators + status bar.

    Active tools are rendered inline (inside Live) so they're always visible.
    Completed tools are flushed as static output above Live.
    """

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

        # Render active tools with spinner and elapsed time
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


async def run_agent_capture(agent, message: str, session: "Session | None" = None) -> str | None:
    """Run agent with async streaming display and parallel tool execution."""
    from agno.models.message import Message
    from agno.run.agent import (
        RunContentEvent,
        RunOutput,
        ToolCallCompletedEvent,
        ToolCallStartedEvent,
    )

    console.print()
    final_content = None

    try:
        from aru.tools.codebase import set_display, set_live

        status = StatusBar(interval=3.0)
        display = StreamingDisplay(status)
        tracker = display.tool_tracker

        # Build enriched message with environment context
        dynamic_parts = []
        cwd = os.getcwd()
        dynamic_parts.append(f"The current working directory is: {cwd}")

        if session:
            env_context_parts = []
            try:
                from aru.tools.codebase import get_project_tree
                tree_text = get_project_tree(cwd, max_depth=3)
                if tree_text:
                    env_context_parts.append(f"Directory Tree (max depth 3):\n```text\n{tree_text}\n```")
            except Exception:
                pass

            try:
                git_status = subprocess.run(
                    ["git", "status", "-s"], capture_output=True, text=True, cwd=cwd, timeout=2
                ).stdout.strip()
                if git_status:
                    env_context_parts.append(f"Git status:\n{git_status}")
            except Exception:
                pass

            if env_context_parts:
                dynamic_parts.append("## Environment Context\n" + "\n\n".join(env_context_parts))

            # Include active plan progress in context
            if session.current_plan:
                dynamic_parts.append(f"## Active Plan\nTask: {session.plan_task}\n\n{session.render_plan_progress()}")

        dynamic_context = "\n\n".join(dynamic_parts)
        run_message = f"{dynamic_context}\n\n---\n\n## Current Task/Message\n{message}"

        # Build conversation history as real messages for the LLM
        # Exclude the last user message (already in run_message) to avoid duplication
        history_messages: list[Message] = []
        if session and session.history:
            # The last message is the current user input (already added before calling this function)
            prior_history = session.history[:-1]
            for msg in prior_history:
                history_messages.append(Message(role=msg["role"], content=msg["content"], from_history=True))

        # Combine: history messages + current enriched message
        if history_messages:
            history_messages.append(Message(role="user", content=run_message))
            agent_input = history_messages
        else:
            agent_input = run_message

        run_output = None
        with Live(display, console=console, refresh_per_second=10) as live:
            set_live(live)
            set_display(display)
            accumulated = ""
            async for event in agent.arun(agent_input, stream=True, stream_events=True, yield_run_output=True):
                if isinstance(event, RunOutput):
                    run_output = event
                    break

                if isinstance(event, ToolCallStartedEvent):
                    if hasattr(event, "tool") and event.tool:
                        tool_name = event.tool.tool_name or "tool"
                        tool_args = event.tool.tool_args or None
                        tool_id = getattr(event.tool, "tool_call_id", None) or tool_name
                    else:
                        tool_name = getattr(event, "tool_name", "tool")
                        tool_args = getattr(event, "tool_args", None)
                        tool_id = getattr(event, "tool_call_id", None) or tool_name
                    label = _format_tool_label(tool_name, tool_args)
                    # Flush any accumulated content before tool runs
                    if accumulated[display._flushed_len:]:
                        live.stop()
                        display.flush()
                        live.start()
                        live._live_render._shape = None
                    tracker.start(tool_id, label)
                    status.set_text(f"{label}...")
                    live.update(display)

                elif isinstance(event, ToolCallCompletedEvent):
                    if hasattr(event, "tool") and event.tool:
                        tool_id = getattr(event.tool, "tool_call_id", None) or getattr(event.tool, "tool_name", "tool")
                    else:
                        tool_id = getattr(event, "tool_call_id", None) or getattr(event, "tool_name", "tool")

                    result = tracker.complete(tool_id)
                    # Flush completed tools as static output above Live
                    for label, duration in tracker.pop_completed():
                        dur_str = f" {duration:.1f}s" if duration >= 0.5 else ""
                        live.console.print(Text.assemble(
                            ("  ", ""),
                            ("\u2713 ", "bold green"),
                            (label, "dim"),
                            (dur_str, "dim cyan"),
                        ))
                    if not tracker.active_labels:
                        status.resume_cycling()
                    live.update(display)

                elif isinstance(event, RunContentEvent):
                    if hasattr(event, "content") and event.content:
                        accumulated += event.content
                        unflushed = accumulated[display._flushed_len:]

                        # Auto-flush long chunks to prevent rich.Live smearing
                        if unflushed.count("\n") > 15:
                            break_point = unflushed.rfind("\n\n")
                            if break_point == -1:
                                break_point = unflushed.rfind("\n")

                            if break_point != -1:
                                chunk = unflushed[:break_point + 1]
                                # Only flush if we are outside of a code block (balanced ```)
                                if chunk.count("```") % 2 == 0:
                                    live.stop()
                                    console.print(Markdown(chunk))
                                    display._flushed_len += len(chunk)
                                    live.start()
                                    live._live_render._shape = None

                        display.set_content(accumulated)
                        live.update(display)

        set_live(None)
        set_display(None)

        if run_output and session and hasattr(run_output, "metrics"):
            session.track_tokens(run_output.metrics)

        # Print only un-flushed content
        final_content = accumulated or final_content
        remaining = (final_content or "")[display._flushed_len:]
        if remaining:
            console.print(Markdown(remaining))

    except (KeyboardInterrupt, asyncio.CancelledError):
        set_live(None)
        set_display(None)
        console.print("\n[yellow]Interrupted.[/yellow]")
    except Exception as e:
        set_live(None)
        set_display(None)
        from rich.markup import escape
        console.print(f"[red]Error: {escape(str(e))}[/red]")

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
        return await run_agent_capture(executor, exec_prompt, session)

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

        # Build step-specific prompt — compact to save tokens
        step_prompt = (
            f"## Task: {session.plan_task}\n\n"
            f"## Current Step ({step.index}/{len(session.plan_steps)})\n{step.description}\n\n"
            f"## Plan Progress\n{session.render_plan_progress()}\n"
        )
        if completed_context:
            step_prompt += (
                f"\nCompleted steps are marked [x] above. Do NOT repeat them."
            )
        step_prompt += (
            "\n\nIMPORTANT: Just execute. Do NOT summarize or recap what you did."
        )

        # Execute this step
        executor = executor_factory()
        try:
            result = await run_agent_capture(executor, step_prompt, session)
            if result:
                step.status = "completed"
                all_results.append(f"### Step {step.index}: {step.description}\n{result}")
                completed_context += f"\n- Step {step.index} ({step.description}): Done"
            else:
                step.status = "completed"
                completed_context += f"\n- Step {step.index} ({step.description}): Done (no output)"
        except (KeyboardInterrupt, asyncio.CancelledError):
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

    # Final walkthrough — one concise summary of everything that was done
    if all_results:
        console.print()
        console.print("[bold cyan]Generating walkthrough...[/bold cyan]")
        walkthrough_executor = executor_factory()
        walkthrough_prompt = (
            f"## Task: {session.plan_task}\n\n"
            f"All steps are done. Give a brief walkthrough of what was accomplished — "
            f"key changes, files modified, and anything the user should know. Be concise.\n\n"
            f"## Completed Steps\n{session.render_plan_progress()}"
        )
        walkthrough = await run_agent_capture(walkthrough_executor, walkthrough_prompt, session)
        if walkthrough:
            all_results.append(f"### Walkthrough\n{walkthrough}")

    return "\n\n".join(all_results) if all_results else None


async def run_cli(skip_permissions: bool = False, resume_id: str | None = None):
    """Main REPL loop."""
    from aru.tools.codebase import set_console, set_model_id, set_small_model_ref, set_skip_permissions, reset_allowed_actions, set_permission_rules
    set_console(console)
    set_skip_permissions(skip_permissions)

    store = SessionStore()

    def _sync_model(sess: Session):
        """Sync the model IDs to the tools module from the session's model_ref."""
        set_model_id(sess.model_id)
        # Determine small model for sub-agents based on provider
        small_ref = config.model_defaults.get("small") if config else None
        if not small_ref:
            provider_key, _ = resolve_model_ref(sess.model_ref)
            # Use same provider but pick a small/fast model
            _small_defaults = {
                "anthropic": "anthropic/claude-haiku-4-5",
                "openai": "openai/gpt-4o-mini",
                "groq": "groq/llama-3.1-8b-instant",
            }
            small_ref = _small_defaults.get(provider_key, sess.model_ref)
        set_small_model_ref(small_ref)

    # Load project configuration (AGENTS.md, .agents/commands, .agents/skills)
    config = load_config()
    if config.agents_md:
        console.print("[dim]Loaded AGENTS.md[/dim]")
    if config.commands:
        console.print(f"[dim]Loaded {len(config.commands)} custom command(s): {', '.join(f'/{k}' for k in config.commands)}[/dim]")
    if config.skills:
        console.print(f"[dim]Loaded {len(config.skills)} skill(s): {', '.join(config.skills.keys())}[/dim]")
    permission_allow = config.permissions.get("allow", [])
    if permission_allow:
        set_permission_rules(permission_allow)
        console.print(f"[dim]Loaded {len(permission_allow)} permission rule(s)[/dim]")

    extra_instructions = config.get_extra_instructions()

    # Resume or create session
    if resume_id:
        if resume_id == "last":
            session = store.load_last()
        else:
            session = store.load(resume_id)
        if session is None:
            console.print(f"[red]Session not found: {resume_id}[/red]")
            return
        console.print(Markdown(f"# aru - Resuming session `{session.session_id}`"))
        console.print(f"[dim]Title: {session.title}[/dim]")
        console.print(f"[dim]Messages: {len(session.history)} | Created: {session.created_at}[/dim]")
        if session.history:
            console.print(f"[green]Session loaded — {len(session.history)} messages restored.[/green]")
        if session.current_plan:
            console.print(f"[dim]Active plan: {session.plan_task}[/dim]")
            if session.plan_steps:
                completed = sum(1 for s in session.plan_steps if s.status == "completed")
                console.print(f"[dim]Steps: {completed}/{len(session.plan_steps)} completed[/dim]")
        # Restore model
        _sync_model(session)
    else:
        session = Session()
        # Apply default model from config if set
        if config.model_defaults.get("default"):
            session.model_ref = config.model_defaults["default"]
        _render_home(session, skip_permissions)

    planner = None
    executor = None
    paste_state = PasteState()
    prompt_session = _create_prompt_session(paste_state, config)

    # Parallel startup: MCP tools + background index warm-up
    from aru.tools.codebase import load_mcp_tools
    from aru.tools.indexer import warm_up_index

    async def _startup_mcp():
        await load_mcp_tools()

    async def _startup_index():
        await asyncio.to_thread(warm_up_index)

    await asyncio.gather(_startup_mcp(), _startup_index())

    while True:
        try:
            paste_state.clear()
            prompt_session.bottom_toolbar = None
            user_text = (
                await asyncio.to_thread(
                    prompt_session.prompt,
                    HTML(f"<b><cyan>aru</cyan></b> <style fg='ansigray'>({session.model_display})</style><b><cyan>&gt;</cyan></b> "),
                    multiline=False,
                )
            ).strip()
        except (EOFError, KeyboardInterrupt, asyncio.CancelledError):
            store.save(session)
            console.print(f"\n[dim]Session saved: {session.session_id}[/dim]")
            console.print(f"[dim]Resume with:[/dim] [bold cyan]aru --resume {session.session_id}[/bold cyan]")
            console.print("[dim]Bye![/dim]")
            from aru.tools.mcp_client import cleanup_mcp
            await cleanup_mcp()
            break

        user_input = _sanitize_input(paste_state.build_message(user_text))

        # Resolve @file mentions
        resolved = _resolve_mentions(user_input, os.getcwd())
        if resolved != user_input:
            injected = resolved.count("Contents of ")
            console.print(f"[dim]Attached {injected} file(s) from @ mentions[/dim]")
            user_input = resolved

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
            console.print(f"[dim]Resume with:[/dim] [bold cyan]aru --resume {session.session_id}[/bold cyan]")
            console.print("[dim]Bye![/dim]")
            from aru.tools.mcp_client import cleanup_mcp
            await cleanup_mcp()
            break

        if user_input.startswith("/model"):
            arg = user_input[6:].strip()
            if not arg:
                console.print(f"[bold]Current model:[/bold] {session.model_display} ({session.model_id})")
                console.print()
                console.print("[bold]Legacy aliases:[/bold]")
                for alias, ref in LEGACY_MODEL_ALIASES.items():
                    console.print(f"  [cyan]{alias}[/cyan] → {ref}")
                console.print()
                console.print("[bold]Providers:[/bold]")
                for pkey, pconfig in list_providers().items():
                    dflt = pconfig.default_model or "—"
                    console.print(f"  [cyan]{pkey}[/cyan] ({pconfig.name}) — default: {dflt}")
                console.print()
                console.print("[dim]Usage: /model <provider/model> (e.g., /model ollama/llama3.1, /model openai/gpt-4o)[/dim]")
            else:
                arg_lower = arg.lower()
                try:
                    # Validate the model reference resolves to a known provider
                    provider_key, model_name = resolve_model_ref(arg_lower)
                    from aru.providers import get_provider
                    provider = get_provider(provider_key)
                    if provider is None:
                        available = ", ".join(sorted(list_providers().keys()))
                        console.print(f"[yellow]Unknown provider '{provider_key}'. Available: {available}[/yellow]")
                    else:
                        session.model_ref = arg_lower if "/" in arg_lower else (
                            LEGACY_MODEL_ALIASES.get(arg_lower, arg_lower)
                        )
                        _sync_model(session)
                        planner = None
                        executor = None
                        console.print(f"[bold green]Switched to {session.model_display}[/bold green] ({session.model_id})")
                except Exception as e:
                    console.print(f"[yellow]Error: {e}[/yellow]")
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
                console.print(f"\n[dim]Resume with: aru --resume <id>[/dim]")
            continue

        if user_input.lower() == "/commands":
            if not config.commands:
                console.print("[dim]No custom commands found. Add .md files to .agents/commands/[/dim]")
            else:
                console.print("[bold]Custom commands:[/bold]\n")
                for name, cmd_def in config.commands.items():
                    console.print(f"  [bold cyan]/{name}[/bold cyan]  [dim]{cmd_def.description}[/dim]")
                console.print(f"\n[dim]Source: .agents/commands/[/dim]")
            continue

        if user_input.lower() == "/skills":
            if not config.skills:
                console.print("[dim]No skills found. Add .md files to .agents/skills/[/dim]")
            else:
                console.print("[bold]Available skills:[/bold]\n")
                for name, skill in config.skills.items():
                    console.print(f"  [bold cyan]{name}[/bold cyan]  [dim]{skill.description}[/dim]")
                console.print(f"\n[dim]Source: .agents/skills/[/dim]")
                console.print(f"\n[dim]Source: .agents/skills/[/dim]")
            continue

        if user_input.lower() == "/mcp":
            from aru.tools.codebase import ALL_TOOLS
            from agno.tools import Function
            mcp_tools = [t for t in ALL_TOOLS if isinstance(t, Function) and getattr(t, "name", "").count("__") > 0]
            if not mcp_tools:
                console.print("[dim]No MCP tools loaded. Check aru.mcp.json config.[/dim]")
            else:
                console.print(f"[bold]Loaded MCP Tools ({len(mcp_tools)}):[/bold]\n")
                for t in mcp_tools:
                    console.print(f"  [bold cyan]{t.name}[/bold cyan]  [dim]{t.description}[/dim]")
            continue

        if user_input.lower() == "/help":
            _show_help(config)
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
                planner = create_planner(session.model_ref, extra_instructions)

            # No need to manually inject session context into prompt; run_agent_capture will do it.
            prompt = task

            plan_content = await run_agent_capture(planner, prompt, session)

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
                        return create_executor(session.model_ref, extra_instructions)

                    result = await execute_plan_steps(session, make_executor)
                    if result:
                        session.add_message("assistant", f"[Execution]\n{result}")

                session.clear_plan()

        elif user_input.startswith("/") and not user_input.startswith("//"):
            # Check for custom commands from .agents/commands/
            parts = user_input[1:].split(None, 1)
            cmd_name = parts[0].lower()
            cmd_args = parts[1] if len(parts) > 1 else ""

            if cmd_name in config.commands:
                cmd_def = config.commands[cmd_name]
                prompt = render_command_template(cmd_def.template, cmd_args)
                console.print(f"[bold magenta]Running /{cmd_name}...[/bold magenta]")

                agent = create_general_agent(session, config)
                session.add_message("user", user_input)
                result = await run_agent_capture(agent, prompt, session)
                if result:
                    session.add_message("assistant", result)
            else:
                console.print(f"[yellow]Unknown command: /{cmd_name}[/yellow]")
                console.print(f"[dim]Built-in: /plan, /model, /sessions, /commands, /skills, /quit[/dim]")
                if config.commands:
                    console.print(f"[dim]Custom: {', '.join(f'/{k}' for k in config.commands)}[/dim]")

        else:
            agent = create_general_agent(session, config)
            session.add_message("user", user_input)
            result = await run_agent_capture(agent, user_input, session)
            if result:
                session.add_message("assistant", result)

        # Show token usage and auto-save
        if session.token_summary:
            console.print(f"[dim]{session.token_summary}[/dim]")
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
    console.print(f"\n[dim]Resume with: aru --resume <id>[/dim]")


def main():
    """Entry point for the aru CLI."""
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
    except (KeyboardInterrupt, asyncio.CancelledError, SystemExit):
        _graceful_exit()


def _graceful_exit():
    """Save session and show resume hint on exit."""
    try:
        store = SessionStore()
        last = store.load_last()
        if last:
            console.print(f"\n[dim]Session saved: {last.session_id}[/dim]")
            console.print(f"[dim]Resume with:[/dim] [bold cyan]aru --resume {last.session_id}[/bold cyan]")
    except Exception:
        pass
    console.print("[dim]Bye![/dim]")
