"""Slash command definitions, help display, shell execution, and user prompts."""

from __future__ import annotations

import subprocess
import os

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from aru.display import console

SLASH_COMMANDS = [
    ("/help", "Show help and available commands", "/help"),
    ("/plan", "Create an implementation plan", "/plan <task>"),
    ("/model", "Switch model/provider", "/model [provider/model]"),
    ("/reasoning", "Set reasoning effort for this session", "/reasoning [low|medium|high|max|off|clear]"),
    ("/sessions", "List recent sessions", "/sessions"),
    ("/commands", "List custom commands", "/commands"),
    ("/skills", "List available skills", "/skills"),
    ("/agents", "List custom agents", "/agents"),
    ("/subagents", "Show sub-agent invocation tree for this session", "/subagents"),
    ("/subagent", "Show detailed trace for a sub-agent by task_id", "/subagent <task_id>"),
    ("/bg", "List background sub-agent tasks (pending notifications)", "/bg"),
    ("/mcp", "List loaded MCP tools", "/mcp"),
    ("/plugin", "Manage cached plugins (install/list/remove/update)", "/plugin <subcommand>"),
    ("/worktree", "Manage git worktrees (list/create/enter/exit/remove)", "/worktree <subcommand>"),
    ("/memory", "Inspect/manage auto-extracted project memory", "/memory <subcommand>"),
    ("/debug", "Debug utilities (plugin-errors)", "/debug <subcommand>"),
    ("/undo", "Undo last turn — restore files and/or conversation", "/undo"),
    ("/cost", "Show detailed token usage and cost", "/cost"),
    ("/yolo", "Toggle DANGEROUSLY skip all permissions (YOLO mode)", "/yolo"),
    ("/quit", "Exit aru", "/quit"),
]


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


def ask_yes_no(prompt: str) -> bool:
    """Ask the user a yes/no question."""
    try:
        answer = console.input(f"[bold yellow]{prompt} (y/n):[/bold yellow] ").strip().lower()
        return answer in ("y", "yes", "s", "sim")
    except (EOFError, KeyboardInterrupt):
        return False


