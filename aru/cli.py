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
    _build_file_context,
    _extract_plan_file_paths,
    execute_plan_steps,
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

from aru.agents.planner import create_planner, review_plan
from aru.config import load_config, render_command_template, render_skill_template
from aru.permissions import get_skip_permissions
from aru.providers import (
    MODEL_ALIASES,
    list_providers,
    resolve_model_ref,
)


# ── Main REPL ──────────────────────────────────────────────────────────

async def run_cli(skip_permissions: bool = False, resume_id: str | None = None):
    """Main REPL loop."""
    import atexit
    from aru.runtime import init_ctx, get_ctx
    from aru.permissions import parse_permission_config, reset_session as perm_reset_session
    from aru.tools.codebase import cleanup_processes

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

    # Wire file-mutation callback and atexit cleanup
    ctx.on_file_mutation = session.invalidate_context_cache
    atexit.register(lambda: cleanup_processes(ctx.tracked_processes))

    planner = None
    executor = None
    paste_state = PasteState()
    prompt_session = _create_prompt_session(paste_state, config)

    # Startup: load MCP tools
    from aru.tools.codebase import load_mcp_tools
    await load_mcp_tools()

    while True:
        try:
            paste_state.clear()
            _render_input_separator()
            model_tb = session.model_display
            from prompt_toolkit.formatted_text import HTML
            user_text = (
                await asyncio.to_thread(
                    prompt_session.prompt,
                    HTML('<b><ansigreen>❯</ansigreen></b> '),
                    multiline=False,
                    bottom_toolbar=HTML(
                        f'  <style fg="ansigray">{model_tb}</style>'
                        f'  <style fg="ansigray">│</style>'
                        f'  <style fg="ansigray">/help</style>'
                        f'  <style fg="ansigray">│</style>'
                        f'  <style fg="ansigray">Esc+Enter newline</style>'
                    ),
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
        resolved, injected = _resolve_mentions(user_input, os.getcwd(), _agent_names)
        if resolved != user_input:
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
        perm_reset_session()

        if user_input.lower() in ("/quit", "/exit", "quit", "exit"):
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

            prompt = task

            plan_result = await run_agent_capture(planner, prompt, session, lightweight=True)
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
                    console.print(f"\n[bold]{len(session.plan_steps)} steps detected.[/bold]")

                if get_skip_permissions() or ask_yes_no("Execute this plan?"):
                    console.print("[bold green]Executing plan...[/bold green]")

                    from aru.agents.executor import create_executor
                    light_instructions = config.get_extra_instructions(lightweight=True) if config else ""

                    def make_executor():
                        return create_executor(session.model_ref, light_instructions)

                    result = await execute_plan_steps(session, make_executor)
                    if result:
                        session.add_message("assistant", f"[Execution]\n{result}")

                session.clear_plan()

        elif user_input.startswith("/") and not user_input.startswith("//"):
            parts = user_input[1:].split(None, 1)
            cmd_name = parts[0].lower()
            cmd_args = parts[1] if len(parts) > 1 else ""

            if cmd_name in config.commands:
                cmd_def = config.commands[cmd_name]
                prompt = render_command_template(cmd_def.template, cmd_args)
                console.print(f"[bold magenta]Running /{cmd_name}...[/bold magenta]")

                agent = create_general_agent(session, config)
                session.add_message("user", user_input)
                run_result = await run_agent_capture(agent, prompt, session)
                if run_result.content:
                    session.add_message("assistant", run_result.with_tools_summary())
            elif cmd_name in config.skills:
                skill = config.skills[cmd_name]
                if not skill.user_invocable:
                    console.print(f"[yellow]Skill '{cmd_name}' is not user-invocable[/yellow]")
                else:
                    prompt = render_skill_template(skill.content, cmd_args)
                    console.print(f"[bold magenta]Running skill /{cmd_name}...[/bold magenta]")

                    agent = create_general_agent(session, config)
                    session.add_message("user", user_input)
                    run_result = await run_agent_capture(agent, prompt, session)
                    if run_result.content:
                        session.add_message("assistant", run_result.with_tools_summary())
            elif cmd_name in config.custom_agents:
                agent_def = config.custom_agents[cmd_name]
                if agent_def.mode == "subagent":
                    console.print(f"[yellow]Agent '{cmd_name}' is a subagent — invoke via delegate_task only[/yellow]")
                else:
                    from aru.permissions import permission_scope
                    console.print(f"[bold magenta]Running agent /{cmd_name}...[/bold magenta]")
                    agent = create_custom_agent_instance(agent_def, session, config)
                    session.add_message("user", user_input)
                    with permission_scope(agent_def.permission):
                        run_result = await run_agent_capture(agent, cmd_args or user_input, session)
                    if run_result.content:
                        session.add_message("assistant", run_result.with_tools_summary())
            else:
                console.print(f"[yellow]Unknown command: /{cmd_name}[/yellow]")
                console.print(f"[dim]Built-in: /plan, /model, /sessions, /commands, /skills, /agents, /quit[/dim]")
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
                agent = create_custom_agent_instance(agent_def, session, config)
                session.add_message("user", user_input)
                with permission_scope(agent_def.permission):
                    run_result = await run_agent_capture(agent, message_text, session)
                if run_result.content:
                    session.add_message("assistant", run_result.with_tools_summary())
            else:
                agent = create_general_agent(session, config)
                session.add_message("user", user_input)
                run_result = await run_agent_capture(agent, user_input, session)
                if run_result.content:
                    session.add_message("assistant", run_result.with_tools_summary())

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


def main():
    """Entry point for the aru CLI."""
    from dotenv import load_dotenv

    load_dotenv()
    args = sys.argv[1:]
    skip_permissions = "--dangerously-skip-permissions" in args

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
