"""SlashCompleter — inline dropdown for slash-command completion (E6c).

Textual doesn't ship a first-class combobox; we build a lightweight one by
placing an ``OptionList`` above the Input that becomes visible while the
current input text starts with ``/``. Arrow keys move through entries,
Enter accepts, Tab also accepts, Esc hides.

Supported completions:

* Slash commands from a static registry (help, clear, plan, memory,
  worktree, subagents, plugin, debug, cost, model, undo, compact,
  skills, agents, commands, mcp, sessions, resume, yolo, quit, exit).
* ``@file`` — resolves against the current cwd via a bounded ``glob``.

The actual text injection stays on the Input; the completer only
populates suggestions.
"""

from __future__ import annotations

import os
import pathlib
from typing import Iterable

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import OptionList
from textual.widgets.option_list import Option


# Canonical slash-command registry for completion. Keeps descriptions
# short so the popup fits on narrow terminals.
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("help",       "Show this help"),
    ("clear",      "Clear chat pane"),
    ("plan",       "Toggle plan mode"),
    ("quit",       "Save and quit"),
    ("exit",       "Save and quit"),
    ("cost",       "Show token usage & cost"),
    ("model",      "Switch model"),
    ("compact",    "Compact conversation"),
    ("memory",     "Auto-extracted project memories"),
    ("worktree",   "Git worktree operations"),
    ("subagents",  "List subagent tree"),
    ("plugin",     "Plugin management"),
    ("debug",      "Debug internals"),
    ("undo",       "Undo last turn"),
    ("skills",     "List available skills"),
    ("agents",     "List custom agents"),
    ("commands",   "List custom commands"),
    ("mcp",        "List loaded MCP tools"),
    ("sessions",   "List recent sessions"),
    ("resume",     "Show resume hint"),
    ("yolo",       "Toggle YOLO mode"),
    ("theme",      "Switch TUI colour theme"),
]


