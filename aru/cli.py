"""Non-interactive CLI for aru - a Claude Code clone.

This module is the slim orchestrator: arg parsing, one-shot execution, and the
entrypoint that launches the Textual TUI. All domain logic lives in dedicated
modules; public names are re-exported here for backward compatibility.
"""

from __future__ import annotations

import asyncio
import io as _io
import logging as _logging
import os
import sys

# ── Re-exports for backward compatibility ─────────────────────────────
# Tests and external code import these from aru.cli; keep them accessible.

from aru.session import (  # noqa: F401
    DEFAULT_MODEL,
    PlanStep,
    Session,
    SessionStore,
    SESSIONS_DIR,
    _generate_session_id,
    parse_plan_steps,
)

from aru.display import (  # noqa: F401
    StatusBar,
    StreamingDisplay,
    ToolTracker,
    THINKING_PHRASES,
    TOOL_DISPLAY_NAMES,
    TOOL_PRIMARY_ARG,
    _build_logo_with_shadow,
    _format_tool_label,
    aru_logo,
    console,
    format_duration,
    neon_green,
    shadow_green,
)

from aru.commands import (  # noqa: F401
    SLASH_COMMANDS,
    _show_help,
    ask_yes_no,
    run_shell,
)

from aru.runner import (  # noqa: F401
    AgentRunResult,
    _MUTATION_TOOLS,
    build_env_context,
    run_agent_capture,
)

from aru.agent_factory import (  # noqa: F401
    create_custom_agent_instance,
    create_general_agent,
)

# ── Platform setup ─────────────────────────────────────────────────────

if sys.platform == "win32" and not hasattr(sys, "_called_from_test"):
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

_logging.getLogger("agno").setLevel(_logging.WARNING)


def _restore_worktree_from_session(session) -> str:
    """Re-enter the worktree persisted on *session* (Tier 3 #2 R11).

    Called at the top of ``run_tui`` right after the session is bound to
    the runtime context. Three branches:

    - ``worktree_path`` empty or missing → no-op, returns ``"none"``.
    - ``worktree_path`` set AND directory exists on disk → ``enter_worktree``
      is invoked so ``ctx.cwd`` matches the previous run. Returns
      ``"entered"``.
    - ``worktree_path`` set but directory is gone → warn, null the
      session fields, return ``"stale"``.

    Returns a short outcome label (``"none"``/``"entered"``/``"stale"``/
    ``"error"``) so tests can pin the branch chosen without re-parsing
    console output.
    """
    saved_wt = getattr(session, "worktree_path", None)
    saved_branch = getattr(session, "worktree_branch", None)
    if not saved_wt:
        return "none"
    if os.path.isdir(saved_wt):
        from aru.runtime import enter_worktree as _re_enter
        try:
            _re_enter(saved_wt, saved_branch)
            console.print(
                f"[dim]Resumed inside worktree: {saved_branch} ({saved_wt})[/dim]"
            )
            return "entered"
        except Exception as exc:
            console.print(
                f"[yellow]Could not re-enter worktree {saved_wt}: {exc}[/yellow]"
            )
            return "error"
    console.print(
        f"[yellow]Saved worktree path no longer exists: {saved_wt} — "
        f"using project root[/yellow]"
    )
    session.worktree_path = None
    session.worktree_branch = None
    return "stale"


def _configure_plugin_logger(verbose: bool = False) -> None:
    """Attach a stderr handler to the ``aru.plugins`` logger.

    Without this, ``logger.error(...)`` inside ``PluginManager`` (e.g. when a
    subscriber raises) has no handler and disappears silently. Idempotent —
    a marker attribute on the logger prevents double-registration when
    ``run_oneshot`` is invoked multiple times in the same process (tests).
    """
    lg = _logging.getLogger("aru.plugins")
    if getattr(lg, "_aru_handler_attached", False):
        return
    handler = _logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _logging.Formatter("[aru.plugins] %(levelname)s: %(message)s")
    )
    lg.addHandler(handler)
    lg.setLevel(_logging.DEBUG if verbose else _logging.WARNING)
    lg._aru_handler_attached = True  # type: ignore[attr-defined]


# ── CLI entrypoint ─────────────────────────────────────────────────────

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


