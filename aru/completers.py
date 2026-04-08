"""Input handling: completers, paste detection, mention resolution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from agno.media import Image

from aru.commands import SLASH_COMMANDS
from aru.config import AgentConfig

_MENTION_RE = re.compile(r'(?<!\S)@([a-zA-Z0-9_./\\:-]+)')
_MENTION_MAX_SIZE = 10_000  # bytes — smaller to protect context (model uses read_file for large files)
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_IMAGE_MAX_SIZE = 20 * 1024 * 1024  # 20MB


@dataclass
class MentionResult:
    """Result of resolving @file mentions."""
    text: str                          # User text (without file contents)
    file_messages: list[dict[str, str]]  # Simulated tool-call pairs for history
    images: list[Image]
    count: int                         # Total attached (files + images)


def _resolve_mentions(text: str, cwd: str, agent_names: set[str] | None = None) -> MentionResult:
    """Resolve @file mentions as simulated read_file tool calls.

    Instead of inlining file contents into the user message (which bloats
    history and can't be pruned), we return separate assistant+tool_result
    message pairs that the session can prune/compact like normal tool outputs.

    Image files are returned as Image objects.
    Skips @mentions that match known agent names.
    """
    agent_names = agent_names or set()
    matches = list(_MENTION_RE.finditer(text))
    if not matches:
        return MentionResult(text=text, file_messages=[], images=[], count=0)

    file_messages: list[dict[str, str]] = []
    images: list[Image] = []
    seen = set()
    for m in matches:
        rel_path = m.group(1)
        if rel_path.lower() in agent_names:
            continue
        if rel_path in seen:
            continue
        seen.add(rel_path)
        abs_path = rel_path if os.path.isabs(rel_path) else os.path.join(cwd, rel_path)
        if not os.path.isfile(abs_path):
            continue

        ext = os.path.splitext(rel_path)[1].lower()
        if ext in _IMAGE_EXTENSIONS:
            try:
                size = os.path.getsize(abs_path)
                if size > _IMAGE_MAX_SIZE:
                    continue
                images.append(Image(filepath=abs_path, id=rel_path))
            except OSError:
                pass
            continue

        try:
            size = os.path.getsize(abs_path)
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(_MENTION_MAX_SIZE)
            truncated = size > _MENTION_MAX_SIZE
            label = f"[read_file: {rel_path}]"
            if truncated:
                label += f" (truncated to {_MENTION_MAX_SIZE // 1000}KB of {size // 1000}KB — use read_file for the rest)"
            # Simulated tool call pair — can be pruned like normal tool outputs
            file_messages.append({"role": "assistant", "content": label})
            file_messages.append({"role": "user", "content": content})
        except OSError:
            continue

    count = len(file_messages) // 2 + len(images)
    return MentionResult(text=text, file_messages=file_messages, images=images, count=count)


def _extract_agent_mention(
    text: str, custom_agents: dict
) -> tuple[str, str] | None:
    """Detect @agentname anywhere in the message.

    Returns (agent_name, full_message_text) if found, None otherwise.
    """
    for m in re.finditer(r'(?<!\S)@([a-zA-Z0-9_-]+)', text):
        name = m.group(1).lower()
        if name in custom_agents:
            return name, text
    return None


TIPS = [
    "Type naturally — aru decides whether to plan or execute.",
    "Use /plan to break down complex tasks before executing.",
    "Place AGENTS.md in project root for custom instructions.",
    "Use .agents/commands/ and skills/<name>/SKILL.md for extensions.",
    "Use ! <command> to run shell commands directly.",
    "Use /model to switch providers (e.g., /model ollama/llama3.1).",
    "Use /sessions to resume previous conversations.",
]


class SlashCommandCompleter(Completer):
    """Show slash commands only when '/' is typed as the first character."""

    def __init__(self, custom_commands: dict | None = None, skills: dict | None = None,
                 custom_agents: dict | None = None):
        self._custom_commands = custom_commands or {}
        self._skills = skills or {}
        self._custom_agents = custom_agents or {}

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for cmd, description, usage in SLASH_COMMANDS:
            if cmd.startswith(text):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=HTML(f"<b>{cmd}</b>"),
                    display_meta=description,
                )
        for name, cmd_def in self._custom_commands.items():
            slash_name = f"/{name}"
            if slash_name.startswith(text):
                yield Completion(
                    slash_name,
                    start_position=-len(text),
                    display=HTML(f"<b>{slash_name}</b>"),
                    display_meta=cmd_def.description,
                )
        for name, skill in self._skills.items():
            if not skill.user_invocable:
                continue
            slash_name = f"/{name}"
            if slash_name.startswith(text):
                yield Completion(
                    slash_name,
                    start_position=-len(text),
                    display=HTML(f"<b>{slash_name}</b>"),
                    display_meta=f"[skill] {skill.description}",
                )
        for name, agent_def in self._custom_agents.items():
            if agent_def.mode != "primary":
                continue
            slash_name = f"/{name}"
            if slash_name.startswith(text):
                yield Completion(
                    slash_name,
                    start_position=-len(text),
                    display=HTML(f"<b>{slash_name}</b>"),
                    display_meta=f"[agent] {agent_def.description}",
                )


class FileMentionCompleter(Completer):
    """Show file/directory and agent suggestions when '@' is typed."""

    def __init__(self, custom_agents: dict | None = None):
        self._custom_agents = custom_agents or {}

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        idx = text.rfind("@")
        if idx < 0:
            return
        if idx > 0 and not text[idx - 1].isspace():
            return

        partial = text[idx + 1:]

        if "/" not in partial and "\\" not in partial:
            for name, agent_def in self._custom_agents.items():
                if name.lower().startswith(partial.lower()):
                    yield Completion(
                        name,
                        start_position=-len(partial),
                        display=HTML(f"<b>@{name}</b>"),
                        display_meta=f"[agent] {agent_def.description}",
                    )

        if "/" in partial or "\\" in partial:
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
            if count >= 50:
                break
            if not entry.lower().startswith(name_prefix.lower()):
                continue

            full_path = os.path.join(search_dir, entry)
            rel_path = os.path.relpath(full_path, cwd).replace("\\", "/")

            if is_ignored(rel_path, cwd):
                continue
            if entry.startswith("."):
                continue

            is_dir = os.path.isdir(full_path)
            display_text = rel_prefix + entry + ("/" if is_dir else "")
            file_ext = os.path.splitext(entry)[1].lower()
            is_image = not is_dir and file_ext in _IMAGE_EXTENSIONS
            meta = "dir" if is_dir else ("image" if is_image else "")

            yield Completion(
                display_text,
                start_position=-len(partial),
                display=HTML(f"<b>@{display_text}</b>"),
                display_meta=meta,
            )
            count += 1


class AruCompleter(Completer):
    """Merges slash-command and @file completions."""

    def __init__(self, custom_commands: dict | None = None, skills: dict | None = None,
                 custom_agents: dict | None = None):
        self._slash = SlashCommandCompleter(custom_commands, skills, custom_agents)
        self._mention = FileMentionCompleter(custom_agents)

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
    skills = config.skills if config else {}
    custom_agents = config.custom_agents if config else {}
    session = PromptSession(
        key_bindings=bindings,
        multiline=False,
        enable_open_in_editor=False,
        completer=AruCompleter(custom_cmds, skills, custom_agents),
        complete_while_typing=True,
    )

    @bindings.add(Keys.BracketedPaste)
    def _handle_paste(event):
        """Intercept multi-line pastes: store content and show line count."""
        data = event.data
        lines = data.splitlines()
        if len(lines) > 1:
            paste_state.set(data)
            existing_text = event.current_buffer.text
            event.current_buffer.reset()
            if existing_text.strip():
                event.current_buffer.insert_text(existing_text)
            session.bottom_toolbar = HTML(
                f'<style fg="ansicyan">│</style>  <b><style bg="ansiblue" fg="ansiwhite"> {paste_state.line_count} lines pasted </style></b>'
                f'  <i><style fg="ansigray">Type a message about this paste, or press Enter to send as-is</style></i>'
            )
            event.app.invalidate()
        else:
            event.current_buffer.insert_text(data)

    return session