class SlashCompleter(Widget):
    """Dropdown shown above the Input when typing ``/`` or ``@``."""

    DEFAULT_CSS = """
    SlashCompleter {
        display: none;
        background: $panel;
        border: round $primary;
        height: auto;
        max-height: 10;
        margin: 0 1;
    }
    SlashCompleter.-open {
        display: block;
    }
    SlashCompleter > OptionList {
        background: $panel;
        height: auto;
        max-height: 8;
    }
    """

    # Hard cap on entries we render — keeps things snappy on big dirs.
    MAX_SUGGESTIONS: int = 20

    def __init__(self) -> None:
        super().__init__()
        self._current_prefix: str = ""  # "/" or "@" when open, empty otherwise
        self._current_query: str = ""
        # Extra slash entries populated at runtime from config
        # (custom commands, custom agents, skills, plugin subcommands, etc.).
        # Each entry: (name, description).
        self._dynamic_slashes: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield OptionList(id="completer-options")

    # ── API used by AruApp on input change / key events ───────────────

    def is_open(self) -> bool:
        return self.has_class("-open")

    def close(self) -> None:
        if self.has_class("-open"):
            self.remove_class("-open")

    def update_for(self, text: str) -> None:
        """Refresh the dropdown for the current Input text.

        Opens for ``/...`` and ``@...`` prefixes, hides otherwise.
        Trailing whitespace means the user finished the current token —
        we close so accepting a suggestion (which appends a space) does
        not immediately reopen the dropdown for the completed word.
        """
        if not text or text.endswith((" ", "\t", "\n")):
            self.close()
            return
        # Only activate when the *last token* starts with / or @ — this
        # lets the user keep typing after selecting a suggestion.
        last = text.rsplit(None, 1)[-1] if " " in text else text
        if last.startswith("/"):
            query = last[1:]
            entries = list(self._matching_slashes(query))  # instance method now
            self._current_prefix = "/"
        elif last.startswith("@"):
            query = last[1:]
            entries = list(self._matching_files(query))
            self._current_prefix = "@"
        else:
            self.close()
            return

        self._current_query = query
        if not entries:
            self.close()
            return

        opts = self.query_one(OptionList)
        opts.clear_options()
        for value, label in entries[: self.MAX_SUGGESTIONS]:
            opts.add_option(Option(label, id=value))
        self.add_class("-open")
        try:
            opts.highlighted = 0
        except Exception:
            pass

    def set_dynamic_slashes(
        self, entries: list[tuple[str, str]]
    ) -> None:
        """Register extra slash-command entries for completion.

        Replaces the previous dynamic set. Called by ``AruApp`` once the
        config has been loaded (custom commands, custom agents, skills,
        plugin names all go in here).
        """
        # De-duplicate while preserving order.
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for name, desc in entries:
            if name in seen:
                continue
            seen.add(name)
            out.append((name, desc))
        self._dynamic_slashes = out

    # ── Movement helpers (forwarded from App key handlers) ───────────

    def move_up(self) -> None:
        opts = self.query_one(OptionList)
        if opts.option_count == 0:
            return
        cur = opts.highlighted or 0
        opts.highlighted = (cur - 1) % opts.option_count

    def move_down(self) -> None:
        opts = self.query_one(OptionList)
        if opts.option_count == 0:
            return
        cur = opts.highlighted or 0
        opts.highlighted = (cur + 1) % opts.option_count

    def accept(self) -> str | None:
        """Return the current selection inserted as a replacement token.

        Example: user typed ``/he`` and highlighted ``help`` → returns
        ``/help ``. User typed ``@src/ma`` and highlighted ``src/main.py``
        → returns ``@src/main.py ``.
        """
        opts = self.query_one(OptionList)
        cur = opts.highlighted
        if cur is None or cur < 0 or cur >= opts.option_count:
            return None
        option = opts.get_option_at_index(cur)
        if option is None or option.id is None:
            return None
        self.close()
        return f"{self._current_prefix}{option.id} "

    # ── Match providers ───────────────────────────────────────────────

    def _matching_slashes(self, query: str) -> Iterable[tuple[str, Text]]:
        q = query.lower()
        # Merge static registry + dynamic entries. Static first so common
        # commands top the list; dynamic after.
        seen: set[str] = set()
        combined: list[tuple[str, str]] = []
        for name, desc in SLASH_COMMANDS:
            if name in seen:
                continue
            seen.add(name)
            combined.append((name, desc))
        for name, desc in self._dynamic_slashes:
            if name in seen:
                continue
            seen.add(name)
            combined.append((name, desc))
        for name, desc in combined:
            if not q or name.lower().startswith(q):
                label = Text()
                label.append(f"/{name}", style="bold cyan")
                if desc:
                    label.append(f"  {desc}", style="dim")
                yield name, label

    @staticmethod
    def _matching_files(query: str) -> Iterable[tuple[str, Text]]:
        """Best-effort glob against the current cwd.

        Returns paths relative to cwd. Keeps the implementation simple —
        doesn't respect .gitignore (too slow for a popup). Large dirs get
        truncated by ``MAX_SUGGESTIONS``.
        """
        base = pathlib.Path(os.getcwd())
        prefix = query
        try:
            # Walk entries at the relevant directory level.
            if "/" in prefix or "\\" in prefix:
                head, tail = os.path.split(prefix)
                directory = base / head
                stub = tail.lower()
            else:
                directory = base
                stub = prefix.lower()

            if not directory.exists():
                return

            seen = 0
            for entry in sorted(directory.iterdir()):
                if seen >= SlashCompleter.MAX_SUGGESTIONS:
                    break
                name = entry.name
                if stub and not name.lower().startswith(stub):
                    continue
                rel = (entry.relative_to(base)).as_posix()
                kind = "dir" if entry.is_dir() else "file"
                label = Text()
                label.append(f"@{rel}", style="bold yellow")
                label.append(f"  {kind}", style="dim")
                yield rel, label
                seen += 1
        except Exception:
            return