async def run_oneshot(prompt: str, print_only: bool = False, skip_permissions: bool = False):
    """Run a single prompt non-interactively and exit.

    Args:
        prompt: The user prompt to execute.
        print_only: If True, run without tools (text-only response).
        skip_permissions: If True, skip all permission checks.
    """
    from aru.runtime import init_ctx
    from aru.config import load_config
    from aru.cache_patch import apply_cache_patch

    apply_cache_patch()
    _configure_plugin_logger(verbose=bool(os.environ.get("ARU_VERBOSE")))
    ctx = init_ctx(console=console, skip_permissions=skip_permissions)

    # E6a: install REPL UI so migrated call sites work in one-shot mode.
    from aru.ui import install_repl_ui_on_ctx
    install_repl_ui_on_ctx(ctx)

    config = load_config()
    ctx.config = config
    # Populate invoke_skill's dynamic docstring (same as interactive path)
    from aru.tools.skill import _update_invoke_skill_docstring
    _update_invoke_skill_docstring(config.skills)
    session = Session()
    # Same precedence as run_tui: aru.json default_model wins, otherwise
    # fall back to the most-recent /connect or /model selection persisted
    # in ~/.aru/state.json. Built-in default kicks in only when neither
    # source provides a usable ref.
    if config.default_model:
        session.model_ref = config.default_model
    else:
        try:
            from aru import state as _state
            last = _state.get_last_model()
        except Exception:
            last = None
        if last:
            session.model_ref = last

    ctx.session = session
    ctx.model_id = session.model_id
    small_ref = config.model_aliases.get("small") if config else None
    if not small_ref:
        from aru.providers import resolve_model_ref
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

    extra_instructions = config.get_extra_instructions()

    if print_only:
        # Text-only mode: no tools, just a direct LLM call
        from agno.agent import Agent
        from aru.providers import create_model
        from aru.agents.base import build_instructions

        agent = Agent(
            name="Aru",
            model=create_model(session.model_ref, reasoning_override=session.reasoning_override),
            tools=[],
            instructions=build_instructions("general", extra_instructions),
            markdown=True,
        )
        response = await agent.arun(prompt)
        if response and response.content:
            # Print raw text to stdout for piping
            print(response.content)
    else:
        # Full mode with tools
        from aru.runner import build_env_context
        env_ctx = build_env_context(session)
        agent = await create_general_agent(session, config, env_context=env_ctx)
        session.add_message("user", prompt)
        await run_agent_capture(agent, prompt, session)

        if session.token_summary:
            console.print(f"[dim]{session.token_summary}[/dim]")


def main():
    """Entry point for the aru CLI."""
    from dotenv import load_dotenv

    load_dotenv()
    args = sys.argv[1:]
    skip_permissions = "--dangerously-skip-permissions" in args
    print_only = "--print" in args or "-p" in args

    if "--list" in args:
        _list_sessions_and_exit()
        return

    resume_id = None
    if "--resume" in args:
        idx = args.index("--resume")
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            resume_id = args[idx + 1]
        else:
            resume_id = "last"

    # Collect positional arguments (non-flag, non-flag-value)
    flags_with_value = {"--resume"}
    positional = []
    skip_next = False
    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("--") or arg.startswith("-"):
            if arg in flags_with_value:
                skip_next = True
            continue
        positional.append(arg)

    # Piped stdin: echo "fix bug" | aru
    if not sys.stdin.isatty() and not positional:
        piped_input = sys.stdin.read().strip()
        if piped_input:
            positional = [piped_input]

    # One-shot mode: aru "fix the bug" or aru --print "explain this"
    if positional:
        prompt = " ".join(positional)
        try:
            asyncio.run(run_oneshot(prompt, print_only=print_only, skip_permissions=skip_permissions))
        except (KeyboardInterrupt, asyncio.CancelledError, SystemExit):
            pass
        except Exception as e:
            from rich.markup import escape
            console.print(f"\n[bold red]Fatal error: {escape(str(e))}[/bold red]")
        return

    # Interactive mode — the Textual TUI is the only interactive interface.
    # `--repl` / `--tui` are still accepted as no-op flags for backwards compat.
    from aru.tui import run_tui
    try:
        asyncio.run(run_tui(skip_permissions=skip_permissions, resume_id=resume_id))
    except (KeyboardInterrupt, asyncio.CancelledError, SystemExit):
        _graceful_exit()
    except Exception as e:
        from rich.markup import escape
        console.print(f"\n[bold red]Fatal error: {escape(str(e))}[/bold red]")
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
