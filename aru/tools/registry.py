"""Tool set composition, registry lookup, and MCP gateway loading.

This is the single source of truth for which tools each agent kind sees. All
other tool modules expose individual tools; this module composes them into
the named sets consumed by `agents/catalog.py` and returns `TOOL_REGISTRY`
for name-based lookups.
"""

from __future__ import annotations

from aru.runtime import get_ctx
from aru.tools import delegate as _delegate_module
from aru.tools._shared import _thread_tool
from aru.tools.delegate import delegate_task
from aru.tools.file_ops import (
    _edit_file_tool,
    _edit_files_tool,
    _list_directory_tool,
    _read_file_tool,
    _write_file_tool,
    _write_files_tool,
    read_files,
)
from aru.tools.plan_mode import enter_plan_mode, exit_plan_mode
from aru.tools.ranker import rank_files
from aru.tools.search import _glob_search_tool, _grep_search_tool
from aru.tools.shell import bash
from aru.tools.skill import invoke_skill
from aru.tools.tasklist import create_task_list, update_plan_step, update_task
from aru.tools.web import web_fetch, web_search


_rank_files_tool = _thread_tool(rank_files)


# Tool sets composed from a single core set — avoid duplication and drift.
_READ_ONLY_TOOLS = [
    _read_file_tool,
    read_files,
    _glob_search_tool,
    _grep_search_tool,
    _list_directory_tool,
]

_WRITE_TOOLS = [
    _write_file_tool,
    _write_files_tool,
    _edit_file_tool,
    _edit_files_tool,
]

_NET_TOOLS = [web_search, web_fetch]

_TASK_MGMT_TOOLS = [
    create_task_list,
    update_task,
    update_plan_step,
    enter_plan_mode,
    exit_plan_mode,
]

# Skill invocation tool — lets primary agents load another skill's SKILL.md
# into the next turn's context. Kept separate from _TASK_MGMT_TOOLS for
# clarity; excluded from subagent / planner / explorer sets.
_SKILL_TOOLS = [invoke_skill]

CORE_TOOLS = _READ_ONLY_TOOLS + _WRITE_TOOLS + [bash] + _NET_TOOLS + [delegate_task]

ALL_TOOLS = _TASK_MGMT_TOOLS + _SKILL_TOOLS + CORE_TOOLS

# GENERAL_TOOLS and EXECUTOR_TOOLS used to diverge silently; both are now the
# same canonical set. Keep separate names for callers that reference them.
GENERAL_TOOLS = ALL_TOOLS
EXECUTOR_TOOLS = ALL_TOOLS

# Planner tools — read-only subset (no write/edit/bash/net/delegate)
PLANNER_TOOLS = list(_READ_ONLY_TOOLS)

# Explorer tools — read-only with bash and ranker (subagent for fast research)
EXPLORER_TOOLS = _READ_ONLY_TOOLS + [bash, _rank_files_tool]

# Set the subagent default tool list on the delegate module. We assign
# the attribute via the module reference (not via an imported symbol) so
# there is a single authoritative binding at
# `aru.tools.delegate._DEFAULT_SUBAGENT_TOOLS`. Using `from ... import` +
# in-place `[:]=` created TWO bindings in separate module namespaces
# that could diverge under `monkeypatch.setattr` (the teardown restored
# only one of them). Assignment-through-module keeps test semantics clean.
_delegate_module._DEFAULT_SUBAGENT_TOOLS = (
    _READ_ONLY_TOOLS + _WRITE_TOOLS + [bash] + _NET_TOOLS + [_rank_files_tool]
)


# Registry mapping tool name strings to function references.
# Keys follow the wrapper __name__, which functools.wraps sets to the
# original sync function name (e.g. "read_file"), so lookups from the
# LLM side resolve to the async wrapper transparently.
TOOL_REGISTRY: dict[str, object] = {f.__name__: f for f in ALL_TOOLS}
TOOL_REGISTRY["rank_files"] = _rank_files_tool


def resolve_tools(tool_spec: list[str] | dict[str, bool]) -> list:
    """Resolve a tool specification to a list of tool functions.

    Args:
        tool_spec: Either:
            - Empty list: returns GENERAL_TOOLS (default set)
            - List of strings: allowlist of tool names
            - Dict[str, bool]: starts from GENERAL_TOOLS, adds/removes by name
    """
    if isinstance(tool_spec, dict):
        result = list(GENERAL_TOOLS)
        for name, enabled in tool_spec.items():
            func = TOOL_REGISTRY.get(name)
            if func is None:
                continue
            if enabled and func not in result:
                result.append(func)
            elif not enabled and func in result:
                result.remove(func)
        return result

    if not tool_spec:
        return list(GENERAL_TOOLS)

    resolved = []
    for name in tool_spec:
        func = TOOL_REGISTRY.get(name)
        if func:
            resolved.append(func)
    return resolved


async def load_mcp_tools(eager: bool = False):
    """Initialize MCP servers and expose their tools to agents.

    Args:
        eager: If True, inject each MCP tool as its own Agno Function (legacy mode).
               If False (default), inject a single gateway tool + lightweight catalog
               in the system prompt — saves thousands of tokens per turn.
    """
    from aru.tools.mcp_client import init_mcp
    try:
        manager = await init_mcp()
        if manager is None or not manager.catalog:
            return

        tool_count = len(manager.catalog)

        if eager:
            mcp_tools = manager.get_eager_tools()
            get_ctx().mcp_loaded_msg = f"Loaded {tool_count} tools from MCP servers (eager mode)."
            for t in mcp_tools:
                ALL_TOOLS.append(t)
                EXECUTOR_TOOLS.append(t)
                GENERAL_TOOLS.append(t)
        else:
            gateway = _build_mcp_gateway(manager)
            ALL_TOOLS.append(gateway)
            EXECUTOR_TOOLS.append(gateway)
            GENERAL_TOOLS.append(gateway)
            get_ctx().mcp_catalog_text = manager.get_catalog_text()
            get_ctx().mcp_loaded_msg = f"Loaded {tool_count} tools from MCP servers."

    except Exception as e:
        get_ctx().mcp_loaded_msg = f"Failed to load MCP tools: {e}"


def _build_mcp_gateway(manager):
    """Build the single gateway Function that routes to any MCP tool."""
    from agno.tools import Function

    async def use_mcp_tool(tool_name: str, arguments: dict | None = None) -> str:
        """Call an external MCP tool by name, or pass tool_name="list" to see all available tools.

        Args:
            tool_name: The MCP tool name (e.g. "github__search_repositories"), or "list" to see the catalog.
            arguments: The arguments to pass to the tool as key-value pairs.
        """
        if tool_name == "list":
            return manager.get_catalog_text() or "No MCP tools available."
        result = await manager.call_tool(tool_name, arguments)
        if result.startswith("Error: Unknown MCP tool"):
            result += "\n\n" + (manager.get_catalog_text() or "")
        return result

    servers = sorted(set(e.server_name for e in manager.catalog.values()))
    server_list = ", ".join(servers) if servers else "none"
    return Function(
        name="use_mcp_tool",
        description=f'Call an external MCP tool by name. Available servers: {server_list}. Pass tool_name="list" to discover all tools and their parameters.',
        parameters={
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": 'MCP tool name (e.g. "github__search_repositories") or "list" to see all available tools'
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments to pass to the tool as key-value pairs",
                    "additionalProperties": True
                }
            },
            "required": ["tool_name"]
        },
        entrypoint=use_mcp_tool,
    )
