"""Aru Textual App — full TUI shell (E2 + E3b + E4 + E5 + E6a).

Layout::

    ┌────────────────────────────────────────────────────────┐
    │ AruHeader (branded)                                    │
    ├────────────────────────────────────────────┬───────────┤
    │ ChatPane (scrollable, streams assistant)   │ ToolsPane │
    │                                            │  (live)   │
    ├────────────────────────────────────────────┴───────────┤
    │ StatusPane (session · model · tokens · cost · mode)    │
    ├────────────────────────────────────────────────────────┤
    │ Input (type & Enter → dispatches an agent turn)        │
    ├────────────────────────────────────────────────────────┤
    │ Footer (key hints)                                     │
    └────────────────────────────────────────────────────────┘

* ``aru --tui`` routes here via ``cli.main`` / ``main.py``.
* User Enter → ``run_agent_capture_tui`` in a worker; ``TextualBusSink``
  streams into ChatPane while ToolsPane + StatusPane subscribe to the
  plugin bus for tool/turn/mode/cwd events.
* Ctrl+Q persists the session and exits cleanly.

Scope parked:

* E6c — completers (``/cmd`` / ``@file`` / ``@agent``) + paste preview
* E7  — migrating ``check_permission`` / plan-approval / ``/undo`` to ``ctx.ui``
* E8  — scrollback search + mode cycling bindings
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Input, TextArea

# Loop-saturation tracer (off unless ``ARU_DEBUG_LOOP=1``). Patches
# Textual's ``Driver.process_message`` and ``App._post_message`` at
# import time so every keystroke / message is logged with thread name
# + monotonic timestamp. See ``aru/_debug/loop_tracer.py`` and the
# investigation plan in ``docs/aru/2026-04-30-ctrlc-streaming-plan.md``.
from aru._debug import loop_tracer as _loop_tracer
_loop_tracer.install_textual_patches()

from aru.tui.widgets.chat import ChatPane
from aru.tui.widgets.completer import SlashCompleter
from aru.tui.widgets.context_pane import ContextPane
from aru.tui.widgets.loaded_pane import LoadedPane
from aru.tui.widgets.prompt_area import PasteAwarePromptArea, PromptArea
from aru.tui.widgets.prompt_queue import PromptQueueWidget
from aru.tui.widgets.status import StatusPane
from aru.tui.widgets.subagent_panel import SubagentPanel
from aru.tui.widgets.tasklist_panel import TasklistPanel
from aru.tui.widgets.thinking import ThinkingIndicator
from aru.tui.widgets.tools import ToolsPane


# NOTE: ``PromptArea`` (multi-line ``TextArea`` subclass) lives in
# ``aru.tui.widgets.prompt_area``. The previous ``PromptInput(Input)``
# stash workaround is gone — multi-line pastes now land natively in
# the visible buffer where the user can edit them before sending.


# ── Terminal title helpers ───────────────────────────────────────────
# OSC 0 sets both window and icon (tab) title on xterm-compatible
# terminals (Windows Terminal, iTerm2, GNOME Terminal, Alacritty, etc).
# CSI 22;0t pushes the current title onto the terminal's stack; 23;0t
# pops it back, so we leave the shell's title unchanged when Aru exits.
#
# We write to ``sys.__stdout__`` (the *original* stdout, captured by
# Python at interpreter startup) rather than ``sys.stdout``:
#
# * ``cli.py`` wraps ``sys.stdout`` in a fresh ``TextIOWrapper`` on
#   Windows to force UTF-8. That wrapper doesn't always share flush
#   behaviour with the underlying terminal stream — OSC escapes can get
#   buffered or lost depending on the host (PowerShell + Windows
#   Terminal hit this).
# * Textual's WindowsDriver itself writes to ``sys.__stdout__`` via a
#   dedicated WriterThread, so going the same route keeps our sequences
#   on the same physical handle as the alt-screen frames.
#
# The ``is_headless`` check lives in the callers (``AruApp.on_mount``
# and friends), so the helpers themselves only guard against
# ``sys.__stdout__`` being absent (e.g. ``pythonw.exe`` with no
# console) and then write unconditionally. Terminals that don't grok
# OSC 0 / CSI title-stack just ignore the bytes.

def _set_terminal_title(text: str) -> None:
    out = sys.__stdout__
    if out is None:
        return
    try:
        clean = "".join(ch for ch in text if ch >= " ").strip()
        if len(clean) > 80:
            clean = clean[:77].rstrip() + "…"
        out.write(f"\033]0;{clean}\a")
        out.flush()
    except Exception:
        pass


def _push_terminal_title() -> None:
    out = sys.__stdout__
    if out is None:
        return
    try:
        out.write("\033[22;0t")
        out.flush()
    except Exception:
        pass


def _pop_terminal_title() -> None:
    out = sys.__stdout__
    if out is None:
        return
    try:
        out.write("\033[23;0t")
        out.flush()
    except Exception:
        pass


def _compose_terminal_title(session: Any, pending: str | None = None) -> str:
    """Return an ``aru <summary>`` string for the terminal tab.

    ``pending`` wins when the user has just submitted a turn but the
    message hasn't landed in ``session.history`` yet — lets the caller
    flash the new prompt into the tab title immediately.
    """
    summary = ""
    if pending:
        summary = pending
    elif session is not None:
        try:
            summary = session.title or ""
        except Exception:
            summary = ""
    summary = summary.strip()
    if summary in ("", "(empty session)"):
        return "aru"
    return f"aru — {summary}"


class AruApp(App):
    """The Aru Textual App."""

    CSS = """
    Screen {
        layout: vertical;
        padding: 0;
    }
    #body {
        height: 1fr;
        layout: horizontal;
        padding: 0;
        margin: 0;
    }
    ChatPane {
        /* Give the chat every column we can: no side padding, flex
           ratio 5:1 against the sidebar, and the sidebar itself caps
           at 36 cols so file previews have room to breathe. */
        width: 5fr;
        padding: 0;
        margin: 0;
    }
    ChatPane.-hide-sidebar {
        /* With the sidebar hidden the chat fills the whole row. */
        width: 100%;
    }
    #sidebar {
        width: 1fr;
        min-width: 24;
        max-width: 36;
        layout: vertical;
        padding: 0;
        margin: 0;
    }
    #sidebar.-hidden {
        display: none;
    }
    #input {
        /* PromptArea drives its own height (auto, min 3, max 10).
           Keep margin/padding zero here so the border drawn by the
           widget itself isn't compounded with a host-level frame. */
        margin: 0;
    }
    #input.-hidden {
        /* Hidden while an InlineChoicePrompt is awaiting a decision —
           nudges the user toward the approval options instead of the
           text box (parity with claude-code's approval UX). */
        display: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit_app", "Quit", show=True),
        # Ctrl+C is context-sensitive: copies when there's a text
        # selection (standard TUI behaviour), otherwise interrupts the
        # current agent turn / exits. Implemented in action_ctrl_c.
        # ``priority=True`` so the focused widget can't shadow this. The
        # default focus is ``PromptArea`` (a ``TextArea``), which binds
        # ``ctrl+c`` to its own ``copy`` action — when there's no
        # in-widget selection it raises ``SkipAction`` and the App
        # binding fires, but priority avoids any race where the SkipAction
        # path doesn't propagate.
        Binding("ctrl+c", "ctrl_c", "Copy/Interrupt", show=True, priority=True),
        Binding("ctrl+l", "clear_chat", "Clear chat", show=True),
        Binding("ctrl+a", "cycle_mode", "Mode", show=True),
        Binding("ctrl+p", "toggle_plan", "Plan mode", show=True),
        Binding("ctrl+f", "search_chat", "Search", show=True),
        Binding("ctrl+t", "toggle_tasklist", "Tasklist", show=True),
        Binding("ctrl+i", "focus_input", "Input", show=False),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar", show=True),
        Binding("ctrl+y", "copy_last", "Copy last", show=True),
        Binding("ctrl+shift+y", "copy_all", "Copy all", show=False),
        Binding("ctrl+k", "copy_code", "Copy code", show=False),
        Binding("f1", "show_keymap", "Keys", show=True),
        Binding("ctrl+s", "show_sessions", "Sessions", show=True),
        # Layer 13 — user-invoked terminal recovery. priority=True so the
        # binding fires before any focused widget can absorb the key, in
        # case Textual ever reclassifies ctrl+r as printable on some
        # platform. See ``action_recover_terminal`` for what it does.
        Binding("ctrl+r", "recover_terminal", "Recover", show=True, priority=True),
        Binding("up", "history_prev", "Prev", show=False, priority=False),
        Binding("down", "history_next", "Next", show=False, priority=False),
    ]

    # Slash commands handled locally by the App (no agent round-trip).
    # Extending this map is the cheapest way to add a new local command.
    _LOCAL_SLASH = {
        "clear", "quit", "exit", "help", "plan",
        "cost", "compact", "sessions", "model", "undo",
        "skills", "agents", "commands", "mcp", "yolo",
        "theme",
    }

    # Layer 10 / 12 — interval (seconds) between belt-and-suspenders re-emits
    # of the mouse-tracking enable sequences. Was 8s pre-Layer-12; user
    # report on 2026-04-25 against ``final-fantasy-9/.aru/sessions/b33dfb99``
    # was that the wheel never came back even after the turn ended in YOLO,
    # and 8s was visibly long enough that the user gave up before the next
    # tick. 3s is short enough that a corrupted state self-heals before the
    # next mouse interaction, and the cost is still ~64 bytes per tick (the
    # Layer 12 off-then-on shake — see ``_reenable_mouse_tracking``).
    _MOUSE_REENABLE_INTERVAL: float = 3.0

    # Layer 12 — minimum interval (seconds) between keypress-triggered
    # mouse-tracking re-arms. Each keystroke is an opportunity to recover
    # — if the user is typing it might be precisely BECAUSE the wheel just
    # stopped working — but we don't want a fast typist to turn every
    # keystroke into four extra terminal writes. 500 ms is below human
    # noticeable retry latency yet caps the keystroke→write amplification
    # at ~2 Hz worst case.
    _KEYPRESS_REARM_DEBOUNCE: float = 0.5

    def __init__(
        self,
        *,
        session: Any = None,
        config: Any = None,
        session_store: Any = None,
        ctx: Any = None,
        plugin_manager: Any = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.config = config
        self.session_store = session_store
        self.ctx = ctx
        self.plugin_manager = plugin_manager
        self._busy = False
        # Double-Ctrl+C-to-exit guard. A single Ctrl+C while idle used
        # to quit immediately; users who pressed Ctrl+C to copy a
        # terminal-level mouse selection (where the OS terminal handles
        # the copy and forwards the keystroke to the app) lost their
        # session. Mirrors the Claude Code / Python REPL safeguard: the
        # first idle Ctrl+C arms a 2s window, the second inside that
        # window actually exits.
        self._last_ctrl_c_t: float = 0.0
        # Lightweight input history (E6c minimal). Up/Down cycle through
        # prior turns so the user can re-send or tweak a message.
        self._history: list[str] = []
        self._history_cursor: int | None = None
        # Layer 12 — last time we re-emitted the mouse-tracking enable
        # sequences via the keypress path. Used to debounce per-keystroke
        # re-arming so a fast typist doesn't spam the terminal with re-
        # enables. Initialised to negative infinity so the first keystroke
        # always rearms.
        self._last_mouse_reenable_at: float = float("-inf")

    # ── Composition ──────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        # AruHeader intentionally omitted — the branded logo is printed
        # into the ChatPane on mount instead (see on_mount below).
        with Horizontal(id="body"):
            yield ChatPane()
            with Vertical(id="sidebar"):
                yield ContextPane(session=self.session)
                yield TasklistPanel()
                yield LoadedPane(
                    config=self.config,
                    plugin_manager=self.plugin_manager,
                    ctx=self.ctx,
                )
        yield ThinkingIndicator()
        yield SubagentPanel()
        yield PromptQueueWidget()
        yield StatusPane(session=self.session)
        yield SlashCompleter()
        prompt = PasteAwarePromptArea(id="input")
        # ``placeholder`` isn't a TextArea constructor arg in current
        # Textual; we fake the affordance with a tooltip + the empty
        # state hint kept in the keymap overlay (F1).
        try:
            prompt.tooltip = (
                "Type a message · / commands · @ files · Tab to accept · "
                "Enter to send · Shift+Enter newline"
            )
        except Exception:
            pass
        yield prompt
        yield Footer()

    def _compose_subtitle(self) -> str:
        if self.session is None:
            return ""
        sid = (
            getattr(self.session, "session_id", None)
            or getattr(self.session, "id", None)
            or "?"
        )[:8]
        model = (
            getattr(self.session, "model_ref", None)
            or getattr(self.session, "model_id", None)
            or ""
        )
        bits = [f"session {sid}"]
        if model:
            bits.append(model)
        return " · ".join(bits)

    def on_mount(self) -> None:
        # Block the OS-level SIGINT path to Python. Even with Textual
        # disabling ``ENABLE_PROCESSED_INPUT`` on the console, Windows
        # still routes ``CTRL_C_EVENT`` through Python's signal module,
        # which fires ``KeyboardInterrupt`` on the asyncio main task and
        # tears the App down before any binding can run. We let the
        # keystroke path (Textual's input parser → ``action_ctrl_c``)
        # own Ctrl+C entirely.
        #
        # Why a no-op handler instead of ``SIG_IGN``: on Windows,
        # asyncio's ProactorEventLoop relies on a signal-driven wakeup
        # FD; ``SIG_IGN`` interacts oddly with that path and was
        # observed to swallow keystrokes wholesale. A regular handler
        # absorbs the signal, lets the loop wake, and Textual's input
        # parser still gets the Ctrl+C keystroke through the normal
        # console-input path.
        # Investigation patch (see docs/aru/2026-04-30-ctrlc-streaming-plan.md
        # Fase 4): the Layer-1 hypothesis was that Textual's input parser
        # would receive Ctrl+C as a keystroke once we disabled
        # ``ENABLE_PROCESSED_INPUT``. The 2026-04-30 trace shows it does
        # not — 1182 ``driver.process_message`` events landed in
        # ``loop-trace.log`` with every other keystroke, but **never**
        # ``key=ctrl+c``. Conclusion: on Windows, Python's Console Control
        # Handler intercepts Ctrl+C and turns it into ``SIGINT`` *before*
        # the byte ever reaches the console input buffer that
        # ``ReadConsoleInputW`` drains. The previous no-op handler
        # absorbed the signal so the app didn't die, but the keystroke
        # was lost — Ctrl+C was effectively silent.
        #
        # Fix: keep the signal handler, but make it actively trigger the
        # same path the keystroke would have taken. The handler runs in
        # an unspecified thread (Python's signal delivery thread on
        # Windows), so it must hop back to the loop via
        # ``call_soon_threadsafe``. ``action_ctrl_c`` is idempotent and
        # already context-sensitive (selection / busy / idle), so firing
        # it from here matches the keystroke contract exactly.
        try:
            import signal as _signal
            self._prev_sigint_handler = _signal.getsignal(_signal.SIGINT)

            def _sigint_handler(_sig, _frame, _app=self):
                _loop_tracer.trace("sigint_received", "")
                loop = _app._loop
                if loop is None:
                    return
                try:
                    loop.call_soon_threadsafe(_app._on_sigint_from_handler)
                except Exception:
                    pass

            _signal.signal(_signal.SIGINT, _sigint_handler)
        except (ValueError, OSError, AttributeError):
            self._prev_sigint_handler = None
        # Apply theme from aru.json (best-effort — silent if Textual's
        # stylesheet API has shifted under us). Done before focus / logo
        # so the very first paint already shows the user's colours.
        theme_name = ""
        if self.config is not None:
            theme_name = (getattr(self.config, "theme", "") or "").strip()
        if theme_name:
            try:
                from aru.tui.themes import apply_theme
                apply_theme(self, theme_name)
            except Exception:
                pass
        try:
            self.query_one(PromptArea).focus()
        except Exception:
            pass
        chat = self.query_one(ChatPane)
        # Branded ASCII logo replaces the now-removed AruHeader so the
        # user sees the same "aru" welcome moment as in the REPL.
        # Wrapped in ``Align.center`` so the ASCII art sits centered on
        # the chat width instead of hugging the left margin.
        try:
            from rich.align import Align
            from aru.display import _build_logo_with_shadow, aru_logo
            chat.add_renderable(Align.center(_build_logo_with_shadow(aru_logo)))
        except Exception:
            pass
        # Tagline under the logo — includes the package version so the
        # user always knows which build they're on.
        try:
            from rich.align import Align
            from rich.text import Text as _Text
            try:
                from importlib.metadata import version as _pkg_version
                _v = _pkg_version("aru-code")
            except Exception:
                _v = ""
            tagline = _Text()
            tagline.append("A coding agent powered by OpenSource", style="italic")
            if _v:
                tagline.append(f"  v{_v}", style="dim")
            chat.add_renderable(Align.center(tagline))
            # Two blank rows so the logo+tagline aren't glued to whatever
            # lands underneath (first system line, chat message, or the
            # prompt itself on short terminals).
            from textual.widgets import Static as _Static
            spacer = _Static("")
            spacer.styles.height = 2
            chat.mount(spacer)
        except Exception:
            pass
        # Session/model info already surfaces in the sidebar ContextPane
        # and the bottom StatusPane, so no duplicate welcome line is
        # rendered under the logo — only the tagline above remains.
        self._replay_resumed_history(chat)
        self._install_bus_subscriptions()
        self._populate_completer()
        # Push the shell's title onto the terminal stack, then advertise
        # ``aru`` (or ``aru — <last prompt>`` for resumed sessions) in
        # the tab chrome. ``run_tui`` pops it back when the App exits.
        # Skipped under ``run_test`` (headless) so pytest output stays
        # clean and nobody's real tab gets relabeled during tests.
        if not self.is_headless:
            _push_terminal_title()
            _set_terminal_title(_compose_terminal_title(self.session))
        # Layer 10 / 11 self-heal — periodic recovery of terminal state and
        # input focus. Two failure classes share one tick:
        # * mouse-enable lost (leaked DEC private-mode escape disabled the
        #   wheel) — re-emit ``_enable_mouse_support`` (Layer 10).
        # * input focus / visibility lost (a focusable panel mounted by
        #   ``add_renderable`` grabbed focus, or an ``InlineChoicePrompt``
        #   left ``#input.-hidden`` stuck because its callback raised) —
        #   reassert the prompt as focused-and-visible (Layer 11).
        # Both checks are idempotent on a healthy app and skipped under
        # headless tests where there's no live driver to talk to.
        if not self.is_headless:
            self.set_interval(
                self._MOUSE_REENABLE_INTERVAL,
                self._self_heal_terminal_state,
            )
        # Loop-saturation heartbeat (off unless ``ARU_DEBUG_LOOP=1``).
        # 20 Hz tick on the main loop — gaps > 200 ms are logged as
        # ``loop_blocked`` and pin exactly when/how long the loop was
        # unable to drain a callback.
        try:
            if self._loop is not None:
                _loop_tracer.start_heartbeat(self._loop)
        except Exception:
            pass

    def _replay_resumed_history(self, chat: ChatPane) -> None:
        """Render a resumed session's user/assistant text back into the chat.

        When the user launches ``aru --resume <id>`` (or ``--resume``
        alone for the last session), ``run_tui`` restores ``session.history``
        from disk but the fresh ``ChatPane`` is empty, which makes the
        TUI look like a brand-new session. Replaying the prior turns
        gives the user a visible anchor — they can scroll up, read what
        they were working on, and, critically, see the last prompt they
        sent (the immediate reason they resumed).

        Only ``text`` blocks are replayed; ``tool_use`` / ``tool_result``
        blocks are skipped because a long session can easily accumulate
        dozens of tool rows, and replaying them here would bury the
        human-readable thread. The agent still sees the full block
        history on the next turn via ``session.history``.
        """
        session = self.session
        if session is None or not getattr(session, "history", None):
            return
        try:
            from aru.history_blocks import is_text
        except Exception:
            return

        pairs: list[tuple[str, str]] = []
        for msg in session.history:
            role = msg.get("role") if isinstance(msg, dict) else None
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and is_text(b)]
                text = "\n".join(p for p in parts if p)
            else:
                text = ""
            if text and text.strip():
                pairs.append((role, text))

        if not pairs:
            return

        chat.add_system_message(f"Resumed session · {len(pairs)} message(s) restored")
        for role, text in pairs:
            if role == "user":
                chat.add_user_message(text)
            else:
                chat.start_assistant_message()
                chat.finalize_assistant_message(text)

    def _populate_completer(self) -> None:
        """Feed the SlashCompleter with dynamic entries from config.

        Custom commands (``.agents/commands/*.md``), custom agents
        (``.agents/agents/*.md``), skills (``skills/<name>/SKILL.md``),
        and plugin names all land here so autocomplete surfaces the full
        REPL catalogue, not just the built-in slashes.
        """
        try:
            completer = self.query_one(SlashCompleter)
        except Exception:
            return
        entries: list[tuple[str, str]] = []
        cfg = self.config
        if cfg is not None:
            for name, body in (getattr(cfg, "commands", None) or {}).items():
                desc = ""
                if isinstance(body, str):
                    desc = body.strip().split("\n", 1)[0][:80]
                entries.append((name, f"custom command  {desc}" if desc else "custom command"))
            for name, agent in (getattr(cfg, "custom_agents", None) or {}).items():
                desc = getattr(agent, "description", "") or ""
                mode = getattr(agent, "mode", "")
                label = f"custom agent ({mode})" if mode else "custom agent"
                entries.append((name, f"{label}  {desc[:60]}" if desc else label))
            for name, skill in (getattr(cfg, "skills", None) or {}).items():
                desc = getattr(skill, "description", "") or ""
                entries.append((name, f"skill  {desc[:70]}" if desc else "skill"))
        plugin_mgr = self.plugin_manager or (self.ctx and self.ctx.plugin_manager)
        if plugin_mgr is not None:
            for pname in getattr(plugin_mgr, "plugin_names", []):
                entries.append((pname, "plugin"))
        completer.set_dynamic_slashes(entries)

    def _print_startup_summary(self, chat: ChatPane) -> None:
        """Emit "Loaded …" lines for what was discovered at bootstrap."""
        cfg = self.config
        if cfg is None:
            return
        lines: list[str] = []
        if getattr(cfg, "agents_md", None):
            lines.append("• Loaded AGENTS.md")
        commands = getattr(cfg, "commands", None) or {}
        if commands:
            names = ", ".join(f"/{k}" for k in sorted(commands.keys()))
            lines.append(f"• Loaded {len(commands)} custom command(s): {names}")
        skills = getattr(cfg, "skills", None) or {}
        if skills:
            names = ", ".join(sorted(skills.keys()))
            lines.append(f"• Loaded {len(skills)} skill(s): {names}")
        custom_agents = getattr(cfg, "custom_agents", None) or {}
        if custom_agents:
            primary = [k for k, v in custom_agents.items()
                       if getattr(v, "mode", "") == "primary"]
            subagents = [k for k, v in custom_agents.items()
                         if getattr(v, "mode", "") == "subagent"]
            parts = []
            if primary:
                parts.append(", ".join(f"/{k}" for k in primary))
            if subagents:
                parts.append(f"{len(subagents)} subagent(s)")
            lines.append(
                f"• Loaded {len(custom_agents)} custom agent(s): "
                f"{', '.join(parts) if parts else ''}"
            )
        if getattr(cfg, "permissions", None):
            lines.append("• Loaded permission config")
        plugin_mgr = self.plugin_manager or (self.ctx and self.ctx.plugin_manager)
        if plugin_mgr is not None:
            plugin_count = getattr(plugin_mgr, "plugin_count", 0)
            if plugin_count:
                names = ", ".join(plugin_mgr.plugin_names)
                lines.append(f"• Loaded {plugin_count} plugin(s): {names}")
        ctx = self.ctx
        mcp_msg = getattr(ctx, "mcp_loaded_msg", None) if ctx else None
        if mcp_msg:
            lines.append(f"• {mcp_msg}")
        if lines:
            chat.add_system_message("\n".join(lines))

    # ── Input handling ───────────────────────────────────────────────

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Keep the SlashCompleter in sync with the cursor's current line.

        The completer fires on the *current line text* (not the whole
        document) so a user pasting a multi-line block and then typing
        ``/`` on the last line still pops the menu. ``current_line_text``
        on the PromptArea handles that — the App just hands the result
        to the completer.
        """
        try:
            completer = self.query_one(SlashCompleter)
        except Exception:
            return
        try:
            prompt = self.query_one(PromptArea)
        except Exception:
            return
        if event.text_area is not prompt:
            return
        try:
            line = prompt.current_line_text()
        except Exception:
            line = prompt.value or ""
        completer.update_for(line or "")

    async def on_key(self, event) -> None:
        """Route Tab / Up / Down / Esc to the completer when it is open.

        Enter never reaches here — ``PromptArea`` consumes it and posts
        ``PromptArea.Submitted``. Tab is the only key that accepts a
        completer suggestion; Up/Down move through suggestions when the
        completer is open *and* hand off to the App's history navigation
        otherwise (handled in ``on_prompt_area_history_prev/next``).

        Layer 12 — every keystroke is also a recovery opportunity. The
        Layer 10 periodic tick still runs every ``_MOUSE_REENABLE_INTERVAL``
        but a typing user wants the wheel back NOW, not in three seconds.
        Debounced via ``_KEYPRESS_REARM_DEBOUNCE`` so a fast typist
        doesn't amplify each keystroke into four extra terminal writes.
        """
        self._maybe_rearm_mouse_on_keypress()
        try:
            completer = self.query_one(SlashCompleter)
        except Exception:
            return
        if not completer.is_open():
            return
        key = event.key
        if key == "tab":
            accepted = completer.accept()
            if accepted is not None:
                try:
                    prompt = self.query_one(PromptArea)
                    prompt.replace_current_line_token(accepted)
                except Exception:
                    pass
                event.stop()
                event.prevent_default()
        elif key == "up":
            completer.move_up()
            event.stop()
            event.prevent_default()
        elif key == "down":
            completer.move_down()
            event.stop()
            event.prevent_default()
        elif key == "escape":
            completer.close()
            event.stop()
            event.prevent_default()

    def on_prompt_area_submitted(self, event: "PromptArea.Submitted") -> None:
        """Handle Enter from the multi-line PromptArea.

        Routing rules (same as the previous single-line path, minus the
        invisible-paste stash):

        1. Empty after rstrip → no-op.
        2. ``/...`` → slash command (local handler or fall-through).
        3. ``! ...`` → shell escape.
        4. Anything else → agent turn (or queued if busy — see
           ``_enqueue_or_dispatch``).
        """
        text = (event.text or "").strip("\n")
        # Strip trailing whitespace per line but preserve internal
        # newlines so multi-line prompts retain their structure.
        if not text.strip():
            return
        try:
            prompt = self.query_one(PromptArea)
            prompt.value = ""
        except Exception:
            pass
        try:
            self.query_one(SlashCompleter).close()
        except Exception:
            pass
        self._history.append(text)
        self._history_cursor = None

        if text.startswith("/") and self._maybe_run_local_slash(text):
            return

        if text.startswith("!"):
            cmd = text[1:].lstrip()
            if not cmd:
                self.query_one(ChatPane).add_system_message(
                    "Usage: ! <command>"
                )
                return
            if self._busy:
                self.query_one(ChatPane).add_system_message(
                    "Busy — wait for the current task to finish."
                )
                return
            self._dispatch_shell_command(cmd)
            return

        self._enqueue_or_dispatch(text)

    def _enqueue_or_dispatch(self, text: str) -> None:
        """Send to agent or push onto the visible prompt queue."""
        if self._busy:
            try:
                queue = self.query_one(PromptQueueWidget)
                queue.enqueue(text)
                self.notify(
                    "Queued — will run after the current turn.",
                    severity="information",
                    timeout=2,
                )
            except Exception:
                # No queue available → fall back to the previous "tell
                # the user we're busy" behaviour so the prompt isn't
                # silently dropped.
                self.query_one(ChatPane).add_system_message(
                    "Agent is busy — wait for the current turn to finish."
                )
            return
        self._dispatch_user_turn(text)

    def _drain_prompt_queue(self) -> None:
        """Pop the next queued prompt and dispatch it as a user turn.

        Called from ``_run_turn`` finally. Drains exactly one prompt
        per turn — if more are pending, they cascade through the same
        finally block as each completes. This preserves the visible
        ordering and lets the user cancel mid-cascade.
        """
        try:
            queue = self.query_one(PromptQueueWidget)
        except Exception:
            return
        next_text = queue.pop_next()
        if not next_text:
            return
        # We're inside _run_turn's finally — _busy was just reset to
        # False, so dispatching is safe and lands in the foreground
        # turn slot.
        self._dispatch_user_turn(next_text)

    def on_prompt_area_history_prev(
        self, event: "PromptArea.HistoryPrev"
    ) -> None:
        """Up at the first row → recall older prompt."""
        self.action_history_prev()

    def on_prompt_area_history_next(
        self, event: "PromptArea.HistoryNext"
    ) -> None:
        """Down at the last row → recall newer prompt (or empty)."""
        self.action_history_next()

    # ``_stash_paste`` is gone — multi-line paste lands directly in the
    # PromptArea where the user can edit it before submitting. The toast
    # cue moved into ``PasteAwarePromptArea._on_paste``.

    def _maybe_run_local_slash(self, text: str) -> bool:
        """Handle slash commands we can execute without the agent.

        Resolution order:

        1. ``_LOCAL_SLASH`` (hard-coded, TUI-specific): ``/clear``,
           ``/quit``, ``/exit``, ``/help``, ``/plan``.
        2. ``slash_bridge`` — reuse REPL's ``handle_*`` functions (e.g.
           ``/memory``, ``/worktree``, ``/subagents``, ``/plugin``,
           ``/debug``) via a captured console and mirror the output
           into the ChatPane.

        Returns True when handled (caller does NOT dispatch to agent),
        False for unknown commands — fall through to agent as text.
        """
        body_full = text[1:].strip()
        if not body_full:
            return False
        parts = body_full.split(None, 1)
        name = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        # (1) Hard-coded local handlers.
        if name in self._LOCAL_SLASH:
            if name == "clear":
                self.action_clear_chat()
            elif name in ("quit", "exit"):
                self.action_quit_app()
            elif name == "help":
                self._render_help()
                # Also append the bridged ``_show_help`` output so users
                # see the full REPL command reference.
                self._run_bridged_slash("help", "")
            elif name == "plan":
                self.action_toggle_plan()
            elif name == "cost":
                self._slash_cost()
            elif name == "compact":
                self._slash_compact()
            elif name == "sessions":
                self._slash_sessions()
            elif name == "model":
                self._slash_model(rest)
            elif name == "undo":
                self._slash_undo()
            elif name == "skills":
                self._slash_list_skills()
            elif name == "agents":
                self._slash_list_agents()
            elif name == "commands":
                self._slash_list_commands()
            elif name == "mcp":
                self._slash_list_mcp()
            elif name == "yolo":
                self._slash_yolo()
            elif name == "theme":
                self._slash_theme(rest)
            return True

        # (2) Bridged REPL handlers.
        from aru.tui.slash_bridge import BRIDGED_COMMANDS
        if name in BRIDGED_COMMANDS:
            self._run_bridged_slash(name, rest)
            return True

        return False

    def _run_bridged_slash(self, name: str, body: str) -> None:
        """Execute a bridged REPL handler and show its output in ChatPane."""
        from aru.tui.slash_bridge import run_bridged

        handled, text = run_bridged(name, body, self)
        if not handled:
            return
        chat = self.query_one(ChatPane)
        # Prefix with the command so the user has context.
        header = f"/{name}" + (f" {body}" if body else "")
        chat.add_system_message(f"$ {header}\n{text}" if text else f"$ {header}")

    # ── Inline slash handlers (local to TUI) ─────────────────────────

    def _push_chat(self, message: str, title: str | None = None) -> None:
        chat = self.query_one(ChatPane)
        header = f"$ /{title}" if title else ""
        if header:
            chat.add_system_message(f"{header}\n{message}")
        else:
            chat.add_system_message(message)

    def _slash_cost(self) -> None:
        session = self.session
        if session is None:
            self._push_chat("No session.", "cost")
            return
        try:
            summary = getattr(session, "cost_summary", None)
            text = summary if isinstance(summary, str) else str(summary)
        except Exception as exc:
            text = f"cost failed: {exc}"
        self._push_chat(text, "cost")

    def _slash_compact(self) -> None:
        session = self.session
        if session is None:
            self._push_chat("No session.", "compact")
            return
        self.run_worker(self._run_compact(), name="compact", group="maint")

    async def _run_compact(self) -> None:
        session = self.session
        try:
            from aru.context import compact_conversation, prune_history
            if self.ctx is not None:
                from aru.runtime import set_ctx
                set_ctx(self.ctx)
            session.history = prune_history(
                session.history, model_id=session.model_id
            )
            session.history = await compact_conversation(
                session.history,
                session.model_ref,
                getattr(session, "plan_task", None),
                model_id=session.model_id,
                invoked_skills=(
                    session.get_invoked_skills_for_agent(None)
                    if hasattr(session, "get_invoked_skills_for_agent")
                    else None
                ),
            )
            self.call_from_thread(
                self._push_chat, "Context compacted.", "compact"
            )
        except Exception as exc:
            self.call_from_thread(
                self._push_chat, f"compact failed: {exc}", "compact"
            )

    def _slash_sessions(self) -> None:
        # Slash equivalent of Ctrl+S — open the picker.
        self.action_show_sessions()

    def _slash_model(self, body: str) -> None:
        session = self.session
        if session is None:
            self._push_chat("No session.", "model")
            return
        from aru.providers import (
            MODEL_ALIASES,
            get_provider,
            list_providers,
            resolve_model_ref,
        )

        config = self.config
        config_aliases = (getattr(config, "model_aliases", None) or {}) if config else {}

        body = body.strip()
        if not body:
            lines = [
                f"Current model: {session.model_display} ({session.model_id})",
                "",
            ]
            if config_aliases:
                lines.append("Model aliases (aru.json):")
                for alias, ref in config_aliases.items():
                    lines.append(f"  {alias} → {ref}")
                lines.append("")
            lines.append("Built-in aliases:")
            for alias, ref in MODEL_ALIASES.items():
                lines.append(f"  {alias} → {ref}")
            lines.append("")
            lines.append("Providers:")
            for pkey, pconfig in list_providers().items():
                dflt = pconfig.default_model or "—"
                lines.append(f"  {pkey} ({pconfig.name}) — default: {dflt}")
            lines.append("")
            lines.append("Usage: /model <provider/name>  (e.g. /model anthropic/claude-sonnet-4-5, /model minimax)")
            self._push_chat("\n".join(lines), "model")
            return

        try:
            arg_lower = body.lower()
            resolved_ref = config_aliases.get(arg_lower, arg_lower) if config_aliases else arg_lower
            provider_key, _ = resolve_model_ref(resolved_ref)
            if get_provider(provider_key) is None:
                available = ", ".join(sorted(list_providers().keys()))
                self._push_chat(
                    f"Unknown provider '{provider_key}'. Available: {available}",
                    "model",
                )
                return
            # Normalize to the fully-qualified ref so model_display + create_model
            # see the right provider/model pair.
            session.model_ref = resolved_ref if "/" in resolved_ref else (
                MODEL_ALIASES.get(resolved_ref, resolved_ref)
            )
            if self.ctx is not None:
                self.ctx.model_id = session.model_id
                small_ref = config_aliases.get("small")
                if not small_ref:
                    sp_key, _ = resolve_model_ref(session.model_ref)
                    _small_defaults = {
                        "anthropic": "anthropic/claude-haiku-4-5",
                        "openai": "openai/gpt-4o-mini",
                        "groq": "groq/llama-3.1-8b-instant",
                        "deepseek": "deepseek/deepseek-chat",
                        "ollama": "ollama/llama3.1",
                    }
                    small_ref = _small_defaults.get(sp_key, session.model_ref)
                self.ctx.small_model_ref = small_ref
            status = self.query_one(StatusPane)
            status._refresh_from_session()
            self._push_chat(
                f"Switched to {session.model_display} ({session.model_id})",
                "model",
            )
        except Exception as exc:
            self._push_chat(f"model switch failed: {exc}", "model")

    def _slash_undo(self) -> None:
        # Full /undo semantics require restoring checkpoints; keep it
        # minimal for now — hint + run the bridged handler if available.
        self._push_chat(
            "Undo from the TUI is not yet implemented — use the REPL for "
            "full /undo support.",
            "undo",
        )

    def _slash_list_skills(self) -> None:
        cfg = self.config
        skills = getattr(cfg, "skills", None) or {}
        if not skills:
            self._push_chat("No skills discovered.", "skills")
            return
        lines = [f"Skills ({len(skills)}):"]
        for name, skill in sorted(skills.items()):
            desc = getattr(skill, "description", "") or ""
            lines.append(f"- {name}  {desc}")
        self._push_chat("\n".join(lines), "skills")

    def _slash_list_agents(self) -> None:
        cfg = self.config
        agents = getattr(cfg, "custom_agents", None) or {}
        if not agents:
            self._push_chat("No custom agents.", "agents")
            return
        lines = [f"Custom agents ({len(agents)}):"]
        for name, agent in sorted(agents.items()):
            mode = getattr(agent, "mode", "?")
            desc = getattr(agent, "description", "") or ""
            lines.append(f"- {name}  [{mode}]  {desc}")
        self._push_chat("\n".join(lines), "agents")

    def _slash_list_commands(self) -> None:
        cfg = self.config
        commands = getattr(cfg, "commands", None) or {}
        lines = ["Built-in:"]
        for name in sorted(self._LOCAL_SLASH):
            lines.append(f"  /{name}")
        from aru.tui.slash_bridge import BRIDGED_COMMANDS
        lines.append("")
        lines.append("Bridged from REPL:")
        for name in sorted(BRIDGED_COMMANDS.keys()):
            lines.append(f"  /{name}")
        if commands:
            lines.append("")
            lines.append(f"Custom (from .agents/commands/):")
            for name in sorted(commands.keys()):
                lines.append(f"  /{name}")
        self._push_chat("\n".join(lines), "commands")

    def _slash_list_mcp(self) -> None:
        ctx = self.ctx
        text = getattr(ctx, "mcp_catalog_text", None) if ctx else None
        if not text:
            text = "No MCP tools loaded."
        self._push_chat(text, "mcp")

    def _slash_theme(self, body: str) -> None:
        """List or switch the active TUI theme.

        ``/theme`` with no argument lists curated presets and marks the
        active one. ``/theme <name>`` swaps to the named preset live
        (no restart). Persist by setting ``"theme": "<name>"`` in
        ``aru.json``.
        """
        from aru.tui.themes import THEME_NAMES, apply_theme, resolve_theme

        # Active theme is whatever Textual is currently rendering with —
        # ``self.theme`` (the reactive attribute) is authoritative. Map
        # back to the Aru alias when possible so the marker matches the
        # name the user typed / sees in the list.
        active_canonical = getattr(self, "theme", "") or ""
        body = (body or "").strip()
        if not body:
            lines = ["Available themes:"]
            for tn in THEME_NAMES:
                canon = resolve_theme(tn)
                marker = "•" if canon == active_canonical else " "
                lines.append(f"  {marker} {tn}")
            lines.append("")
            lines.append(f"Active: {active_canonical}")
            lines.append("Usage: /theme <name>")
            lines.append("Persist via aru.json: \"theme\": \"<name>\"")
            self._push_chat("\n".join(lines), "theme")
            return
        canonical = resolve_theme(body)
        available = getattr(self, "available_themes", {}) or {}
        if not canonical or canonical not in available:
            shown = ", ".join(THEME_NAMES)
            self._push_chat(
                f"Unknown theme '{body}'. Available: {shown}",
                "theme",
            )
            return
        ok = apply_theme(self, body)
        if not ok:
            self._push_chat(
                f"Theme '{body}' could not be applied.",
                "theme",
            )
            return
        # Mirror into config so subsequent /theme listings show the
        # right marker; on-disk persistence is still the user's call.
        try:
            if self.config is not None:
                self.config.theme = body
        except Exception:
            pass
        self._push_chat(
            f"Theme switched to '{canonical}'. Add \"theme\": \"{body}\" to "
            "aru.json to persist.",
            "theme",
        )

    def _slash_yolo(self) -> None:
        try:
            if self.ctx is not None:
                from aru.runtime import set_ctx
                set_ctx(self.ctx)
            from aru.permissions import (
                get_permission_mode,
                set_permission_mode,
            )
            current = get_permission_mode()
            new = "default" if current == "yolo" else "yolo"
            set_permission_mode(new)
            try:
                status = self.query_one(StatusPane)
                status.mode = new
            except Exception:
                pass
            self._push_chat(f"Permission mode: {new}", "yolo")
        except Exception as exc:
            self._push_chat(f"yolo failed: {exc}", "yolo")

    def _render_help(self) -> None:
        chat = self.query_one(ChatPane)
        lines = [
            "Aru TUI — local commands & shortcuts:",
            "  /help            this message",
            "  /clear           clear chat pane",
            "  /plan            toggle plan mode",
            "  /quit  /exit     save session and exit",
            "  ! <command>      run a shell command (output streams to chat)",
            "",
            "Shortcuts:",
            "  Ctrl+Q           quit",
            "  Ctrl+L           clear chat",
            "  Ctrl+B           toggle sidebar (more chat width)",
            "  Ctrl+A           cycle permission mode",
            "  Ctrl+P           toggle plan mode",
            "  Ctrl+F           search chat",
            "  Click + drag     select text · Ctrl+C copies the selection",
            "  Ctrl+Y           copy last assistant reply (no selection needed)",
            "  Ctrl+Shift+Y     copy full transcript",
            "  Up / Down        cycle prior inputs",
            "  Tab              accept completer suggestion",
            "  Esc              close completer",
            "",
            "Anything else is sent to the agent.",
        ]
        chat.add_system_message("\n".join(lines))

    def _dispatch_user_turn(self, text: str) -> None:
        """Run the agent in a worker and stream into the chat pane."""
        chat = self.query_one(ChatPane)
        chat.add_user_message(text)
        # Persist the raw user message to session.history — parallel to
        # the REPL's ``session.add_message("user", user_input)`` call in
        # ``cli.py``. Without this, TUI sessions reload with an empty
        # user side (``session.history`` contains only assistant + tool
        # turns) and follow-up turns like "continue" see no user context
        # from prior turns, so the agent replies with text and halts.
        # We save the *raw* text, before ``@file`` expansion, so the
        # rehydrated history matches what the user actually typed.
        if self.session is not None and text:
            try:
                self.session.add_message("user", text)
            except Exception:
                pass
        # Reflect the new prompt in the terminal tab so users with many
        # tabs open can tell which one is working on what.
        if not self.is_headless:
            _set_terminal_title(_compose_terminal_title(self.session, pending=text))
        # Expand ``@file`` mentions in the user message so the agent sees
        # actual file contents as a prefixed block. Mirrors the REPL's
        # ``_resolve_mentions`` behaviour.
        expanded = self._expand_mentions(text)
        self._busy = True
        # Flip the ThinkingIndicator on so the user sees the rotating
        # phrase while the agent reasons / streams its first tokens.
        try:
            self.query_one(ThinkingIndicator).busy = True
        except Exception:
            pass
        self.run_worker(
            self._run_turn(expanded),
            name="agent-turn",
            exclusive=True,
            group="agent",
        )

    def _expand_mentions(self, text: str) -> str:
        """Inline ``@path`` mentions by prepending file contents.

        Best-effort: tokens that look like ``@path`` where the path
        exists on disk get their contents prepended as a markdown block
        and the mention stays in the message (so the agent knows what
        was referenced). Unknown mentions are left untouched.
        """
        import re, os
        mention_re = re.compile(r"(?:^|\s)(@([^\s]+))")
        matches = list(mention_re.finditer(text))
        if not matches:
            return text
        attachments: list[str] = []
        seen: set[str] = set()
        cwd = os.getcwd()
        for _full, _mention, path in [(m.group(0), m.group(1), m.group(2))
                                       for m in matches]:
            if path in seen:
                continue
            seen.add(path)
            abs_path = path if os.path.isabs(path) else os.path.join(cwd, path)
            if not os.path.isfile(abs_path):
                continue
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    body = f.read()
            except Exception:
                continue
            if len(body) > 30_000:
                body = body[:30_000] + "\n... (truncated)"
            attachments.append(f"--- @{path} ---\n{body}")
        if not attachments:
            return text
        return "\n".join(attachments) + "\n\n" + text

    async def _run_turn(self, text: str) -> None:
        chat = self.query_one(ChatPane)
        try:
            from aru.agent_factory import create_agent_from_spec
            from aru.agents.catalog import AGENTS
            from aru.runner import run_agent_capture_tui

            if self.ctx is not None:
                from aru.runtime import set_ctx
                set_ctx(self.ctx)

            # Clear any abort flag left over from a prior Ctrl+C so the
            # new turn isn't short-circuited before it even starts.
            try:
                from aru.runtime import reset_abort
                reset_abort()
            except Exception:
                pass

            agent = await create_agent_from_spec(
                AGENTS["build"],
                session=self.session,
                model_ref=self.session.model_ref if self.session else None,
                extra_instructions=(
                    self.config.get_extra_instructions() if self.config else ""
                ),
            )
            await run_agent_capture_tui(
                agent,
                text,
                session=self.session,
                app=self,
                chat_pane=chat,
            )
            try:
                if self.session_store and self.session:
                    self.session_store.save(self.session)
            except Exception:
                pass
        except Exception as exc:  # pragma: no cover — surfaced via chat
            try:
                chat.add_system_message(
                    f"Turn failed: {type(exc).__name__}: {exc}"
                )
            except Exception:
                pass
        finally:
            self._busy = False
            # Turn off the ThinkingIndicator regardless of success / failure.
            try:
                self.query_one(ThinkingIndicator).busy = False
            except Exception:
                pass
            # Belt-and-suspenders: even if the turn.end event didn't reach
            # a subscriber for some reason, refresh directly from the
            # session state now that track_tokens has landed.
            try:
                self.query_one(StatusPane)._refresh_from_session()
            except Exception:
                pass
            try:
                self.query_one(ContextPane).refresh_from_session()
            except Exception:
                pass
            # Layer 9 self-heal — re-assert Textual's mouse-tracking
            # sequences at the turn boundary. See ``_reenable_mouse_tracking``
            # for the rationale; here we eagerly recover the moment the
            # turn ends so the user's first post-turn scroll always works,
            # without waiting for the periodic Layer 10 tick.
            self._reenable_mouse_tracking()
            # Drain the next prompt off the visible queue (if any). Done
            # here so the queue follows directly from agent idleness —
            # the user-side semantic is "queued prompts run as soon as
            # the previous one finishes". Failures during drain are
            # absorbed so a single bad prompt can't break the loop.
            try:
                self._drain_prompt_queue()
            except Exception:
                pass

    # ── Shell escape (``! <command>``) ───────────────────────────────

    def _dispatch_shell_command(self, command: str) -> None:
        """Run ``command`` in the session cwd and stream output to chat.

        Parity with the REPL's ``! <cmd>`` path in ``cli.py``: we render
        a syntax-highlighted header, run the command via the system
        shell, then push stdout/stderr (interleaved) into a single
        system message that grows as lines arrive. The exit code is
        appended on completion so the user can tell success from
        failure.

        Output is NOT persisted to ``session.history`` — the agent never
        sees ``!`` shell runs (it has its own ``bash`` tool). This is a
        user convenience, not part of the conversation.
        """
        chat = self.query_one(ChatPane)
        try:
            from rich.panel import Panel
            from rich.syntax import Syntax
            chat.add_renderable(Panel(
                Syntax(command, "bash", theme="monokai"),
                title="[bold]Shell[/bold]",
                border_style="dim",
                expand=False,
            ))
        except Exception:
            chat.add_system_message(f"$ {command}")

        from aru.tui.widgets.chat import ChatMessageWidget
        live = ChatMessageWidget(role="system", initial="")
        chat.mount(live)
        self._busy = True
        try:
            self.query_one(ThinkingIndicator).busy = True
        except Exception:
            pass
        self.run_worker(
            self._run_shell_command(command, live),
            name="shell-cmd",
            exclusive=False,
            group="shell",
        )

    async def _run_shell_command(
        self, command: str, live: "ChatMessageWidget"
    ) -> None:
        """Spawn ``command`` and stream output into ``live`` line by line."""
        import asyncio

        try:
            from aru.runtime import get_cwd
            cwd = get_cwd()
        except Exception:
            import os
            cwd = os.getcwd()

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
        except Exception as exc:
            live.buffer = f"[shell error] {type(exc).__name__}: {exc}"
            self._busy = False
            try:
                self.query_one(ThinkingIndicator).busy = False
            except Exception:
                pass
            return

        assert proc.stdout is not None
        buffer_lines: list[str] = []
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                buffer_lines.append(line)
                # Cap displayed buffer so a runaway command doesn't grow
                # the widget until the chat pane stalls. Mirrors the
                # ``bash`` tool's 10K-char output truncation.
                joined = "\n".join(buffer_lines)
                if len(joined) > 10_000:
                    head = joined[:10_000]
                    live.buffer = head + "\n... (truncated, still running)"
                else:
                    live.buffer = joined
            await proc.wait()
        except asyncio.CancelledError:
            try:
                proc.kill()
            except Exception:
                pass
            live.buffer = (live.buffer or "") + "\n[interrupted]"
            raise
        except Exception as exc:
            live.buffer = (live.buffer or "") + (
                f"\n[shell error] {type(exc).__name__}: {exc}"
            )
        finally:
            rc = proc.returncode if proc.returncode is not None else "?"
            tail = f"\n[exit {rc}]"
            current = live.buffer or ""
            if not current.endswith(tail):
                live.buffer = current + tail
            self._busy = False
            try:
                self.query_one(ThinkingIndicator).busy = False
            except Exception:
                pass

    # Layer 14 — full set of DEC private modes that ``WindowsDriver
    # .start_application_mode`` enables at boot, minus alt-screen
    # (``?1049``, not idempotent — would save/restore the display
    # buffer) and kitty-keyboard (``>1u``, terminal-specific, doesn't
    # affect wheel). Layer 13 introduced this set as a Ctrl+R-only
    # heavy shake; user confirmation that Ctrl+R actually recovered
    # the wheel after Windows display sleep/wake (2026-04-25) is the
    # signal that the broader set is what works in practice — the
    # mouse-only shake from Layer 12 was insufficient. Layer 14 promotes
    # the full set into ``_reenable_mouse_tracking`` so every existing
    # caller (Layer 9 turn boundary, Layer 10 periodic tick, Layer 12
    # broken keypress) gets the proven recovery automatically.
    _FULL_MODE_DISABLE_SEQS: tuple[str, ...] = (
        "\x1b[?1000l",  # mouse VT200
        "\x1b[?1003l",  # any-event mouse
        "\x1b[?1015l",  # VT200 highlight mouse
        "\x1b[?1006l",  # SGR ext mode mouse
        "\x1b[?1004l",  # focus events
        "\x1b[?2004l",  # bracketed paste
    )
    _FULL_MODE_ENABLE_SEQS: tuple[str, ...] = (
        "\x1b[?1000h",
        "\x1b[?1003h",
        "\x1b[?1015h",
        "\x1b[?1006h",
        "\x1b[?1004h",
        "\x1b[?2004h",
    )

    def _reenable_mouse_tracking(self) -> None:
        """Re-arm terminal modes via console-mode re-assert + full-mode shake.

        Single recovery primitive used by every layer: turn boundary
        (Layer 9), periodic tick (Layer 10), keypress trigger (Layer 12,
        broken — see chat.py post-mortem), and ``Ctrl+R`` action (Layer
        13, which adds a refresh + chat message on top). The method
        keeps its name (``_reenable_mouse_tracking``) for git-blame
        continuity even though it now re-arms more than just mouse —
        what it does is documented here, and the post-mortem in
        chat.py traces the evolution from Layer 12 through Layer 14.

        Two failure modes the recovery handles:

        1. **``ENABLE_VIRTUAL_TERMINAL_INPUT`` cleared on stdin (Windows).**
           ``enable_application_mode`` (textual win32.py:179) sets this
           flag at startup, but a display sleep / wake or other Windows
           console state transition can clear it. While cleared,
           ConPTY stops translating mouse / focus events into VT
           sequences and *no* stdout escape we write can recover wheel
           input. Re-asserting the flag additively (``current | flag``)
           preserves any other input flags while ensuring VT input
           translation is back on.

        2. **DEC private-mode state lost on the terminal side.** Layer
           12 originally addressed this for mouse-only via an off-then-on
           shake (``?1000l → ?1000h``) to defeat ConPTY's enable-cache.
           Layer 14 widens the shake to the full set ``WindowsDriver
           .start_application_mode`` enables: mouse (4 modes) + focus
           events (``?1004``) + bracketed paste (``?2004``). 12 escapes
           total off-then-on, ~108 bytes, one flush. Excluded:
           alt-screen (not idempotent) and kitty-keyboard (terminal-
           specific, doesn't affect wheel). The user report on
           2026-04-25 confirmed the mouse-only shake didn't recover
           the wheel after display wake but the full shake (via Ctrl+R)
           did — Layer 14 promotes that proven recovery into the auto
           path.

        Cost per call: ~108 bytes + one ``GetConsoleMode`` +
        ``SetConsoleMode`` syscall pair on Windows. At the 3s tick
        rate that is ~36 B/s plus microseconds — negligible.

        Wrapped in ``try/except`` everywhere because the driver may be
        ``None`` in headless / test mode and the win32 import may fail
        on non-Windows; we'd rather no-op silently than crash.
        """
        if sys.platform == "win32":
            try:
                from textual.drivers.win32 import (
                    ENABLE_VIRTUAL_TERMINAL_INPUT,
                    get_console_mode,
                    set_console_mode,
                )
                current = get_console_mode(sys.__stdin__)
                set_console_mode(
                    sys.__stdin__, current | ENABLE_VIRTUAL_TERMINAL_INPUT
                )
            except Exception:
                pass

        try:
            driver = self._driver
            if driver is None:
                return
            for seq in self._FULL_MODE_DISABLE_SEQS:
                try:
                    driver.write(seq)
                except Exception:
                    pass
            for seq in self._FULL_MODE_ENABLE_SEQS:
                try:
                    driver.write(seq)
                except Exception:
                    pass
            try:
                driver.flush()
            except Exception:
                pass
        except Exception:
            pass

    def _maybe_rearm_mouse_on_keypress(self) -> None:
        """Layer 12 — re-arm mouse tracking on each keystroke (debounced).

        Trigger fires from ``on_key`` so any user keypress is treated as a
        recovery opportunity. A typing user is the strongest signal we
        have that the wheel just stopped working — they reached for the
        keyboard because the mouse stopped responding, or they're about
        to scroll back with PgUp and want it ready. Either way, paying
        ~64 bytes per keypress (capped at 2 Hz by ``_KEYPRESS_REARM_DEBOUNCE``)
        is a trivial cost for sub-second recovery latency.

        The debounce intentionally uses ``time.monotonic`` rather than the
        Textual scheduler so it survives across the async ``on_key``
        boundary without an extra task. ``-inf`` initial value guarantees
        the first keystroke always rearms.
        """
        now = time.monotonic()
        if now - self._last_mouse_reenable_at < self._KEYPRESS_REARM_DEBOUNCE:
            return
        self._last_mouse_reenable_at = now
        self._reenable_mouse_tracking()

    def _self_heal_terminal_state(self) -> None:
        """Periodic recovery of mouse tracking and input focus (Layers 10 + 11).

        Two failure classes that the tick recovers from:

        1. **Terminal mouse-tracking lost.** Layer 9 already re-enables at
           the turn boundary; this catches mid-turn corruption so the
           wheel comes back within ``_MOUSE_REENABLE_INTERVAL`` instead
           of waiting for the agent to finish.
        2. **Input prompt invisible or unfocused** when nothing else
           legitimately owns it. Three concrete scenarios this fixes:
           * an ``InlineChoicePrompt`` callback raised before
             ``on_unmount`` ran, leaving ``#input.-hidden`` stuck;
           * a focusable panel mounted by ``add_renderable`` (pre-Layer-11
             behaviour) grabbed focus and never released it;
           * an exception during ``finalize_assistant_message`` cancelled
             a focus-restore that ``_run_turn`` would normally do.

        We only intervene when **no modal is on top** (modal owns input,
        ``len(self.screen_stack) <= 1``) and **no ``InlineChoicePrompt``
        is currently mounted** (the inline prompt legitimately steals
        focus and hides the input by design — touching it mid-flight
        would steal back from the user). When both conditions hold, we
        treat the input as the canonical focus target.
        """
        # Layer 10 — mouse tracking.
        self._reenable_mouse_tracking()

        # Layer 11 — input watchdog. Skip if a modal is on top: the modal
        # is the legitimate input owner and the underlying ``Input`` is
        # not part of the active focus chain.
        try:
            if len(self.screen_stack) > 1:
                return
        except Exception:
            return

        # Skip if an ``InlineChoicePrompt`` is currently mounted: it has
        # explicitly hidden the input and owns the focus while waiting
        # for the user's choice. ``query`` returns an empty list when the
        # widget tree has no match, so the truth-test is safe.
        try:
            from aru.tui.widgets.inline_choice import InlineChoicePrompt
            if list(self.query(InlineChoicePrompt)):
                return
        except Exception:
            pass

        # Recover ``#input`` if it's stuck hidden (the ``-hidden`` class
        # comes off only inside ``InlineChoicePrompt._toggle_input``; if
        # that didn't run because the callback raised, the user is
        # stranded with no visible prompt). ``remove_class`` on a class
        # that isn't applied is a no-op, so the unconditional call is safe.
        try:
            inp = self.query_one(PromptArea)
        except Exception:
            return
        try:
            if inp.has_class("-hidden"):
                inp.remove_class("-hidden")
        except Exception:
            pass

        # Re-focus only when *nothing* currently has focus. We deliberately
        # do NOT yank focus away from a sidebar / scrollback / search
        # screen the user navigated to themselves — that would fight
        # legitimate keyboard navigation. The ``focused is None`` guard
        # narrows the recovery to the ghost-focus state we actually
        # observed in the bug.
        try:
            if self.screen.focused is None:
                inp.focus()
        except Exception:
            pass

    # ── Bus wiring — ToolsPane + StatusPane subscribe to plugin events ──

    def _install_bus_subscriptions(self) -> None:
        """Register bus callbacks for sidebar panes + StatusPane updates.

        The plugin manager dispatches publish() from within the App's
        own event loop (since we await it from ``run_worker`` which runs
        on the same loop). ``call_from_thread`` assumes the caller is
        off-loop, so we dispatch directly and fall back to
        ``call_from_thread`` only if a direct call fails (e.g. if a
        plugin publishes from a real worker thread).
        """
        mgr = self.plugin_manager or (self.ctx and self.ctx.plugin_manager)
        if mgr is None:
            return
        try:
            ctx_pane = self.query_one(ContextPane)
        except Exception:
            ctx_pane = None
        try:
            status = self.query_one(StatusPane)
        except Exception:
            status = None

        def _dispatch(fn, payload):
            if fn is None:
                return
            try:
                fn(payload)
            except Exception:
                try:
                    self.call_from_thread(fn, payload)
                except Exception:
                    pass

        if status is not None:
            mgr.subscribe(
                "turn.end",
                lambda p: _dispatch(status.update_from_turn, p),
            )
            mgr.subscribe(
                "permission.mode.changed",
                lambda p: _dispatch(status.update_from_mode_change, p),
            )
            mgr.subscribe(
                "cwd.changed",
                lambda p: _dispatch(status.update_from_cwd_change, p),
            )
            # Intra-turn: refresh after every internal LLM call so long
            # implementation phases show a live cost/token climb.
            mgr.subscribe(
                "metrics.updated",
                lambda p: _dispatch(status.update_from_metrics, p),
            )
        if ctx_pane is not None:
            mgr.subscribe(
                "turn.end",
                lambda p: _dispatch(ctx_pane.update_from_turn, p),
            )
            mgr.subscribe(
                "metrics.updated",
                lambda p: _dispatch(ctx_pane.update_from_metrics, p),
            )

        # TasklistPanel — sidebar with macro plan + executor subtasks.
        # Subscribes to two events emitted from ``aru.tools.tasklist``.
        try:
            tasklist_panel = self.query_one(TasklistPanel)
        except Exception:
            tasklist_panel = None
        if tasklist_panel is not None:
            mgr.subscribe(
                "tasklist.updated",
                lambda p: _dispatch(tasklist_panel.on_tasklist_updated, p),
            )
            mgr.subscribe(
                "plan.updated",
                lambda p: _dispatch(tasklist_panel.on_plan_updated, p),
            )

        # OS notification dispatcher — fires terminal bell + OSC 9 + OS
        # toast on subagent.complete / turn.end per the configured policy.
        # Lazy-instantiated here so we share the same plugin manager as
        # the rest of the bus subscribers; cheap to construct.
        try:
            policy = (
                getattr(self.config, "notify", "background")
                if self.config is not None
                else "background"
            )
            threshold = (
                float(getattr(self.config, "notify_threshold_sec", 30.0))
                if self.config is not None
                else 30.0
            )
            from aru.tui.notifications import NotificationDispatcher
            self._notify_dispatcher = NotificationDispatcher(
                self, policy=policy, threshold_sec=threshold
            )
            self._notify_dispatcher.install(mgr)
        except Exception:
            pass

        # SubagentPanel — live view of fan-out workers + their current
        # tool. Hidden when no sub-agent is running. Subscribes to four
        # events emitted by ``aru.tools.delegate``.
        try:
            sub_panel = self.query_one(SubagentPanel)
        except Exception:
            sub_panel = None
        if sub_panel is not None:
            mgr.subscribe(
                "subagent.start",
                lambda p: _dispatch(sub_panel.on_subagent_start, p),
            )
            mgr.subscribe(
                "subagent.complete",
                lambda p: _dispatch(sub_panel.on_subagent_complete, p),
            )
            mgr.subscribe(
                "subagent.tool.started",
                lambda p: _dispatch(sub_panel.on_subagent_tool_started, p),
            )
            mgr.subscribe(
                "subagent.tool.completed",
                lambda p: _dispatch(sub_panel.on_subagent_tool_completed, p),
            )

    # ── Actions ──────────────────────────────────────────────────────

    def action_clear_chat(self) -> None:
        chat = self.query_one(ChatPane)
        for child in list(chat.children):
            try:
                child.remove()
            except Exception:
                pass
        chat.add_system_message("Chat cleared.")

    def action_cycle_mode(self) -> None:
        """Cycle permission mode (default → acceptEdits → yolo → default).

        Updates StatusPane directly — the bus publish is best-effort
        (requires ctx installed in the current task) but we don't want
        the UI to silently stay out of sync if it fails.
        """
        try:
            if self.ctx is not None:
                from aru.runtime import set_ctx
                set_ctx(self.ctx)
            from aru.permissions import cycle_permission_mode
            new_mode = cycle_permission_mode()
            # Push the mode directly into the StatusPane regardless of
            # whether the bus subscriber fired.
            try:
                self.query_one(StatusPane).mode = new_mode
            except Exception:
                pass
            self.notify(f"Permission mode: {new_mode}", severity="info")
        except Exception as exc:  # pragma: no cover
            self.notify(f"Mode cycle failed: {exc}", severity="error")

    def action_toggle_plan(self) -> None:
        """Toggle session.plan_mode flag — agent gets plan reminder next turn."""
        if self.session is None:
            return
        new_state = not bool(getattr(self.session, "plan_mode", False))
        self.session.plan_mode = new_state
        label = "ON" if new_state else "OFF"
        self.notify(f"Plan mode: {label}", severity="info")
        chat = self.query_one(ChatPane)
        chat.add_system_message(f"Plan mode {label}.")

    def action_search_chat(self) -> None:
        """Open SearchScreen; jump to the chosen message on select."""
        from aru.tui.screens import SearchScreen
        from aru.tui.widgets.chat import ChatMessageWidget

        chat = self.query_one(ChatPane)
        items: list[tuple[int, str]] = []
        for i, msg in enumerate(chat.query(ChatMessageWidget)):
            text = (msg.buffer or "").strip()
            if text:
                items.append((i, text))

        def _on_picked(idx: int | None) -> None:
            if idx is None:
                return
            msgs = list(chat.query(ChatMessageWidget))
            if 0 <= idx < len(msgs):
                try:
                    msgs[idx].scroll_visible(animate=False)
                except Exception:
                    pass

        self.push_screen(SearchScreen(items), _on_picked)

    def action_focus_tools(self) -> None:
        try:
            self.query_one(ToolsPane).focus()
        except Exception:
            pass

    def action_toggle_tasklist(self) -> None:
        """Show/hide the right-sidebar tasklist panel (Ctrl+T)."""
        try:
            panel = self.query_one(TasklistPanel)
        except Exception:
            return
        hidden = panel.toggle_visibility()
        self.notify(
            "Tasklist sidebar hidden." if hidden else "Tasklist sidebar shown.",
            severity="information",
            timeout=2,
        )

    def action_focus_input(self) -> None:
        try:
            self.query_one(PromptArea).focus()
        except Exception:
            pass

    def action_recover_terminal(self) -> None:
        """Layer 13 — user-invoked terminal-state recovery (Ctrl+R).

        Delegates the recovery sequence (Windows console-mode re-assert
        + full DEC private-mode shake + flush) to
        ``_reenable_mouse_tracking`` — that method now does the strong
        shake for every layer (Layer 14 promotion), so Ctrl+R, the 3s
        tick, and the turn-boundary call all run identical recovery
        bytes. This action adds two extras unique to the manual path:

        * ``self.refresh()`` to force a compositor redraw — the
          autonomous paths don't need this because the next paint
          cycle handles it; Ctrl+R is interactive and the user wants
          immediate visible confirmation.
        * **Visible chat message** so the user sees the recovery did
          execute. The user explicitly noted that silent recovery is
          indistinguishable from no recovery, so we surface it on the
          manual path. Periodic / turn-boundary callers stay silent
          to avoid spamming the chat.

        Bound to ``Ctrl+R`` with ``priority=True`` so the binding fires
        regardless of focused widget. Bindings dispatch via Textual's
        binding system, not through ``_on_key``, so this path is immune
        to the ``Input._on_key → event.stop()`` problem that breaks
        Layer 12's keypress trigger.
        """
        self._reenable_mouse_tracking()

        try:
            self.refresh()
        except Exception:
            pass

        try:
            self.query_one(ChatPane).add_system_message(
                "[Ctrl+R] Terminal modes re-armed (mouse / focus / paste)"
            )
        except Exception:
            pass

    def action_toggle_sidebar(self) -> None:
        """Hide / show the right sidebar to give the chat full width."""
        try:
            sidebar = self.query_one("#sidebar")
            chat = self.query_one(ChatPane)
        except Exception:
            return
        if sidebar.has_class("-hidden"):
            sidebar.remove_class("-hidden")
            chat.remove_class("-hide-sidebar")
        else:
            sidebar.add_class("-hidden")
            chat.add_class("-hide-sidebar")

    def copy_to_clipboard(self, text: str) -> None:
        """Native-OS clipboard write that bypasses the Textual writer queue.

        Textual's default ``copy_to_clipboard`` writes an OSC 52 escape
        through ``self._driver.write`` (textual/app.py:1755). On Windows
        the driver hands the bytes to a ``WriterThread`` whose internal
        ``Queue`` has ``MAX_QUEUED_WRITES = 30``
        (textual/drivers/_writer_thread.py:9). During a streaming agent
        turn the compositor enqueues many paint sequences per second; if
        ConPTY drains slower than producers fill the queue, ``Queue.put``
        blocks the *main thread* — which means the asyncio loop freezes,
        Ctrl+C key events sit unprocessed, and the user perceives "the
        copy didn't happen, the thread is doing something else." Even
        when the queue isn't full, the OSC 52 sequence is serialised
        behind every queued paint, so the actual clipboard update can
        arrive seconds after the keypress (and may be dropped by the
        terminal's OSC parser when interleaved with other escapes).

        On Windows we sidestep the queue entirely by writing through the
        Win32 clipboard API. ``win32clipboard.SetClipboardText`` runs
        synchronously and is normally microseconds — even when another
        app holds the clipboard briefly, ``OpenClipboard`` returns fast.
        We still update ``self._clipboard`` so Textual's
        ``app.clipboard`` property keeps mirroring the last copied text,
        and we fall back to the OSC 52 path if the Win32 call raises.

        On non-Windows platforms we keep the OSC 52 path — the Linux
        driver writes synchronously to stdout without an intermediate
        bounded queue, so the saturation issue does not apply.
        """
        self._clipboard = text
        if sys.platform == "win32":
            try:
                self._win32_set_clipboard(text)
                return
            except Exception:
                # Fall through to OSC 52 — better than silently dropping.
                pass
        super().copy_to_clipboard(text)

    @staticmethod
    def _win32_set_clipboard(text: str) -> None:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()

    def _gather_selected_text(self) -> str:
        """Collect any active text selection across the App.

        Two distinct selection models can produce a "selected" string:

        1. **Screen-level mouse selections**: populated by Textual when
           the user click-drags inside any widget that opts in via
           ``ALLOW_SELECT``. Lives in ``Screen.selections``. Read via
           ``Screen.get_selected_text``.
        2. **Widget-internal selections**: ``Input`` and ``TextArea``
           track their own text selection (shift+arrow, double-click).
           These are NOT visible to the screen-level API. We have to
           ask the focused widget directly.

        Returning the first non-empty hit means a Ctrl+C with selection
        in *either* model copies the right text instead of arming the
        exit confirmation.
        """
        try:
            screen_sel = self.screen.get_selected_text() or ""
        except Exception:
            screen_sel = ""
        if screen_sel.strip():
            return screen_sel
        try:
            focused = self.focused
        except Exception:
            focused = None
        if focused is not None:
            # ``TextArea.selected_text`` and ``Input.selected_text`` are
            # the public attribute names; both return ``""`` when no
            # range is selected.
            for attr in ("selected_text", "_selected_text"):
                try:
                    val = getattr(focused, attr, None)
                except Exception:
                    val = None
                if isinstance(val, str) and val:
                    return val
        return ""

    def action_ctrl_c(self) -> None:
        """Context-sensitive Ctrl+C — matches REPL semantics, minus quit.

        1. If there's any text selection (screen mouse selection OR
           focused-widget internal selection) → copy it.
        2. If an agent turn is running → abort the turn, keep the app
           alive, and hand the prompt back to the user. This mirrors
           the REPL where SIGINT during an agent run raises
           ``KeyboardInterrupt`` inside ``run_agent_capture`` and drops
           back to the readline prompt without exiting.
        3. When the prompt is idle → display a hint and stay alive.

        Ctrl+C **does not exit** Aru. The earlier "two Ctrl+C exits"
        pattern was a footgun: when the screen-level selection wasn't
        detected (focus shift, intermittent terminal mouse state), the
        first press armed silently and the user's retry-to-copy became
        a quit. Users who really want to leave have ``Ctrl+Q`` (visible
        in the footer) or ``/quit``.
        """
        _loop_tracer.trace("action_ctrl_c", f"busy={self._busy}")
        selected = self._gather_selected_text()
        if selected.strip():
            try:
                self.copy_to_clipboard(selected)
                self.notify(
                    f"Copied {len(selected)} chars to clipboard.",
                    severity="information",
                )
            except Exception as exc:
                self.notify(f"Copy failed: {exc}", severity="error")
            # Clear the selection afterwards — mirrors the shell flow
            # where the selection vanishes once copied.
            try:
                self.screen.clear_selection()
            except Exception:
                pass
            # Reset the exit-arm — a successful copy is a clear
            # "user did something useful" signal, no need to keep the
            # quit window open.
            self._last_ctrl_c_t = 0.0
            return
        if self._busy:
            # Mid-turn: interrupt the agent and return control to the
            # user. The _run_turn finally clause resets ``_busy`` and
            # the ThinkingIndicator; we just need to push a visible
            # marker into the chat and refocus the input so the user
            # can immediately type the follow-up.
            self._abort_running_turn()
            try:
                from aru.tui.widgets.chat import ChatPane
                self.query_one(ChatPane).add_system_message("Interrupted.")
            except Exception:
                pass
            try:
                self.query_one(PromptArea).focus()
            except Exception:
                pass
            self._last_ctrl_c_t = 0.0
            return
        # Idle prompt: never exit. Hint at the alternatives so users who
        # *did* want to quit aren't stranded. Throttle the toast so a
        # frantic Ctrl+C, Ctrl+C, Ctrl+C burst doesn't spawn three of
        # them stacked on top of each other.
        import time
        now = time.monotonic()
        if now - self._last_ctrl_c_t > 1.5:
            self._last_ctrl_c_t = now
            try:
                self.notify(
                    "Nothing selected. Use Ctrl+Q or /quit to exit.",
                    severity="information",
                    timeout=2,
                )
            except Exception:
                pass

    def _on_sigint_from_handler(self) -> None:
        """Loop-thread entry point for the SIGINT handler.

        Runs after ``call_soon_threadsafe`` from ``_sigint_handler``.
        Forwards to ``action_ctrl_c`` so SIGINT-delivered Ctrl+C
        (the only path that exists on Windows — see
        ``on_mount`` for why) follows the same selection / abort /
        idle hint flow as a keystroke would have. Wrapped in
        ``try/except`` because action handlers may touch widgets
        and a failure must not kill the app.
        """
        _loop_tracer.trace("sigint_dispatched_to_loop", "")
        try:
            self.action_ctrl_c()
        except Exception:
            pass

    def _abort_running_turn(self) -> None:
        """Signal any in-flight agent worker to cancel."""
        try:
            from aru.runtime import abort_current
            abort_current()
        except Exception:
            pass
        try:
            self.workers.cancel_all()
        except Exception:
            pass

    def action_copy_last(self) -> None:
        """Copy the last assistant reply into the system clipboard.

        The TUI owns the terminal's mouse events while running so the
        native "select + Ctrl+C" flow doesn't work. Ctrl+Y mirrors the
        vim-style yank and uses Textual's ``copy_to_clipboard`` under
        the hood (works through OSC 52 or the native backend).
        """
        from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
        try:
            chat = self.query_one(ChatPane)
        except Exception:
            return
        assistants = [
            m for m in chat.query(ChatMessageWidget) if m.role == "assistant"
        ]
        if not assistants:
            self.notify("No assistant message to copy yet.", severity="warning")
            return
        text = assistants[-1].buffer or ""
        if not text.strip():
            self.notify("Last assistant message is empty.", severity="warning")
            return
        try:
            self.copy_to_clipboard(text)
            self.notify(
                f"Copied last reply ({len(text)} chars) to clipboard.",
                severity="information",
            )
        except Exception as exc:
            self.notify(f"Copy failed: {exc}", severity="error")

    def action_copy_code(self) -> None:
        """Copy the last fenced code block from any assistant reply (Ctrl+K).

        Walks assistant bubbles in reverse and lifts the most recent
        ``​```lang\\n...\\n```​`` block. Useful when the model emits
        prose plus a snippet and the user wants only the snippet — Ctrl+Y
        copies the whole reply, Ctrl+K narrows to the code.
        """
        import re
        from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
        try:
            chat = self.query_one(ChatPane)
        except Exception:
            return
        assistants = [
            m for m in chat.query(ChatMessageWidget) if m.role == "assistant"
        ]
        fence_re = re.compile(r"```[^\n]*\n(.*?)\n```", re.DOTALL)
        for msg in reversed(assistants):
            text = msg.buffer or ""
            blocks = fence_re.findall(text)
            if blocks:
                code = blocks[-1]
                try:
                    self.copy_to_clipboard(code)
                    self.notify(
                        f"Copied last code block ({len(code)} chars).",
                        severity="information",
                    )
                except Exception as exc:
                    self.notify(f"Copy failed: {exc}", severity="error")
                return
        self.notify("No fenced code block found.", severity="warning")

    def action_show_keymap(self) -> None:
        """Open the keymap overlay (F1)."""
        from aru.tui.screens.keymap import KeymapScreen
        self.push_screen(KeymapScreen())

    def action_show_sessions(self) -> None:
        """Open the session picker (Ctrl+S / /sessions).

        On selection, the chosen session replaces the current one in
        place — chat is cleared, history replayed, status panes
        refreshed. The previous session is saved first so the user
        can return to it later.
        """
        if self.session_store is None:
            self._push_chat("No session store.", "sessions")
            return
        from aru.tui.screens.session_picker import SessionPickerScreen

        current_id = (
            getattr(self.session, "session_id", None)
            if self.session is not None
            else None
        )

        def _on_pick(sid: str | None) -> None:
            if not sid:
                return
            if sid == current_id:
                self.notify(
                    "Already on this session.",
                    severity="information",
                    timeout=2,
                )
                return
            self._swap_session(sid)

        self.push_screen(SessionPickerScreen(self.session_store, current_id), _on_pick)

    def _swap_session(self, new_id: str) -> None:
        """Save the current session and resume ``new_id`` in place.

        Surface-level operations (chat clear, history replay, status
        refresh) keep the visible state consistent with the loaded
        session. The agent loop is untouched — the next turn picks
        up the new session naturally because ``ctx.session`` was
        repointed and ``self.session`` is what every dispatch site
        reads.
        """
        # 1. Save the outgoing session so we don't lose its tail edits.
        try:
            if self.session is not None and self.session_store is not None:
                self.session_store.save(self.session)
        except Exception:
            pass

        # 2. Load the new session.
        try:
            new_session = self.session_store.load(new_id)
        except Exception as exc:
            self.notify(f"Load failed: {exc}", severity="error")
            return
        if new_session is None:
            self.notify(f"Session '{new_id}' not found.", severity="error")
            return

        # 3. Repoint app + ctx + propagate to status / context panes.
        self.session = new_session
        try:
            if self.ctx is not None:
                self.ctx.session = new_session
                # Mirror /model logic so the model + small_model_ref
                # come from the resumed session, not the previous one.
                from aru.providers import resolve_model_ref
                self.ctx.model_id = new_session.model_id
                config_aliases = (
                    getattr(self.config, "model_aliases", None) or {}
                ) if self.config else {}
                small_ref = config_aliases.get("small")
                if not small_ref:
                    provider_key, _ = resolve_model_ref(new_session.model_ref)
                    _small_defaults = {
                        "anthropic": "anthropic/claude-haiku-4-5",
                        "openai": "openai/gpt-4o-mini",
                        "groq": "groq/llama-3.1-8b-instant",
                        "deepseek": "deepseek/deepseek-chat",
                        "ollama": "ollama/llama3.1",
                    }
                    small_ref = _small_defaults.get(
                        provider_key, new_session.model_ref
                    )
                self.ctx.small_model_ref = small_ref
        except Exception:
            pass

        # 4. Clear and replay chat.
        try:
            chat = self.query_one(ChatPane)
            for child in list(chat.children):
                try:
                    child.remove()
                except Exception:
                    pass
            self._replay_resumed_history(chat)
            chat.add_system_message(f"Resumed session {new_id[:8]}.")
        except Exception:
            pass

        # 5. Refresh status / context panes.
        try:
            self.query_one(StatusPane)._refresh_from_session()
        except Exception:
            pass
        try:
            self.query_one(ContextPane).refresh_from_session()
        except Exception:
            pass

        # 6. Reset history cursor + clear queue (queued prompts belonged
        # to the old session — running them against the new one would
        # be surprising).
        self._history_cursor = None
        try:
            queue = self.query_one(PromptQueueWidget)
            for qid, _text in list(queue.items()):
                queue.cancel(qid)
        except Exception:
            pass

        self.notify(
            f"Resumed session {new_id[:8]}.",
            severity="information",
            timeout=3,
        )

    def action_copy_all(self) -> None:
        """Copy the entire chat transcript to the clipboard (Ctrl+Shift+Y)."""
        from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
        try:
            chat = self.query_one(ChatPane)
        except Exception:
            return
        messages = list(chat.query(ChatMessageWidget))
        if not messages:
            self.notify("Chat is empty.", severity="warning")
            return
        lines: list[str] = []
        role_prefix = {
            "user": "> user: ",
            "assistant": "assistant: ",
            "system": "[system] ",
            "tool": "  · ",
        }
        for m in messages:
            prefix = role_prefix.get(m.role, "")
            lines.append(f"{prefix}{m.buffer}")
        transcript = "\n\n".join(lines)
        try:
            self.copy_to_clipboard(transcript)
            self.notify(
                f"Copied full chat ({len(messages)} messages).",
                severity="information",
            )
        except Exception as exc:
            self.notify(f"Copy failed: {exc}", severity="error")

    def action_history_prev(self) -> None:
        """Recall the previous user input (one step older)."""
        if not self._history:
            return
        try:
            prompt = self.query_one(PromptArea)
        except Exception:
            return
        if not prompt.has_focus:
            return
        if self._history_cursor is None:
            self._history_cursor = len(self._history) - 1
        elif self._history_cursor > 0:
            self._history_cursor -= 1
        prompt.value = self._history[self._history_cursor]

    def action_history_next(self) -> None:
        """Move forward in history (towards the empty line)."""
        if not self._history:
            return
        try:
            prompt = self.query_one(PromptArea)
        except Exception:
            return
        if not prompt.has_focus:
            return
        if self._history_cursor is None:
            return
        if self._history_cursor < len(self._history) - 1:
            self._history_cursor += 1
            prompt.value = self._history[self._history_cursor]
        else:
            self._history_cursor = None
            prompt.value = ""

    def action_quit_app(self) -> None:
        _loop_tracer.trace("action_quit_app", "begin")
        self._save_session()
        _loop_tracer.trace("action_quit_app", "after_save_session")
        self._restore_sigint_handler()
        _loop_tracer.trace("action_quit_app", "after_restore_sigint")
        self.exit(return_code=0)
        _loop_tracer.trace("action_quit_app", "after_exit_call")

    def _restore_sigint_handler(self) -> None:
        """Restore the SIGINT handler we replaced in ``on_mount``.

        Otherwise the parent shell would inherit our no-op handler and a
        subsequent long-running command would not respond to Ctrl+C.
        """
        prev = getattr(self, "_prev_sigint_handler", None)
        if prev is None:
            return
        try:
            import signal as _signal
            _signal.signal(_signal.SIGINT, prev)
        except (ValueError, OSError, AttributeError):
            pass
        self._prev_sigint_handler = None

    def on_unmount(self) -> None:
        _loop_tracer.trace("on_unmount", "begin")
        self._restore_sigint_handler()
        try:
            _loop_tracer.stop_heartbeat()
        except Exception:
            pass
        _loop_tracer.trace("on_unmount", "end")

    def _save_session(self) -> None:
        try:
            if self.session is not None and self.session_store is not None:
                self.session_store.save(self.session)
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────


async def run_tui(
    skip_permissions: bool = False,
    resume_id: str | None = None,
) -> None:
    """Bootstrap Aru in TUI mode and run the Textual App.

    Mirrors ``cli.run_cli`` bootstrap sequence so the TUI gets the same
    config resolution, custom tools/agents/plugins/MCP loading,
    formatter wiring, LSP, and session/worktree restoration as the REPL.
    """
    import atexit
    import logging as _logging
    import os

    from aru.cache_patch import apply_cache_patch
    from aru.config import load_config
    from aru.permissions import parse_permission_config
    from aru.plugins.manager import PluginManager
    from aru.plugins.hooks import PluginInput
    from aru.runtime import init_ctx
    from aru.session import Session, SessionStore
    from aru.tools.codebase import cleanup_processes
    from aru.tui.ui import TuiUI

    apply_cache_patch()

    ctx = init_ctx(skip_permissions=skip_permissions)

    config = load_config()
    ctx.config = config

    # LSP wiring (Tier 2 #5)
    try:
        from aru.lsp.manager import install_lsp_from_config
        install_lsp_from_config(config.lsp, root=os.getcwd())
    except Exception:
        pass

    # Populate invoke_skill's dynamic docstring with discovered skills.
    try:
        from aru.tools.skill import _update_invoke_skill_docstring
        _update_invoke_skill_docstring(config.skills)
    except Exception:
        pass

    # Register custom agents so /agent routing can resolve them.
    if config.custom_agents:
        try:
            from aru.tools.codebase import set_custom_agents
            set_custom_agents(config.custom_agents)
        except Exception:
            pass

    if config.permissions:
        ctx.perm_config = parse_permission_config(config.permissions)

    # Session resume-or-create + apply default_model from aru.json.
    store = SessionStore()
    if resume_id:
        if resume_id == "last":
            session = store.load_last() or Session()
        else:
            session = store.load(resume_id) or Session()
    else:
        session = Session()
        if config.default_model:
            session.model_ref = config.default_model
    ctx.session = session

    # Mirror _sync_model from run_cli — update RuntimeContext with the
    # session's model and resolve the small-model reference.
    try:
        from aru.providers import resolve_model_ref
        ctx.model_id = session.model_id
        small_ref = (config.model_aliases or {}).get("small") if config else None
        if not small_ref:
            provider_key, _ = resolve_model_ref(session.model_ref)
            _small_defaults = {
                "anthropic": "anthropic/claude-haiku-4-5",
                "openai": "openai/gpt-4o-mini",
                "groq": "groq/llama-3.1-8b-instant",
                "deepseek": "deepseek/deepseek-chat",
                "ollama": "ollama/llama3.1",
            }
            small_ref = _small_defaults.get(provider_key, session.model_ref)
        ctx.small_model_ref = small_ref
    except Exception:
        pass

    # Tree depth override from config.
    try:
        session._tree_max_depth = config.tree_depth
    except Exception:
        pass

    # Worktree state restoration + file-mutation invalidation.
    try:
        from aru.cli import _restore_worktree_from_session
        _restore_worktree_from_session(session)
    except Exception:
        pass
    ctx.on_file_mutation = session.invalidate_context_cache

    def _atexit_with_trace():
        _loop_tracer.trace("atexit", "begin_cleanup_processes")
        try:
            cleanup_processes(ctx.tracked_processes)
        finally:
            _loop_tracer.trace("atexit", "end_cleanup_processes")

    atexit.register(_atexit_with_trace)

    # Checkpoint manager for /undo support.
    try:
        from aru.checkpoints import CheckpointManager
        ctx.checkpoint_manager = CheckpointManager(session.session_id)
    except Exception:
        pass

    # Custom tools discovery (synchronous — no network).
    try:
        from aru.plugins.custom_tools import (
            discover_custom_tools,
            register_custom_tools,
        )
        _disabled = getattr(config, "disabled_tools", []) or []
        _custom_tool_descs = discover_custom_tools(disabled=_disabled)
        if _custom_tool_descs:
            register_custom_tools(_custom_tool_descs)
    except Exception:
        pass

    # Plugin manager (same flow as REPL).
    plugin_mgr = PluginManager()
    ctx.plugin_manager = plugin_mgr
    try:
        _config_dict = {
            "default_model": config.default_model,
            "model_aliases": config.model_aliases,
            "permissions": config.permissions,
            "plugin_specs": config.plugin_specs,
            "disabled_tools": config.disabled_tools,
            "plan_reviewer": getattr(config, "plan_reviewer", None),
        }
        plugin_input = PluginInput(
            directory=os.getcwd(),
            config_path="aru.json" if os.path.isfile("aru.json") else "",
            model_ref=session.model_ref,
            config=_config_dict,
            session=session,
        )
        _plugin_specs = getattr(config, "plugin_specs", None) or []
        plugin_count = await plugin_mgr.load_all(
            plugin_input, plugin_specs=_plugin_specs
        )
        if plugin_count:
            try:
                plugin_tools = plugin_mgr.get_plugin_tools()
                if plugin_tools:
                    register_custom_tools(plugin_tools)
            except Exception:
                pass
    except Exception as exc:
        _logging.getLogger("aru.plugins").warning(
            "TUI plugin loading failed: %s", exc
        )

    # Install auto-formatter (Tier 3 #1).
    try:
        from aru.format.manager import install_format_from_config
        _fmt_mgr = install_format_from_config(getattr(config, "format", None))
        if _fmt_mgr is not None and _fmt_mgr.enabled():
            plugin_mgr.subscribe("file.changed", _fmt_mgr.handle_file_changed)
    except Exception:
        pass

    # Load MCP tools in the background (don't block TUI boot).
    try:
        async def _load_mcp_background():
            from aru.tools.codebase import load_mcp_tools
            await load_mcp_tools()

        asyncio.create_task(_load_mcp_background())
    except Exception:
        pass

    # Publish session.start now that plugins are loaded.
    if plugin_mgr.loaded:
        try:
            await plugin_mgr.publish("session.start", {
                "session_id": getattr(session, "id", None),
                "model_ref": session.model_ref,
                "directory": os.getcwd(),
            })
        except Exception:
            pass

    # Instantiate the App with everything wired up.
    app = AruApp(
        session=session,
        config=config,
        session_store=store,
        ctx=ctx,
        plugin_manager=plugin_mgr,
    )
    ctx.tui_app = app
    ctx.ui = TuiUI(app)

    # Bridge logging → ChatPane so Agno's ERROR records (rate limits,
    # provider errors, etc.) are visible to the user instead of vanishing
    # into Textual's captured stderr. Detached in the finally block.
    log_bridge_handlers: list = []
    try:
        from aru.tui.log_bridge import install_chat_log_bridge
        log_bridge_handlers = install_chat_log_bridge(app)
    except Exception:
        pass

    try:
        await app.run_async()
    finally:
        _loop_tracer.trace("run_tui_finally", "after_run_async")
        try:
            from aru.tui.log_bridge import uninstall_chat_log_bridge
            uninstall_chat_log_bridge(log_bridge_handlers)
        except Exception:
            pass
        _loop_tracer.trace("run_tui_finally", "log_bridge_uninstalled")
        ctx.tui_app = None
        ctx.ui = None
        try:
            store.save(session)
        except Exception:
            pass
        _loop_tracer.trace("run_tui_finally", "session_saved")
        # Restore the shell's original terminal-tab title. We pushed on
        # mount, so popping leaves the user's tab exactly as they
        # handed it to us — no stale "aru — …" lingering after exit.
        try:
            if not getattr(app, "is_headless", False):
                _pop_terminal_title()
        except Exception:
            pass
        # Mirror the REPL farewell so users see where their session went.
        # Printed after Textual has released the terminal so it lands in
        # the real scrollback, not the alt-screen that the TUI just tore
        # down.
        try:
            from aru.display import console as _console
            _console.print(f"\n[dim]Session saved: {session.session_id}[/dim]")
            _console.print(
                f"[dim]Resume with:[/dim] [bold cyan]aru --resume "
                f"{session.session_id}[/bold cyan]"
            )
        except Exception:
            pass
