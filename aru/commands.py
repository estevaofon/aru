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
    ("/sessions", "List recent sessions", "/sessions"),
    ("/commands", "List custom commands", "/commands"),
    ("/skills", "List available skills", "/skills"),
    ("/agents", "List custom agents", "/agents"),
    ("/mcp", "List loaded MCP tools", "/mcp"),
    ("/plugin", "Manage cached plugins (install/list/remove/update)", "/plugin <subcommand>"),
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
