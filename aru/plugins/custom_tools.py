"""Discover and load custom tool files (.py) from tool directories.

Discovery paths (later overrides earlier):
  1. ~/.agents/tools/*.py, ~/.aru/tools/*.py  (global)
  2. .agents/tools/*.py, .aru/tools/*.py      (project-local)

Tool files can define tools in two ways:
  - @tool-decorated functions (from aru.plugins import tool)
  - Bare functions with -> str return annotation

Naming convention (mirrors OpenCode):
  - File deploy.py with def deploy -> tool name: "deploy"
  - File ci.py with def build + def test -> tool names: "ci_build", "ci_test"
  - Single function in file -> filename only (no prefix)
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import sys
import types
from pathlib import Path
from typing import Any, Callable

from aru.plugins.tool_api import get_tool_meta

logger = logging.getLogger("aru.plugins")


def _extract_tools_from_module(mod: types.ModuleType) -> list[tuple[str, Callable, dict[str, Any] | None]]:
    """Extract tool functions from a loaded module.

    Returns list of (export_name, function, tool_meta_or_None).
    """
    tools: list[tuple[str, Callable, dict[str, Any] | None]] = []

    for name in dir(mod):
        if name.startswith("_"):
            continue
        obj = getattr(mod, name)
        if not callable(obj) or not inspect.isfunction(obj):
            continue

        meta = get_tool_meta(obj)
        if meta is not None:
            # @tool-decorated function
            tools.append((name, obj, meta))
        else:
            # Bare function — must have -> str annotation
            hints = getattr(obj, "__annotations__", {})
            ret = hints.get("return")
            if ret is str or (isinstance(ret, str) and ret == "str"):
                tools.append((name, obj, None))

    return tools


def _build_parameters_from_function(fn: Callable) -> dict[str, Any]:
    """Build a JSON Schema parameters dict from a function's signature + docstring."""
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    # Parse argument descriptions from docstring Args section
    arg_descriptions = _parse_arg_descriptions(fn.__doc__ or "")

    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }

    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue

        annotation = param.annotation
        json_type = "string"  # default
        if annotation != inspect.Parameter.empty:
            if annotation in type_map:
                json_type = type_map[annotation]
            elif isinstance(annotation, str) and annotation in ("str", "int", "float", "bool", "list", "dict"):
                json_type = type_map.get({"str": str, "int": int, "float": float, "bool": bool, "list": list, "dict": dict}.get(annotation, str), "string")

        prop: dict[str, Any] = {"type": json_type}
        desc = arg_descriptions.get(pname)
        if desc:
            prop["description"] = desc

        if param.default != inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(pname)

        properties[pname] = prop

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def _parse_arg_descriptions(docstring: str) -> dict[str, str]:
    """Parse 'Args:' section from a Google-style docstring."""
    descriptions: dict[str, str] = {}
    in_args = False
    current_arg = ""
    current_desc_parts: list[str] = []

    for line in docstring.splitlines():
        stripped = line.strip()

        if stripped.lower().startswith("args:"):
            in_args = True
            continue

        if in_args:
            # End of Args section: another section header or blank after content
            if stripped and not stripped.startswith("-") and ":" not in stripped and current_arg:
                # Continuation line
                current_desc_parts.append(stripped)
                continue
            if stripped == "" and current_arg:
                descriptions[current_arg] = " ".join(current_desc_parts)
                current_arg = ""
                current_desc_parts = []
                continue
            if stripped == "":
                continue

            # Check for section headers (Returns:, Raises:, etc.)
            if stripped.endswith(":") and not stripped.startswith("-") and " " not in stripped.rstrip(":"):
                if current_arg:
                    descriptions[current_arg] = " ".join(current_desc_parts)
                break

            # Parse "param_name: description" or "param_name (type): description"
            if ":" in stripped:
                if current_arg:
                    descriptions[current_arg] = " ".join(current_desc_parts)

                parts = stripped.split(":", 1)
                arg_part = parts[0].strip().lstrip("-").strip()
                # Remove type annotations like (str) or (int)
                if "(" in arg_part:
                    arg_part = arg_part[:arg_part.index("(")].strip()
                current_arg = arg_part
                current_desc_parts = [parts[1].strip()] if len(parts) > 1 and parts[1].strip() else []

    if current_arg:
        descriptions[current_arg] = " ".join(current_desc_parts)

    return descriptions


def _load_module_from_path(filepath: Path) -> types.ModuleType | None:
    """Dynamically import a Python file as a module."""
    module_name = f"aru_custom_tool_{filepath.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(filepath))
        if spec is None or spec.loader is None:
            logger.warning("Cannot load tool file (no spec): %s", filepath)
            return None
        mod = importlib.util.module_from_spec(spec)
        # Add the file's directory to sys.path temporarily so relative imports work
        parent = str(filepath.parent)
        added = parent not in sys.path
        if added:
            sys.path.insert(0, parent)
        try:
            spec.loader.exec_module(mod)
        finally:
            if added and parent in sys.path:
                sys.path.remove(parent)
        return mod
    except Exception as e:
        logger.warning("Failed to load custom tool %s: %s", filepath, e)
        return None


