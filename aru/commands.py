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
