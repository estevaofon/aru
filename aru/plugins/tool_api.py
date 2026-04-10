"""@tool decorator for defining custom tools.

Usage::

    from aru.plugins import tool

    @tool(description="Deploy to staging")
    def deploy(environment: str = "staging") -> str:
        return f"Deployed to {environment}"

    # Or minimal — bare function (no decorator needed):
    def hello(name: str) -> str:
        \"\"\"Greet someone by name.\"\"\"
        return f"Hello, {name}!"
"""

from __future__ import annotations

from typing import Any, Callable


# Sentinel attribute set by @tool to mark decorated functions
_TOOL_MARKER = "_aru_tool_meta"


def tool(
    description: str | None = None,
    *,
    override: bool = False,
) -> Callable:
    """Decorator that marks a function as an Aru custom tool.

    Args:
        description: Tool description shown to the LLM. Falls back to docstring.
        override: If True, explicitly replaces a built-in tool with the same name.
    """
    def decorator(fn: Callable) -> Callable:
        meta: dict[str, Any] = {
            "description": description,
            "override": override,
        }
        setattr(fn, _TOOL_MARKER, meta)
        return fn
    return decorator


def get_tool_meta(fn: Callable) -> dict[str, Any] | None:
    """Return the @tool metadata dict, or None if not decorated."""
    return getattr(fn, _TOOL_MARKER, None)


def is_custom_tool(fn: Callable) -> bool:
    """Check if a function was decorated with @tool."""
    return hasattr(fn, _TOOL_MARKER)