def discover_custom_tools(
    search_roots: list[Path] | None = None,
    disabled: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Discover custom tool files and return tool descriptors.

    Args:
        search_roots: Directories to scan for tools/ subdirectories.
            If None, uses default paths (global + project-local).
        disabled: List of tool names to skip.

    Returns:
        List of dicts with keys: name, description, parameters, entrypoint, source.
        Later entries override earlier ones (project-local wins).
    """
    from agno.tools import Function

    if search_roots is None:
        search_roots = _default_search_roots()

    disabled_set = set(disabled or [])
    tools_by_name: dict[str, dict[str, Any]] = {}

    for root in search_roots:
        tools_dir = root / "tools"
        if not tools_dir.is_dir():
            continue

        for filepath in sorted(tools_dir.glob("*.py")):
            if filepath.name.startswith("_"):
                continue

            mod = _load_module_from_path(filepath)
            if mod is None:
                continue

            file_stem = filepath.stem
            extracted = _extract_tools_from_module(mod)

            # Naming: single function -> filename; multiple -> filename_exportname
            use_prefix = len(extracted) > 1

            for export_name, fn, meta in extracted:
                if use_prefix and export_name != file_stem and export_name != "default":
                    tool_name = f"{file_stem}_{export_name}"
                else:
                    tool_name = file_stem

                if tool_name in disabled_set:
                    logger.debug("Skipping disabled custom tool: %s", tool_name)
                    continue

                description = ""
                if meta and meta.get("description"):
                    description = meta["description"]
                elif fn.__doc__:
                    # First line of docstring as description
                    description = fn.__doc__.strip().split("\n")[0]
                else:
                    description = f"Custom tool: {tool_name}"

                parameters = _build_parameters_from_function(fn)

                # Wrap async/sync functions uniformly
                original_fn = fn
                if asyncio.iscoroutinefunction(fn):
                    async def entrypoint(*, _fn=original_fn, **kwargs) -> str:
                        result = await _fn(**kwargs)
                        return str(result) if result is not None else ""
                else:
                    async def entrypoint(*, _fn=original_fn, **kwargs) -> str:
                        result = _fn(**kwargs)
                        return str(result) if result is not None else ""

                entrypoint.__name__ = tool_name
                entrypoint.__doc__ = fn.__doc__

                tools_by_name[tool_name] = {
                    "name": tool_name,
                    "description": description,
                    "parameters": parameters,
                    "entrypoint": entrypoint,
                    "source": str(filepath),
                    "override": bool(meta and meta.get("override")),
                }

    return list(tools_by_name.values())


def register_custom_tools(tool_descriptors: list[dict[str, Any]]) -> int:
    """Inject custom tools into the global tool registry.

    Custom tools with the same name as built-in tools will override them.

    Returns:
        Number of tools registered.
    """
    from agno.tools import Function

    from aru.tools.codebase import (
        ALL_TOOLS,
        EXECUTOR_TOOLS,
        GENERAL_TOOLS,
        TOOL_REGISTRY,
    )

    count = 0
    for desc in tool_descriptors:
        name = desc["name"]
        agno_fn = Function(
            name=name,
            description=desc["description"],
            parameters=desc["parameters"],
            entrypoint=desc["entrypoint"],
        )

        # Override existing tool if same name exists
        existing = TOOL_REGISTRY.get(name)
        if existing is not None:
            # Determine what's being overridden for clear logging
            existing_source = getattr(existing, "_aru_source", "built-in")
            for tool_list in (ALL_TOOLS, GENERAL_TOOLS, EXECUTOR_TOOLS):
                for i, t in enumerate(tool_list):
                    t_name = getattr(t, "__name__", None) or getattr(t, "name", None)
                    if t_name == name:
                        tool_list[i] = agno_fn
                        break
            logger.warning("Tool '%s' from %s overrides %s", name, desc["source"], existing_source)
        else:
            ALL_TOOLS.append(agno_fn)
            GENERAL_TOOLS.append(agno_fn)
            EXECUTOR_TOOLS.append(agno_fn)

        agno_fn._aru_source = desc["source"]  # tag for collision logging
        TOOL_REGISTRY[name] = agno_fn
        count += 1
        logger.debug("Registered custom tool: %s (from %s)", name, desc["source"])

    return count


def _default_search_roots() -> list[Path]:
    """Return default tool search roots: global dirs first, then project-local."""
    import os
    roots: list[Path] = []
    home = Path.home()

    # Global roots
    for dirname in (".agents", ".aru"):
        global_dir = home / dirname
        if global_dir.is_dir():
            roots.append(global_dir)

    # Project-local roots
    cwd = Path(os.getcwd())
    for dirname in (".agents", ".aru"):
        local_dir = cwd / dirname
        if local_dir.is_dir():
            roots.append(local_dir)

    return roots