def handle_subagents_command(session) -> None:
    """Render the session's sub-agent trace tree (`/subagents`).

    Flat tabular output — task_id, agent name, duration, tokens in/out,
    status, task preview. Nested delegations indent under their parent.
    """
    from rich.table import Table

    traces = list(getattr(session, "subagent_traces", []) or [])
    if not traces:
        console.print("[dim]No sub-agents invoked in this session.[/dim]")
        return

    children_of: dict[str | None, list] = {}
    for t in traces:
        children_of.setdefault(t.parent_id, []).append(t)

    status_style = {
        "running": "yellow",
        "completed": "green",
        "cancelled": "dim",
        "error": "red",
    }

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Agent", no_wrap=True)
    table.add_column("Duration", justify="right", no_wrap=True)
    table.add_column("Tokens (in/out)", justify="right", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Task")

    def _emit(node, depth: int = 0):
        indent = "  " * depth + ("└ " if depth else "")
        task_id = node.task_id[:12]
        dur = f"{node.duration:.1f}s" if node.ended_at else "…"
        tokens = f"{node.tokens_in:,}/{node.tokens_out:,}"
        status = f"[{status_style.get(node.status, 'white')}]{node.status}[/]"
        task_preview = (node.task[:60] + "…") if len(node.task) > 60 else node.task
        table.add_row(
            f"{indent}{task_id}", node.agent_name, dur, tokens, status, task_preview
        )
        for child in children_of.get(node.task_id, []):
            _emit(child, depth + 1)

    trace_ids = {t.task_id for t in traces}
    roots = [t for t in traces if t.parent_id not in trace_ids]
    for root in roots:
        _emit(root)

    console.print(
        Panel(table, title=f"Sub-agent invocations ({len(traces)})",
              border_style="cyan", padding=(0, 1))
    )


def handle_subagent_detail_command(session, task_id: str) -> None:
    """Print detailed trace for one sub-agent by task_id prefix."""
    task_id = task_id.strip()
    if not task_id:
        console.print("[yellow]Usage: /subagent <task_id>[/yellow]")
        return

    traces = list(getattr(session, "subagent_traces", []) or [])
    matches = [t for t in traces if t.task_id == task_id or t.task_id.startswith(task_id)]
    if not matches:
        console.print(f"[yellow]No sub-agent found with task_id starting with '{task_id}'[/yellow]")
        return
    if len(matches) > 1:
        console.print(
            f"[yellow]Ambiguous: {len(matches)} traces match '{task_id}'. Showing the first.[/yellow]"
        )

    trace = matches[0]
    lines: list[str] = [
        f"[bold]task_id:[/bold]     {trace.task_id}",
        f"[bold]agent:[/bold]       {trace.agent_name}",
        f"[bold]status:[/bold]      {trace.status}",
        f"[bold]parent:[/bold]      {trace.parent_id or '(primary)'}",
    ]
    if trace.ended_at:
        lines.append(f"[bold]duration:[/bold]    {trace.duration:.2f}s")
    else:
        lines.append("[bold]duration:[/bold]    (running)")
    lines.extend([
        f"[bold]tokens:[/bold]      in={trace.tokens_in:,}  out={trace.tokens_out:,}",
        "",
        "[bold]task:[/bold]",
        f"  {trace.task}",
        "",
    ])
    if trace.tool_calls:
        lines.append(f"[bold]tool calls ({len(trace.tool_calls)}):[/bold]")
        for i, call in enumerate(trace.tool_calls, 1):
            args = call.get("args_preview", "")
            dur = call.get("duration", 0)
            lines.append(f"  {i}. [cyan]{call.get('tool', '?')}[/cyan] ({dur}s)  {args}")
        lines.append("")
    if trace.result:
        lines.append("[bold]result preview:[/bold]")
        lines.append("  " + trace.result.replace("\n", "\n  "))

    console.print(Panel("\n".join(lines), title="Sub-agent trace", border_style="cyan", padding=(1, 2)))


def handle_background_command(session) -> None:
    """List pending background-task notifications (`/bg`).

    Each entry is a sub-agent spawned with `run_in_background=True` that
    has already completed but hasn't been surfaced to the primary agent
    yet. Notifications are drained automatically on the next turn — this
    command just lets the user see what's queued.
    """
    pending = list(getattr(session, "pending_notifications", []) or [])
    if not pending:
        console.print("[dim]No pending background tasks.[/dim]")
        return
    for n in pending:
        tid = n.get("task_id", "?")
        result = n.get("result", "")
        preview = (result[:200] + "…") if len(result) > 200 else result
        console.print(Panel(
            preview,
            title=f"[bold]Background task: {tid}[/bold]",
            border_style="cyan",
            padding=(0, 1),
        ))


def handle_memory_command(args: str, session) -> None:
    """``/memory <subcommand>`` — inspect auto-extracted project memories.

    Subcommands:
        (no args) or "list"   Show MEMORY.md index.
        "show <slug>"         Cat the body of a specific memory.
        "delete <slug>"       Remove a specific memory.
        "clear"               Wipe all memory for this project (prompts y/n).
    """
    from rich.markup import escape

    from aru.memory.store import (
        clear_memory,
        delete_memory,
        list_memories,
        memory_dir_for_project,
        read_memory,
    )

    project_root = getattr(session, "project_root", None) or os.getcwd()
    parts = args.strip().split(None, 1)
    sub = (parts[0].lower() if parts else "list")

    try:
        if sub == "list":
            entries = list_memories(project_root)
            if not entries:
                console.print(
                    "[dim]No memories. Enable auto-extraction via "
                    "\"memory\": {\"auto_extract\": true} in aru.json.[/dim]"
                )
                return
            console.print(f"[bold]Project memories ({len(entries)}):[/bold]\n")
            for e in entries:
                console.print(
                    f"  [cyan]{e.slug}[/cyan]  [dim]{e.type}[/dim]  "
                    f"{escape(e.description or e.name)}"
                )
            mem_dir = memory_dir_for_project(project_root)
            console.print(f"\n[dim]{mem_dir}[/dim]")
            return

        if sub == "show":
            if len(parts) < 2:
                console.print("[yellow]Usage: /memory show <slug>[/yellow]")
                return
            slug = parts[1].strip()
            entry = read_memory(project_root, slug)
            if entry is None:
                console.print(f"[yellow]No memory found with slug: {escape(slug)}[/yellow]")
                return
            console.print(Panel(
                f"[bold]name:[/bold]        {escape(entry.name)}\n"
                f"[bold]description:[/bold] {escape(entry.description)}\n"
                f"[bold]type:[/bold]        {entry.type}\n\n"
                f"{escape(entry.body)}",
                title=f"[bold]{slug}[/bold]",
                border_style="cyan",
                padding=(0, 1),
            ))
            return

        if sub == "delete":
            if len(parts) < 2:
                console.print("[yellow]Usage: /memory delete <slug>[/yellow]")
                return
            slug = parts[1].strip()
            if delete_memory(project_root, slug):
                console.print(f"[green]Deleted memory:[/green] {escape(slug)}")
            else:
                console.print(f"[yellow]No memory with slug: {escape(slug)}[/yellow]")
            return

        if sub == "clear":
            if not ask_yes_no("Delete all project memory? (cannot be undone)"):
                console.print("[dim]Cancelled.[/dim]")
                return
            count = clear_memory(project_root)
            console.print(f"[green]Cleared {count} memory file(s).[/green]")
            return

        console.print(f"[yellow]Unknown /memory subcommand: {sub}[/yellow]")
    except Exception as exc:  # pragma: no cover — defensive UX
        console.print(f"[red]Memory error:[/red] {escape(str(exc))}")


def handle_worktree_command(args: str) -> None:
    """Handle ``/worktree <subcommand>`` — manage git worktrees for the session.

    Subcommands:
        (no args) or "list"   List all worktrees for this repo.
        "create <branch> [from <base>]"   git worktree add — optionally from a base branch.
        "enter <branch>"      chdir into the worktree (creates on the fly if missing).
        "exit"                Return to the project root.
        "remove <branch>"     git worktree remove (force if dirty).
    """
    from rich.table import Table
    from rich.markup import escape

    from aru.tools.worktree import (
        WorktreeError,
        create_worktree,
        list_worktrees,
        remove_worktree,
    )
    from aru.runtime import enter_worktree, exit_worktree, get_ctx

    parts = args.strip().split()
    sub = (parts[0].lower() if parts else "list")

    try:
        if sub == "list":
            entries = list_worktrees()
            if not entries:
                console.print("[dim]No worktrees (are you inside a git repo?)[/dim]")
                return
            table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
            table.add_column("Branch", style="cyan")
            table.add_column("Path")
            table.add_column("Head", style="dim")
            table.add_column("Active", justify="center")
            ctx = get_ctx()
            for e in entries:
                is_here = (ctx.worktree_path and
                           os.path.abspath(ctx.worktree_path) == os.path.abspath(e.path))
                active_mark = "[bold green]*[/]" if is_here else ""
                tag = "[dim](main)[/dim]" if e.is_main else ""
                table.add_row(
                    f"{e.branch or '-'} {tag}".strip(),
                    e.path,
                    e.head[:8],
                    active_mark,
                )
            console.print(table)
            return

        if sub == "create":
            if len(parts) < 2:
                console.print("[yellow]Usage: /worktree create <branch> [from <base>][/yellow]")
                return
            branch = parts[1]
            from_branch = None
            if len(parts) >= 4 and parts[2].lower() == "from":
                from_branch = parts[3]
            path = create_worktree(branch, from_branch=from_branch)
            console.print(f"[green]Worktree created:[/green] {escape(branch)} at {path}")
            return

        if sub == "enter":
            if len(parts) < 2:
                console.print("[yellow]Usage: /worktree enter <branch>[/yellow]")
                return
            branch = parts[1]
            # Create on the fly if it doesn't exist yet
            path = create_worktree(branch)
            enter_worktree(path, branch)
            console.print(f"[green]Entered worktree:[/green] {escape(branch)} ({path})")
            return

        if sub == "exit":
            if exit_worktree():
                console.print("[green]Left worktree — back at project root.[/green]")
            else:
                console.print("[dim]Not inside a worktree.[/dim]")
            return

        if sub == "remove":
            if len(parts) < 2:
                console.print("[yellow]Usage: /worktree remove <branch> [--force][/yellow]")
                return
            branch = parts[1]
            force = "--force" in parts[2:]
            path = remove_worktree(branch, force=force)
            console.print(f"[green]Worktree removed:[/green] {escape(branch)} ({path})")
            return

        console.print(f"[yellow]Unknown /worktree subcommand: {sub}[/yellow]")
    except WorktreeError as exc:
        console.print(f"[red]Worktree error:[/red] {escape(str(exc))}")


async def handle_mcp_command(args: str) -> None:
    """Handle /mcp [status|restart <name>|list] — inspect & recover MCP servers.

    Subcommands:
        (no args) or "list"      List loaded tools (legacy behaviour).
        "status"                 Show per-server health (state, failures, cooldown).
        "restart <name>"         Atomically restart a single server.
    """
    import time as _time
    from rich.table import Table
    from rich.markup import escape

    from aru.tools.mcp_client import get_mcp_manager

    manager = get_mcp_manager()
    parts = args.strip().split(None, 1)

    # Default: list loaded tools (preserves old /mcp behaviour)
    if not parts or parts[0].lower() == "list":
        if not manager or not manager.catalog:
            console.print("[dim]No MCP tools loaded. Check aru.mcp.json config.[/dim]")
            return
        console.print(f"[bold]MCP Tools ({len(manager.catalog)}):[/bold]\n")
        for entry in manager.catalog.values():
            console.print(f"  [bold cyan]{entry.name}[/bold cyan]  [dim]{entry.description}[/dim]")
        return

    sub = parts[0].lower()

    if sub == "status":
        if not manager or not manager.health:
            console.print("[dim]No MCP servers configured.[/dim]")
            return
        state_style = {
            "healthy": "green",
            "initializing": "yellow",
            "cooldown": "yellow",
            "unavailable": "red",
            "failed": "red",
        }
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Server", style="cyan")
        table.add_column("State")
        table.add_column("Tools", justify="right")
        table.add_column("Failures", justify="right")
        table.add_column("Cooldown", justify="right")
        table.add_column("Last Error")
        now = _time.time()
        for h in sorted(manager.health.values(), key=lambda x: x.name):
            tool_count = sum(1 for e in manager.catalog.values() if e.server_name == h.name)
            cooldown_left = max(0, int(h.cooldown_until - now)) if h.cooldown_until else 0
            cooldown_text = f"{cooldown_left}s" if cooldown_left else "-"
            err_preview = (h.last_error[:60] + "…") if len(h.last_error) > 60 else h.last_error
            table.add_row(
                h.name,
                f"[{state_style.get(h.state, 'white')}]{h.state}[/]",
                str(tool_count),
                str(h.consecutive_failures),
                cooldown_text,
                escape(err_preview) if err_preview else "-",
            )
        console.print(Panel(
            table,
            title=f"[bold]MCP servers ({len(manager.health)})[/bold]",
            border_style="cyan",
            padding=(0, 1),
        ))
        return

    if sub == "restart":
        if not manager:
            console.print("[dim]No MCP manager active.[/dim]")
            return
        if len(parts) < 2:
            console.print("[yellow]Usage: /mcp restart <server-name>[/yellow]")
            return
        name = parts[1].strip()
        console.print(f"[dim]Restarting MCP server '{name}'...[/dim]")
        result = await manager.restart_server(name)
        console.print(result)
        return

    console.print(f"[yellow]Unknown /mcp subcommand: {sub}[/yellow]")


def handle_debug_command(args: str) -> None:
    """Handle /debug <subcommand> — inspect internal state.

    Subcommands:
        plugin-errors    Dump recent plugin subscriber/hook errors.
    """
    from rich.table import Table
    from rich.markup import escape
    import datetime

    parts = args.strip().split(None, 1)
    if not parts:
        console.print("[yellow]Usage: /debug <plugin-errors>[/yellow]")
        return

    sub = parts[0].lower()

    if sub in ("plugin-errors", "plugins"):
        from aru.runtime import get_ctx
        try:
            ctx = get_ctx()
        except LookupError:
            console.print("[yellow]No runtime context active.[/yellow]")
            return
        mgr = getattr(ctx, "plugin_manager", None)
        if mgr is None:
            console.print("[dim]No plugin manager active.[/dim]")
            return
        errors = mgr.recent_errors()
        if not errors:
            console.print("[dim]No plugin errors recorded in this session.[/dim]")
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Time", style="dim", no_wrap=True)
        table.add_column("Category", style="cyan", no_wrap=True)
        table.add_column("Event", style="magenta", no_wrap=True)
        table.add_column("Source")
        table.add_column("Error", style="red")
        for e in errors:
            ts = datetime.datetime.fromtimestamp(e["timestamp"]).strftime("%H:%M:%S")
            table.add_row(
                ts,
                str(e.get("category", "?")),
                str(e.get("event", "?")),
                escape(str(e.get("source", ""))),
                escape(f"{e.get('error_type', 'Exception')}: {e.get('error', '')}"),
            )
        console.print(Panel(
            table,
            title=f"[bold]Plugin errors ({len(errors)})[/bold]",
            border_style="red",
            padding=(0, 1),
        ))
        return

    console.print(f"[yellow]Unknown /debug subcommand: {sub}[/yellow]")


def handle_plugin_command(args: str) -> None:
    """Handle /plugin <subcommand> [args] — install/list/remove/update/info."""
    from rich.table import Table
    from rich.markup import escape

    parts = args.strip().split(None, 2)
    if not parts:
        _show_plugin_help()
        return

    subcmd = parts[0].lower()

    if subcmd == "list":
        from aru.plugin_cache import list_installed
        entries = list_installed()
        if not entries:
            console.print("[dim]No plugins installed. Use /plugin install <spec> to add one.[/dim]")
            return
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Name", style="cyan")
        table.add_column("Version", style="green")
        table.add_column("Source")
        table.add_column("Spec", style="dim")
        for e in entries:
            table.add_row(e.id, e.version or "-", e.source, e.spec)
        console.print(table)
        return

    if subcmd == "install":
        if len(parts) < 2:
            console.print("[yellow]Usage: /plugin install <spec> [name][/yellow]")
            return
        spec = parts[1]
        name = parts[2] if len(parts) >= 3 else None
        from aru.plugin_cache import install
        console.print(f"[dim]Installing {escape(spec)}...[/dim]")
        result = install(spec, name=name)
        if not result.ok:
            console.print(f"[red]Install failed: {escape(result.error or 'unknown error')}[/red]")
            return
        provides = result.provides
        provides_str = ", ".join(f"{c} {k}" for k, c in provides.items()) or "no resources"
        console.print(
            f"[green]Installed {escape(result.name or '')}"
            f"{f'@{result.version}' if result.version else ''}[/green] "
            f"([dim]{result.state}[/dim]) -> {escape(str(result.target))}"
        )
        console.print(f"[dim]Provides: {provides_str}[/dim]")
        console.print(
            "[dim]Discovery refreshes on next aru restart. "
            "Skills/agents/tools from the plugin will then be available.[/dim]"
        )
        return

    if subcmd == "remove":
        if len(parts) < 2:
            console.print("[yellow]Usage: /plugin remove <name>[/yellow]")
            return
        name = parts[1]
        from aru.plugin_cache import remove
        if remove(name):
            console.print(f"[green]Removed plugin: {escape(name)}[/green]")
        else:
            console.print(f"[yellow]Plugin not found: {escape(name)}[/yellow]")
        return

    if subcmd == "update":
        if len(parts) < 2:
            console.print("[yellow]Usage: /plugin update <name>[/yellow]")
            return
        name = parts[1]
        from aru.plugin_cache import update
        console.print(f"[dim]Updating {escape(name)}...[/dim]")
        result = update(name)
        if not result.ok:
            console.print(f"[red]Update failed: {escape(result.error or 'unknown error')}[/red]")
            return
        console.print(
            f"[green]Updated {escape(result.name or '')}"
            f"{f'@{result.version}' if result.version else ''}[/green] "
            f"([dim]{result.state}[/dim])"
        )
        return

    if subcmd == "info":
        if len(parts) < 2:
            console.print("[yellow]Usage: /plugin info <name>[/yellow]")
            return
        name = parts[1]
        from aru.plugin_cache import list_installed, read_manifest
        from pathlib import Path
        entries = {e.id: e for e in list_installed()}
        entry = entries.get(name)
        if entry is None:
            console.print(f"[yellow]Plugin not found: {escape(name)}[/yellow]")
            return
        manifest = read_manifest(Path(entry.target))
        console.print(f"[bold cyan]{escape(entry.id)}[/bold cyan]")
        console.print(f"  [dim]version:[/dim]     {entry.version or '-'}")
        console.print(f"  [dim]source:[/dim]      {entry.source}")
        console.print(f"  [dim]spec:[/dim]        {escape(entry.spec)}")
        console.print(f"  [dim]target:[/dim]      {escape(entry.target)}")
        console.print(f"  [dim]fingerprint:[/dim] {entry.fingerprint}")
        console.print(f"  [dim]first_time:[/dim]  {entry.first_time}")
        console.print(f"  [dim]last_time:[/dim]   {entry.last_time}")
        if manifest:
            desc = manifest.get("description")
            if desc:
                console.print(f"  [dim]description:[/dim] {escape(str(desc))}")
            engines = manifest.get("engines") or {}
            if isinstance(engines, dict) and engines.get("aru"):
                console.print(f"  [dim]engines.aru:[/dim] {escape(str(engines['aru']))}")
        return

    _show_plugin_help()


def _show_plugin_help() -> None:
    """Print /plugin command usage."""
    from rich.table import Table
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Subcommand", style="cyan")
    table.add_column("Description", style="dim")
    table.add_row("/plugin install <spec> [name]", "Install a plugin from git or local path")
    table.add_row("/plugin list", "List installed plugins")
    table.add_row("/plugin remove <name>", "Uninstall a plugin")
    table.add_row("/plugin update <name>", "Update a plugin (git pull)")
    table.add_row("/plugin info <name>", "Show plugin metadata")
    console.print(table)
    console.print()
    console.print("[dim]Spec formats:[/dim]")
    console.print("[dim]  github:user/repo            — shorthand for GitHub[/dim]")
    console.print("[dim]  github:user/repo@v1.0.0     — pin to tag/branch[/dim]")
    console.print("[dim]  git+https://host/path.git   — any git URL[/dim]")
    console.print("[dim]  file:///abs/path  or ./rel  — local directory[/dim]")


def _show_help(config) -> None:
    """Display help with available commands."""
    from rich.table import Table

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Command", style="cyan")
    table.add_column("Description", style="dim")

    table.add_row("/plan <task>", "Create detailed implementation plan")
    table.add_row("/model [provider/model]", "Switch models (e.g., ollama/llama3.1, openai/gpt-4o)")
    table.add_row("/sessions", "List recent sessions")
    table.add_row("/commands", "List custom commands")
    table.add_row("/skills", "List available skills")
    table.add_row("/agents", "List custom agents")
    table.add_row("/mcp", "List loaded MCP tools")
    table.add_row("/plugin <subcmd>", "Manage plugins (install/list/remove/update/info)")
    table.add_row("/undo", "Undo last turn (restore files and/or conversation)")
    table.add_row("/help", "Show this help")
    table.add_row("/quit", "Exit aru")
    table.add_row("! <cmd>", "Run shell command")

    if config and config.commands:
        table.add_row("", "")
        for name, cmd_def in config.commands.items():
            table.add_row(f"/{name}", cmd_def.description)

    if config and config.custom_agents:
        primary = {k: v for k, v in config.custom_agents.items() if v.mode == "primary"}
        if primary:
            table.add_row("", "")
            for name, agent_def in primary.items():
                table.add_row(f"/{name}", f"[agent] {agent_def.description}")

    console.print(table)
    console.print()
