"""Model Context Protocol (MCP) client manager and tool generation."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack

from agno.tools import Function
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession


class McpSessionManager:
    """Manages MCP server subprocesses and active client sessions."""

    def __init__(self, config_path: str = "arc.mcp.json"):
        self.config_path = config_path
        self._exit_stack = AsyncExitStack()
        self.sessions: dict[str, ClientSession] = {}
        
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

    async def get_tools(self) -> list[Function]:
        """Fetch all tools from connected servers concurrently and convert to Agno Functions."""

        async def _fetch(server_name: str, session: ClientSession) -> list[Function]:
            try:
                result = await session.list_tools()
                return [self._create_agno_function(server_name, session, tool) for tool in result.tools]
            except Exception as e:
                print(f"[Warning] Failed to fetch tools from MCP server '{server_name}': {e}")
                return []

        results = await asyncio.gather(
            *[_fetch(name, sess) for name, sess in self.sessions.items()]
        )
        return [tool for tools in results for tool in tools]

    def _create_agno_function(self, server_name: str, session: ClientSession, tool) -> Function:
        """Dynamically create an Agno Function that routes to the remote MCP tool."""
        
        # We need to capture 'session' and 'tool.name' cleanly.
        # Python's default arguments trick captures loop variables.
        async def mcp_caller(**kwargs) -> str:
            try:
                result = await session.call_tool(tool.name, arguments=kwargs)
                # Parse MCP ToolResultContent
                output = []
                for content in result.content:
                    if hasattr(content, "text"):
                        output.append(content.text)
                if result.isError:
                    return f"Error from {tool.name}: " + "\n".join(output)
                return "\n".join(output)
            except Exception as e:
                return f"Error executing {tool.name} on {server_name}: {e}"

        # Assign __name__ to the callable for Agno's internal representation
        safe_name = f"{server_name}__{tool.name}".replace("-", "_")
        mcp_caller.__name__ = safe_name

        return Function(
            name=safe_name,
            description=f"[{server_name}] {tool.description or ''}",
            parameters=tool.inputSchema,
            entrypoint=mcp_caller
        )

    async def cleanup(self):
        """Close all active MCP client sessions and terminate server subprocesses."""
        try:
            await self._exit_stack.aclose()
        except (RuntimeError, Exception):
            pass


# Global Singleton manager to be used entirely inside aru's async loops
_manager: McpSessionManager | None = None

async def init_mcp() -> list[Function]:
    """Initialize MCP servers and return the loaded Agno functions."""
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
        else:
            # Create an empty manager so cleanup doesn't fail, but return no tools
            _manager = McpSessionManager(config_path="")
            return []
    
    return await _manager.get_tools()

async def cleanup_mcp():
    """Cleanup global manager."""
    global _manager
    if _manager:
        await _manager.cleanup()
        _manager = None
