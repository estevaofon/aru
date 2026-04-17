"""Interactive CLI for aru - a Claude Code clone.

This module is the slim orchestrator: REPL loop, arg parsing, and entrypoint.
All domain logic lives in dedicated modules; public names are re-exported here
for backward compatibility.
"""

from __future__ import annotations

import asyncio
import io as _io
import logging as _logging
import os
import sys

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

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
    _render_home,
    _render_input_separator,
    _sanitize_input,
    aru_logo,
    console,
    format_duration,
    neon_green,
    shadow_green,
)

from aru.completers import (  # noqa: F401
    AruCompleter,
    FileMentionCompleter,
    MentionResult,
    PasteState,
    SlashCommandCompleter,
    TIPS,
    _MENTION_RE,
    _create_prompt_session,
    _extract_agent_mention,
    _resolve_mentions,
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

# ── Imports used only in this module ───────────────────────────────────

from aru.agents.planner import review_plan
from aru.config import load_config, render_command_template, render_skill_template
from aru.permissions import get_skip_permissions, set_permission_mode
from aru.providers import (
    MODEL_ALIASES,
    list_providers,
    resolve_model_ref,
)


def _toggle_yolo_mode(ctx) -> None:
    """Toggle YOLO (dangerously-skip-permissions) mode from the REPL.

    Turning YOLO *off* is unconditional — safety is not at risk.
    Turning YOLO *on* requires an explicit y/n confirmation with a red warning panel.
    """
    if ctx.permission_mode == "yolo":
        set_permission_mode("default")
        console.print("[bold green]✔ YOLO disabled — safe mode restored.[/bold green]")
        return

    warning = Text.from_markup(
        "[bold red]⚠  DANGEROUSLY SKIP PERMISSIONS (YOLO)[/bold red]\n\n"
        "[red]All permission prompts will be bypassed for this session, including:[/red]\n"
        "  • Reading/writing [bold].env[/bold] files and other sensitive paths\n"
        "  • Arbitrary shell commands ([bold]rm -rf[/bold], package installs, network calls)\n"
        "  • Edits outside the working directory\n"
        "  • All sub-agents delegated during this session\n\n"
        "[dim]Toggle off anytime with /yolo or shift+tab.[/dim]"
    )
    console.print(Panel(
        warning,
        title="[bold red]Enable YOLO mode?[/bold red]",
        border_style="red",
        padding=(1, 2),
    ))
    if ask_yes_no("Confirm enabling YOLO mode"):
        set_permission_mode("yolo")
        console.print("[bold red]🔥 YOLO MODE ACTIVE — all permissions bypassed.[/bold red]")
    else:
        console.print("[dim]Cancelled. Remaining in safe mode.[/dim]")


# ── Main REPL ──────────────────────────────────────────────────────────

async def run_cli(skip_permissions: bool = False, resume_id: str | None = None):
    """Main REPL loop."""
    import atexit
    from aru.runtime import init_ctx, get_ctx
    from aru.permissions import parse_permission_config, reset_session as perm_reset_session
    from aru.tools.codebase import cleanup_processes

    # Inject cache breakpoints into Agno's Claude API calls — reduces token
    # consumption by ~40% on multi-tool-call interactions via prompt caching.
    from aru.cache_patch import apply_cache_patch
    apply_cache_patch()

    ctx = init_ctx(console=console, skip_permissions=skip_permissions)

    store = SessionStore()

    def _sync_model(sess: Session):
        """Sync the model IDs to the RuntimeContext from the session's model_ref."""
        ctx.model_id = sess.model_id
        small_ref = config.model_aliases.get("small") if config else None
        if not small_ref:
            provider_key, _ = resolve_model_ref(sess.model_ref)
            _small_defaults = {
                "anthropic": "anthropic/claude-haiku-4-5",
                "openai": "openai/gpt-4o-mini",
                "groq": "groq/llama-3.1-8b-instant",
                "deepseek": "deepseek/deepseek-chat",
                "ollama": "ollama/llama3.1",
            }
            small_ref = _small_defaults.get(provider_key, sess.model_ref)
        ctx.small_model_ref = small_ref

    # Load project configuration
    config = load_config()
    ctx.config = config
    # Populate invoke_skill's dynamic docstring so the LLM-facing schema lists
    # the skills actually available on this machine.
    from aru.tools.skill import _update_invoke_skill_docstring
    _update_invoke_skill_docstring(config.skills)
    if config.agents_md:
        console.print("[dim]Loaded AGENTS.md[/dim]")
    if config.commands:
        console.print(f"[dim]Loaded {len(config.commands)} custom command(s): {', '.join(f'/{k}' for k in config.commands)}[/dim]")
    if config.skills:
        console.print(f"[dim]Loaded {len(config.skills)} skill(s): {', '.join(config.skills.keys())}[/dim]")
    if config.rules_instructions:
        console.print("[dim]Loaded custom instructions from aru.json[/dim]")
    if config.custom_agents:
        primary = [k for k, v in config.custom_agents.items() if v.mode == "primary"]
        subagents = [k for k, v in config.custom_agents.items() if v.mode == "subagent"]
        parts = []
        if primary:
            parts.append(", ".join(f"/{k}" for k in primary))
        if subagents:
            parts.append(f"{len(subagents)} subagent(s)")
        console.print(f"[dim]Loaded {len(config.custom_agents)} custom agent(s): {', '.join(parts)}[/dim]")
        from aru.tools.codebase import set_custom_agents
        set_custom_agents(config.custom_agents)
    if config.permissions:
        ctx.perm_config = parse_permission_config(config.permissions)
        console.print("[dim]Loaded permission config[/dim]")

    extra_instructions = config.get_extra_instructions()

    def _build_env_ctx() -> str:
        """Build fresh environment context for agent system prompt."""
        from aru.runner import build_env_context
        return build_env_context(session)

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
        _sync_model(session)
    else:
        session = Session()
        if config.default_model:
            session.model_ref = config.default_model
        _sync_model(session)
        _render_home(session, skip_permissions)

    # Apply tree_depth from config
    session._tree_max_depth = config.tree_depth

    # Wire session and file-mutation callback
    ctx.session = session
    ctx.on_file_mutation = session.invalidate_context_cache
    atexit.register(lambda: cleanup_processes(ctx.tracked_processes))

    # Initialize checkpoint manager for undo/rewind support
    from aru.checkpoints import CheckpointManager
    ctx.checkpoint_manager = CheckpointManager(session.session_id)
    _turn_counter = 0

    paste_state = PasteState()
    prompt_session = _create_prompt_session(paste_state, config)

    # Load custom tools (synchronous — fast, no network)
    from aru.plugins.custom_tools import discover_custom_tools, register_custom_tools
    _disabled_tools = config.disabled_tools if hasattr(config, "disabled_tools") else []
    _custom_tool_descs = discover_custom_tools(disabled=_disabled_tools)
    if _custom_tool_descs:
        _ct_count = register_custom_tools(_custom_tool_descs)
        console.print(f"[dim]Loaded {_ct_count} custom tool(s): {', '.join(d['name'] for d in _custom_tool_descs)}[/dim]")

    # Load plugins (local imports only, no network)
    from aru.plugins.manager import PluginManager
    from aru.plugins.hooks import PluginInput
    _plugin_mgr = PluginManager()
    ctx.plugin_manager = _plugin_mgr

    try:
        _config_dict = {
            "default_model": config.default_model,
            "model_aliases": config.model_aliases,
            "permissions": config.permissions,
            "plugin_specs": config.plugin_specs,
            "disabled_tools": config.disabled_tools,
            "plan_reviewer": config.plan_reviewer,
        }
        _p_input = PluginInput(
            directory=os.getcwd(),
            config_path="aru.json" if os.path.isfile("aru.json") else "",
            model_ref=session.model_ref,
            config=_config_dict,
            session=session,
        )
        _plugin_specs = config.plugin_specs if hasattr(config, "plugin_specs") else []
        _plugin_count = await _plugin_mgr.load_all(_p_input, plugin_specs=_plugin_specs)
        if _plugin_count:
            plugin_tools = _plugin_mgr.get_plugin_tools()
            if plugin_tools:
                _pt_count = register_custom_tools(plugin_tools)
                console.print(f"[dim]Loaded {_plugin_count} plugin(s): {', '.join(_plugin_mgr.plugin_names)} ({_pt_count} tool(s))[/dim]")
            else:
                console.print(f"[dim]Loaded {_plugin_count} plugin(s): {', '.join(_plugin_mgr.plugin_names)}[/dim]")
    except Exception as exc:
        console.print(f"[dim yellow]Warning: plugin loading failed: {exc}[/dim yellow]")

    # Startup: load MCP tools in background (don't block REPL)
    async def _load_mcp_background():
        from aru.tools.codebase import load_mcp_tools
        await load_mcp_tools()

    asyncio.create_task(_load_mcp_background())

    # Event: session.start
    if _plugin_mgr.loaded:
        try:
            await _plugin_mgr.publish("session.start", {
                "session_id": getattr(session, "id", None),
                "model_ref": session.model_ref,
                "directory": os.getcwd(),
            })
        except Exception:
            pass

    while True:
        try:
            paste_state.clear()
            _render_input_separator()
            model_tb = session.model_display
            from prompt_toolkit.formatted_text import HTML

            def _bottom_toolbar():
                mcp_part = ""
                if ctx.mcp_loaded_msg:
                    mcp_part = (
                        f'  <style fg="ansigray">│</style>'
                        f'  <style fg="ansigray">{ctx.mcp_loaded_msg}</style>'
                    )
                if ctx.permission_mode == "yolo":
                    mode_part = (
                        f'  <style fg="ansigray">│</style>'
                        f'  <b><style fg="ansired">🔥 YOLO — permissions bypassed</style></b>'
                        f'  <style fg="ansigray">(/yolo to toggle)</style>'
                    )
                elif ctx.permission_mode == "acceptEdits":
                    mode_part = (
                        f'  <style fg="ansigray">│</style>'
                        f'  <b><style fg="ansigreen">⏵⏵ auto-accept edits on</style></b>'
                        f'  <style fg="ansigray">(shift+tab to toggle)</style>'
                    )
                else:
                    mode_part = (
                        f'  <style fg="ansigray">│</style>'
                        f'  <style fg="ansigray">shift+tab auto-accept</style>'
                    )
                return HTML(
                    f'  <style fg="ansigray">{model_tb}</style>'
                    f'  <style fg="ansigray">│</style>'
                    f'  <style fg="ansigray">/help</style>'
                    f'  <style fg="ansigray">│</style>'
                    f'  <style fg="ansigray">Esc+Enter newline</style>'
                    f'{mode_part}'
                    f'{mcp_part}'
                )

            user_text = (
                await asyncio.to_thread(
                    prompt_session.prompt,
                    HTML('<b><ansigreen>❯</ansigreen></b> '),
                    multiline=False,
                    bottom_toolbar=_bottom_toolbar,
                )
            ).strip()
            _render_input_separator()
        except (EOFError, KeyboardInterrupt, asyncio.CancelledError):
            store.save(session)
            console.print(f"\n[dim]Session saved: {session.session_id}[/dim]")
            console.print(f"[dim]Resume with:[/dim] [bold cyan]aru --resume {session.session_id}[/bold cyan]")
            console.print("[dim]Bye![/dim]")
            from aru.tools.mcp_client import cleanup_mcp
            await cleanup_mcp()
            break

        user_input = _sanitize_input(paste_state.build_message(user_text))

        # Resolve @file mentions (skip known agent names)
        _agent_names = set(config.custom_agents.keys()) if config.custom_agents else set()
        mention_result = _resolve_mentions(user_input, os.getcwd(), _agent_names)
        attached_images = mention_result.images
        # File contents go into history as separate prunable messages (not inline)
        mention_file_msgs = mention_result.file_messages
        if mention_result.count > 0:
            parts = []
            text_count = mention_result.count - len(attached_images)
            if text_count > 0:
                parts.append(f"{text_count} file(s)")
            if attached_images:
                parts.append(f"{len(attached_images)} image(s)")
            console.print(f"[dim]Attached {', '.join(parts)} from @ mentions[/dim]")
            user_input = mention_result.text

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

        # Inject @file contents as prunable history entries BEFORE the user message.
        # These look like simulated read_file tool calls and can be pruned/compacted
        # normally, unlike inline content which bloats the user message permanently.
        if mention_file_msgs:
            for msg in mention_file_msgs:
                session.add_message(msg["role"], msg["content"])
            mention_file_msgs = []  # consumed

        # Reset "allow all" approvals for each new user message
        perm_reset_session()

        if user_input.lower() == "/undo":
            affected_files = ctx.checkpoint_manager.get_last_snapshot_files()
            if not affected_files and not session.history:
                console.print("[dim]Nothing to undo.[/dim]")
                continue

            # Show what will be reverted
            if affected_files:
                cwd = os.getcwd()
                console.print("[bold]Files that will be restored:[/bold]")
                for f in affected_files:
                    rel = os.path.relpath(f, cwd) if f.startswith(cwd) else f
                    console.print(f"  [cyan]{rel}[/cyan]")

            console.print()
            console.print("[bold]Restore options:[/bold]")
            console.print("  [cyan](b)[/cyan] Restore code and conversation (both)")
            console.print("  [cyan](c)[/cyan] Restore only code (keep conversation)")
            console.print("  [cyan](v)[/cyan] Restore only conversation (keep code)")
            console.print("  [cyan](n)[/cyan] Cancel")
            try:
                choice = console.input("[bold yellow]Choice (b/c/v/n):[/bold yellow] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "n"

            if choice in ("n", ""):
                console.print("[dim]Cancelled.[/dim]")
                continue

            restored_files = []
            msgs_removed = 0

            if choice in ("b", "c"):
                # Restore files from checkpoint
                restored_files, _ = ctx.checkpoint_manager.undo_last_turn()

            if choice in ("b", "v"):
                # Remove last turn from conversation
                msgs_removed = session.undo_last_turn()
                # Conversation restore also reverts plan-mode state — the
                # undone turn may have entered plan mode, and leaving the
                # flag on would block the next turn's mutating tools.
                if session.plan_mode:
                    session.plan_mode = False
                    session.clear_plan()

            parts = []
            if restored_files:
                cwd = os.getcwd()
                for f in restored_files:
                    rel = os.path.relpath(f, cwd) if f.startswith(cwd) else f
                    parts.append(f"  [cyan]{rel}[/cyan]")
                console.print(f"[green]Restored {len(restored_files)} file(s):[/green]")
                for p in parts:
                    console.print(p)
                session.invalidate_context_cache()
            if msgs_removed:
                console.print(f"[green]Removed {msgs_removed} message(s) from conversation.[/green]")
            if not restored_files and not msgs_removed:
                console.print("[dim]Nothing was changed.[/dim]")
            else:
                store.save(session)
            continue

        if user_input.lower() in ("/quit", "/exit", "quit", "exit"):
            # Event: session.end
            if _plugin_mgr.loaded:
                try:
                    await _plugin_mgr.publish("session.end", {
                        "session_id": getattr(session, "id", None),
                    })
                except Exception:
                    pass
            store.save(session)
            console.print(f"\n[dim]Session saved: {session.session_id}[/dim]")
            console.print(f"[dim]Resume with:[/dim] [bold cyan]aru --resume {session.session_id}[/bold cyan]")
            console.print("[dim]Bye![/dim]")
            from aru.tools.mcp_client import cleanup_mcp
            await cleanup_mcp()
            break

        if user_input == "/model" or user_input.startswith("/model "):
            arg = user_input[6:].strip()
            if not arg:
                console.print(f"[bold]Current model:[/bold] {session.model_display} ({session.model_id})")
                console.print()
                if config.model_aliases:
                    console.print("[bold]Model aliases (aru.json):[/bold]")
                    for alias, ref in config.model_aliases.items():
                        console.print(f"  [cyan]{alias}[/cyan] → {ref}")
                    console.print()
                console.print("[bold]Aliases:[/bold]")
                for alias, ref in MODEL_ALIASES.items():
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
                    resolved_ref = config.model_aliases.get(arg_lower, arg_lower) if config.model_aliases else arg_lower
                    provider_key, model_name = resolve_model_ref(resolved_ref)
                    from aru.providers import get_provider
                    provider = get_provider(provider_key)
                    if provider is None:
                        available = ", ".join(sorted(list_providers().keys()))
                        console.print(f"[yellow]Unknown provider '{provider_key}'. Available: {available}[/yellow]")
                    else:
                        session.model_ref = resolved_ref if "/" in resolved_ref else (
                            MODEL_ALIASES.get(resolved_ref, resolved_ref)
                        )
                        _sync_model(session)
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
                console.print("[dim]No skills found. Create skills/<name>/SKILL.md in .agents/ or .claude/[/dim]")
            else:
                console.print("[bold]Available skills:[/bold]\n")
                for name, skill in config.skills.items():
                    invocable = "" if skill.user_invocable else " [dim](model-only)[/dim]"
                    hint = f" [dim]{skill.argument_hint}[/dim]" if skill.argument_hint else ""
                    console.print(f"  [bold cyan]/{name}[/bold cyan]{hint}  {skill.description}{invocable}")
                console.print(f"\n[dim]Invoke with: /skill-name <arguments>[/dim]")
            continue

        if user_input.lower() == "/agents":
            if not config.custom_agents:
                console.print("[dim]No custom agents found. Add .md files to .agents/agents/[/dim]")
            else:
                console.print("[bold]Custom agents:[/bold]\n")
                for name, agent_def in config.custom_agents.items():
                    mode_tag = " [dim](subagent)[/dim]" if agent_def.mode == "subagent" else ""
                    model_tag = f" [dim]({agent_def.model})[/dim]" if agent_def.model else ""
                    console.print(f"  [bold cyan]/{name}[/bold cyan]  {agent_def.description}{mode_tag}{model_tag}")
                console.print(f"\n[dim]Source: .agents/agents/*.md[/dim]")
            continue

        if user_input.lower() == "/mcp":
            from aru.tools.mcp_client import get_mcp_manager
            manager = get_mcp_manager()
            if not manager or not manager.catalog:
                console.print("[dim]No MCP tools loaded. Check aru.mcp.json config.[/dim]")
            else:
                console.print(f"[bold]MCP Tools ({len(manager.catalog)}):[/bold]\n")
                for entry in manager.catalog.values():
                    console.print(f"  [bold cyan]{entry.name}[/bold cyan]  [dim]{entry.description}[/dim]")
            continue

        if user_input.lower() == "/plugin" or user_input.lower().startswith("/plugin "):
            from aru.commands import handle_plugin_command
            rest = user_input[len("/plugin"):].strip()
            handle_plugin_command(rest)
            continue

        if user_input.lower() == "/help":
            _show_help(config)
            continue

        if user_input.lower() == "/cost":
            console.print(Panel(
                session.cost_summary,
                title="[bold]Token Usage & Cost[/bold]",
                border_style="cyan",
                padding=(1, 2),
            ))
            continue

        if user_input.lower() in ("/yolo", "/unsafe"):
            _toggle_yolo_mode(ctx)
            continue

        # Begin a new checkpoint turn for undo support
        _turn_counter += 1
        ctx.checkpoint_manager.begin_turn(_turn_counter)

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

            from aru.runner import PromptInput, prompt as runner_prompt
            plan_result = await runner_prompt(PromptInput(
                session=session,
                message=task,
                agent_name="plan",
                extra_instructions=extra_instructions,
                lightweight=True,
            ))
            plan_content = plan_result.content

            if plan_content and config and config.plan_reviewer:
                console.print("[dim]Reviewing scope...[/dim]")
                reviewed = await review_plan(task, plan_content)
                if reviewed != plan_content:
                    plan_content = reviewed
                    console.print(Markdown(plan_content))

            if plan_content:
                session.set_plan(task, plan_content)
                session.add_message("user", f"/plan {task}")
                session.add_message("assistant", f"[Plan]\n{plan_content}")

                if session.plan_steps:
                    console.print(
                        f"\n[bold]{len(session.plan_steps)} steps stored.[/bold] "
                        f"[dim]Send a message (e.g. \"go\") to start execution; the agent "
                        f"will see a PLAN ACTIVE reminder and call update_plan_step "
                        f"as it progresses.[/dim]"
                    )

        elif user_input.startswith("/") and not user_input.startswith("//"):
            parts = user_input[1:].split(None, 1)
            cmd_name = parts[0].lower()
            cmd_args = parts[1] if len(parts) > 1 else ""

            # Hook: command.execute.before — plugins can block or modify
            _cmd_blocked = False
            try:
                _mgr = ctx.plugin_manager
                if _mgr is not None and _mgr.loaded:
                    _cmd_event = await _mgr.fire("command.execute.before", {
                        "command": cmd_name,
                        "command_args": cmd_args,
                        "blocked": False,
                    })
                    if _cmd_event.data.get("blocked"):
                        console.print(f"[yellow]Command /{cmd_name} blocked by plugin.[/yellow]")
                        _cmd_blocked = True
                    else:
                        cmd_args = _cmd_event.data.get("command_args", cmd_args)
            except Exception:
                pass
            if _cmd_blocked:
                continue

            if cmd_name in config.commands:
                cmd_def = config.commands[cmd_name]
                prompt = render_command_template(cmd_def.template, cmd_args)
                console.print(f"[bold magenta]Running /{cmd_name}...[/bold magenta]")

                env_ctx = _build_env_ctx()
                if cmd_def.agent and cmd_def.agent in config.custom_agents:
                    agent_def = config.custom_agents[cmd_def.agent]
                    agent = await create_custom_agent_instance(agent_def, session, config, env_context=env_ctx)
                elif cmd_def.agent:
                    console.print(f"[yellow]Warning: agent '{cmd_def.agent}' not found, using default[/yellow]")
                    agent = await create_general_agent(session, config, model_override=cmd_def.model, env_context=env_ctx)
                elif cmd_def.model:
                    agent = await create_general_agent(session, config, model_override=cmd_def.model, env_context=env_ctx)
                else:
                    agent = await create_general_agent(session, config, env_context=env_ctx)
                session.add_message("user", user_input)
                await run_agent_capture(agent, prompt, session, images=attached_images or None)
            elif cmd_name in config.skills:
                skill = config.skills[cmd_name]
                if not skill.user_invocable:
                    console.print(f"[yellow]Skill '{cmd_name}' is not user-invocable[/yellow]")
                else:
                    session.active_skill = cmd_name
                    prompt = render_skill_template(skill.content, cmd_args)
                    # Record so the skill body survives compaction — mirror of
                    # claude-code's addInvokedSkill. Store the rendered content
                    # (post-argument substitution) so post-compact restoration
                    # matches what the model initially read.
                    session.record_invoked_skill(cmd_name, prompt, skill.source_path)
                    console.print(f"[bold magenta]Running skill /{cmd_name}...[/bold magenta]")

                    agent = await create_general_agent(session, config, env_context=_build_env_ctx())
                    session.add_message("user", user_input)
                    await run_agent_capture(agent, prompt, session, images=attached_images or None)
            elif cmd_name in config.custom_agents:
                agent_def = config.custom_agents[cmd_name]
                if agent_def.mode == "subagent":
                    console.print(f"[yellow]Agent '{cmd_name}' is a subagent — invoke via delegate_task only[/yellow]")
                else:
                    from aru.permissions import permission_scope
                    console.print(f"[bold magenta]Running agent /{cmd_name}...[/bold magenta]")
                    agent = await create_custom_agent_instance(agent_def, session, config, env_context=_build_env_ctx())
                    session.add_message("user", user_input)
                    with permission_scope(agent_def.permission):
                        await run_agent_capture(agent, cmd_args or user_input, session, images=attached_images or None)
            else:
                console.print(f"[yellow]Unknown command: /{cmd_name}[/yellow]")
                console.print(f"[dim]Built-in: /plan, /model, /sessions, /commands, /skills, /agents, /cost, /quit[/dim]")
                if config.commands:
                    console.print(f"[dim]Custom: {', '.join(f'/{k}' for k in config.commands)}[/dim]")
                if config.skills:
                    invocable = [k for k, v in config.skills.items() if v.user_invocable]
                    if invocable:
                        console.print(f"[dim]Skills: {', '.join(f'/{k}' for k in invocable)}[/dim]")
                if config.custom_agents:
                    primary = [k for k, v in config.custom_agents.items() if v.mode == "primary"]
                    if primary:
                        console.print(f"[dim]Agents: {', '.join(f'/{k}' for k in primary)}[/dim]")

        else:
            # Check for @agent mention anywhere in message
            agent_mention = _extract_agent_mention(user_input, config.custom_agents)
            if agent_mention:
                agent_name, message_text = agent_mention
                agent_def = config.custom_agents[agent_name]
                from aru.permissions import permission_scope
                console.print(f"[bold magenta]Routing to @{agent_name}...[/bold magenta]")
                agent = await create_custom_agent_instance(agent_def, session, config, env_context=_build_env_ctx())
                session.add_message("user", user_input)
                with permission_scope(agent_def.permission):
                    await run_agent_capture(agent, message_text, session, images=attached_images or None)
            else:
                agent = await create_general_agent(session, config, env_context=_build_env_ctx())
                session.add_message("user", user_input)
                await run_agent_capture(agent, user_input, session, images=attached_images or None)

        # Show token usage and auto-save
        if session.token_summary:
            console.print(f"[dim]{session.token_summary}[/dim]")
        store.save(session)


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
    ctx = init_ctx(console=console, skip_permissions=skip_permissions)

    config = load_config()
    ctx.config = config
    # Populate invoke_skill's dynamic docstring (same as interactive path)
    from aru.tools.skill import _update_invoke_skill_docstring
    _update_invoke_skill_docstring(config.skills)
    session = Session()
    if config.default_model:
        session.model_ref = config.default_model

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
            model=create_model(session.model_ref),  # None → provider cap
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

    # Interactive REPL mode
    try:
        asyncio.run(run_cli(skip_permissions=skip_permissions, resume_id=resume_id))
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
