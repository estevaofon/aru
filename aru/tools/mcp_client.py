"""Model Context Protocol (MCP) client manager and tool generation.

Supports two modes for exposing MCP tools to agents:
- **Eager** (legacy): Each MCP tool becomes its own Agno Function with full JSON Schema.
  Sends all tool schemas in every request — expensive with many tools.
- **Lazy** (default): A single gateway tool `use_mcp_tool` replaces all individual tools.
  The tool catalog (name + description) is injected as lightweight text in the system prompt.
  Full schema resolution happens only when the model invokes a specific tool.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from agno.tools import Function
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession


@dataclass
class McpToolEntry:
    """Lightweight catalog entry for a discovered MCP tool."""
    name: str           # safe_name: "server__tool_name"
    description: str    # "[server] original description"
    parameters: dict    # full JSON Schema (only used on invocation)
    server_name: str    # originating MCP server
    original_name: str  # tool name as the MCP server knows it
    session: ClientSession = field(repr=False)


class McpSessionManager:
    """Manages MCP server subprocesses and active client sessions."""

    def __init__(self, config_path: str = "arc.mcp.json"):
        self.config_path = config_path
        self._exit_stack = AsyncExitStack()
        self.sessions: dict[str, ClientSession] = {}
        self.catalog: dict[str, McpToolEntry] = {}

    async def initialize(self):
        """Read config and spawn all MCP servers concurrently."""
        if not os.path.exists(self.config_path):
            return

        with open(self.config_path, "r", encoding="utf-8") as f:
            try:
                config = json.load(f)
            except json.JSONDecodeError:
                print(f"[Warning] Failed to parse {self.config_path}")
                return

        servers = config.get("mcpServers", {})
        tasks = []
        for name, svr_config in servers.items():
            cmd = svr_config.get("command")
            if not cmd:
                continue
            tasks.append(self._start_server(name, svr_config))

        if tasks:
            await asyncio.gather(*tasks)

    async def _start_server(self, name: str, svr_config: dict):
        """Start a single MCP server and register its session."""
        cmd = svr_config.get("command")
        args = svr_config.get("args", [])
        env = svr_config.get("env", None)

        server_params = StdioServerParameters(
            command=cmd,
            args=args,
            env={**os.environ.copy(), **env} if env else None
        )

        try:
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )

            await session.initialize()
            self.sessions[name] = session
        except Exception as e:
            print(f"[Warning] Failed to start MCP server '{name}': {e}")

    async def discover_tools(self) -> int:
        """Fetch all tools from connected servers and populate the catalog.

        Returns the number of tools discovered.
        """
        async def _fetch(server_name: str, session: ClientSession) -> list[McpToolEntry]:
            try:
                result = await session.list_tools()
                entries = []
                for tool in result.tools:
                    safe_name = f"{server_name}__{tool.name}".replace("-", "_")
                    entries.append(McpToolEntry(
                        name=safe_name,
                        description=f"[{server_name}] {tool.description or ''}",
                        parameters=tool.inputSchema,
                        server_name=server_name,
                        original_name=tool.name,
                        session=session,
                    ))
                return entries
            except Exception as e:
                print(f"[Warning] Failed to fetch tools from MCP server '{server_name}': {e}")
                return []

        results = await asyncio.gather(
            *[_fetch(name, sess) for name, sess in self.sessions.items()]
        )
        for entries in results:
            for entry in entries:
                self.catalog[entry.name] = entry

        return len(self.catalog)

    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> str:
        """Execute an MCP tool by its safe name."""
        entry = self.catalog.get(tool_name)
        if entry is None:
            available = ", ".join(sorted(self.catalog.keys()))
            return f"Error: Unknown MCP tool '{tool_name}'. Available: {available}"

        try:
            result = await entry.session.call_tool(entry.original_name, arguments=arguments or {})
            output = []
            for content in result.content:
                if hasattr(content, "text"):
                    output.append(content.text)
            if result.isError:
                return f"Error from {entry.original_name}: " + "\n".join(output)
            return "\n".join(output)
        except Exception as e:
            return f"Error executing {entry.original_name} on {entry.server_name}: {e}"

    def get_catalog_text(self) -> str:
        """Build a lightweight text catalog of available MCP tools.

        This text is injected into the system prompt so the model knows
        which tools exist — without the cost of full JSON Schema per tool.
        """
        if not self.catalog:
            return ""

        lines = ["## MCP Tools (external)\n"]
        lines.append("Call these via `use_mcp_tool(tool_name=\"<name>\", arguments={...})`.\n")

        # Group by server
        by_server: dict[str, list[McpToolEntry]] = {}
        for entry in self.catalog.values():
            by_server.setdefault(entry.server_name, []).append(entry)

        for server, entries in sorted(by_server.items()):
            lines.append(f"### {server}")
            for entry in sorted(entries, key=lambda e: e.name):
                desc = entry.description.split("] ", 1)[-1] if "] " in entry.description else entry.description
                # Include parameter names as hints
                props = entry.parameters.get("properties", {})
                if props:
                    param_hints = ", ".join(props.keys())
                    lines.append(f"- `{entry.name}({param_hints})`: {desc}")
                else:
                    lines.append(f"- `{entry.name}()`: {desc}")
            lines.append("")

        return "\n".join(lines)

    def get_eager_tools(self) -> list[Function]:
        """Create individual Agno Functions for each MCP tool (legacy eager mode)."""
        functions = []
        for entry in self.catalog.values():
            async def mcp_caller(*, _entry=entry, **kwargs) -> str:
                return await self.call_tool(_entry.name, kwargs)

            mcp_caller.__name__ = entry.name

            functions.append(Function(
                name=entry.name,
                description=entry.description,
                parameters=entry.parameters,
                entrypoint=mcp_caller,
            ))
        return functions

    # -- Backward-compatible API (used by tests and eager mode) --

    async def get_tools(self) -> list[Function]:
        """Fetch tools and return as Agno Functions (legacy API).

        Calls discover_tools() if catalog is empty, then returns eager functions.
        """
        if not self.catalog:
            await self.discover_tools()
        return self.get_eager_tools()

    def _create_agno_function(self, server_name: str, session: ClientSession, tool) -> Function:
        """Create a single Agno Function from an MCP tool (legacy API)."""
        safe_name = f"{server_name}__{tool.name}".replace("-", "_")
        description = f"[{server_name}] {tool.description or ''}"
        original_name = tool.name

        async def mcp_caller(**kwargs) -> str:
            try:
                result = await session.call_tool(original_name, arguments=kwargs)
                output = []
                for content in result.content:
                    if hasattr(content, "text"):
                        output.append(content.text)
                if result.isError:
                    return f"Error from {original_name}: " + "\n".join(output)
                return "\n".join(output)
            except Exception as e:
                return f"Error executing {original_name} on {server_name}: {e}"

        mcp_caller.__name__ = safe_name

        return Function(
            name=safe_name,
            description=description,
            parameters=tool.inputSchema,
            entrypoint=mcp_caller,
        )

    async def cleanup(self):
        """Close all active MCP client sessions and terminate server subprocesses."""
        try:
            await self._exit_stack.aclose()
        except (RuntimeError, Exception):
            pass


# Global Singleton manager to be used entirely inside aru's async loops
_manager: McpSessionManager | None = None


async def init_mcp() -> McpSessionManager | None:
    """Initialize MCP servers, discover tools, and return the manager.

    Returns None if no MCP config is found.
    """
    global _manager
    if _manager is None:
        config_path = None
        for path in [
            ".aru/mcp_servers.json",
            "aru.mcp.json",
            ".mcp.json",
            "mcp.json"
        ]:
            if os.path.exists(path):
                config_path = path
                break

        if config_path:
            _manager = McpSessionManager(config_path=config_path)
            await _manager.initialize()
            await _manager.discover_tools()
        else:
            _manager = McpSessionManager(config_path="")
            return None

    return _manager


def get_mcp_manager() -> McpSessionManager | None:
    """Return the global MCP manager (None if not initialized)."""
    return _manager


async def cleanup_mcp():
    """Cleanup global manager."""
    global _manager
    if _manager:
        await _manager.cleanup()
        _manager = None
