"""Compat shim — real implementations live in dedicated submodules.

This module used to be monolithic (~2000 lines covering file ops, search,
shell, web, delegate, diff rendering, and the registry). It was split to
improve testability and discovery; this shim exists so existing imports like
``from aru.tools.codebase import read_file`` keep working without churn.

Do not add new code here. Put it in the appropriate submodule:

- ``aru.tools._shared``   — cross-cutting helpers (notify mutation, thread_tool, truncate)
- ``aru.tools._diff``     — unified-diff rendering for permission prompts + LLM context
- ``aru.tools.file_ops``  — read / write / edit / list / get_project_tree (+ async wrappers)
- ``aru.tools.search``    — glob / grep (ripgrep fast path + pure-Python fallback)
- ``aru.tools.shell``     — bash / run_command / background process tracking
- ``aru.tools.web``       — web_search / web_fetch / HTML-to-text
- ``aru.tools.delegate``  — delegate_task, sub-agent lifecycle, set_custom_agents
- ``aru.tools.registry``  — tool set composition, TOOL_REGISTRY, resolve_tools, MCP gateway
"""

from aru.tools._diff import (
    _compact_diff,
    _format_unified_diff,
    _HUNK_HEADER_RE,
)
from aru.tools._shared import (
    _checkpoint_file,
    _get_small_model_ref,
    _MAX_OUTPUT_CHARS,
    _notify_file_mutation,
    _thread_tool,
    _truncate_output,
)
from aru.tools.delegate import (
    _next_subagent_id,
    _DEFAULT_SUBAGENT_TOOLS,
    _update_delegate_task_docstring,
    delegate_task,
    set_custom_agents,
)
from aru.tools.file_ops import (
    _READ_HARD_CAP,
    _edit_file_tool,
    _edit_files_tool,
    _list_directory_tool,
    _read_file_tool,
    _write_file_tool,
    _write_files_tool,
    clear_read_cache,
    edit_file,
    edit_files,
    get_project_tree,
    list_directory,
    read_file,
    read_files,
    write_file,
    write_files,
)
from aru.tools.registry import (
    _build_mcp_gateway,
    _rank_files_tool,
    ALL_TOOLS,
    CORE_TOOLS,
    EXECUTOR_TOOLS,
    EXPLORER_TOOLS,
    GENERAL_TOOLS,
    PLANNER_TOOLS,
    TOOL_REGISTRY,
    load_mcp_tools,
    resolve_tools,
)
from aru.tools.search import (
    _glob_search_python,
    _glob_search_rg,
    _glob_search_tool,
    _grep_search_python,
    _grep_search_rg,
    _grep_search_tool,
    _rg_path,
    _run_rg,
    glob_search,
    grep_search,
)
from aru.tools.shell import (
    BACKGROUND_PATTERNS,
    _fire_plugin_hook,
    _is_long_running,
    _kill_process_tree,
    _register_process,
    bash,
    cleanup_processes,
    run_command,
)
from aru.tools.web import (
    _ddg_html_search,
    _ddg_lite_search,
    _fetch_direct,
    _fetch_via_jina,
    _html_to_text,
    _HTMLToText,
    web_fetch,
    web_search,
)


__all__ = [
    # File ops
    "clear_read_cache",
    "read_file",
    "read_files",
    "write_file",
    "write_files",
    "edit_file",
    "edit_files",
    "list_directory",
    "get_project_tree",
    # Search
    "glob_search",
    "grep_search",
    # Shell
    "bash",
    "run_command",
    "cleanup_processes",
    # Web
    "web_search",
    "web_fetch",
    # Delegate
    "delegate_task",
    "set_custom_agents",
    # Registry / tool sets
    "CORE_TOOLS",
    "ALL_TOOLS",
    "GENERAL_TOOLS",
    "EXECUTOR_TOOLS",
    "PLANNER_TOOLS",
    "EXPLORER_TOOLS",
    "TOOL_REGISTRY",
    "resolve_tools",
    "load_mcp_tools",
    # Diff (re-exported because tests and plugins import these directly)
    "_format_unified_diff",
    "_html_to_text",
    "_is_long_running",
]
