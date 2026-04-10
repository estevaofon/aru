"""Plugin hooks and event system.

Mirrors OpenCode's hook pattern: plugins return a Hooks object with
event handlers that fire at specific points in Aru's execution.

Usage::

    from aru.plugins import Hooks, PluginInput

    async def plugin(ctx: PluginInput, options: dict | None = None) -> Hooks:
        hooks = Hooks()

        @hooks.on("tool.execute.before")
        async def before_tool(event):
            print(f"About to execute: {event.tool_name}")

        return hooks
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("aru.plugins")


# Valid hook names (mirrors relevant OpenCode hooks)
VALID_HOOKS = frozenset({
    "config",                    # After config loaded
    "tool.execute.before",       # Before any tool runs
    "tool.execute.after",        # After any tool runs
    "tool.definition",           # When tools are resolved (can add/remove)
    "permission.ask",            # Before permission prompt
    "shell.env",                 # Before bash subprocess
    "session.compact",           # Before context compaction
    "event",                     # Subscribe to all events
})


@dataclass
class HookEvent:
    """Payload passed to hook handlers. Handlers can mutate fields."""
    hook: str
    data: dict[str, Any] = field(default_factory=dict)

    # Convenience accessors for common fields
    @property
    def tool_name(self) -> str:
        return self.data.get("tool_name", "")

    @property
    def args(self) -> dict[str, Any]:
        return self.data.get("args", {})

    @args.setter
    def args(self, value: dict[str, Any]) -> None:
        self.data["args"] = value

    @property
    def result(self) -> Any:
        return self.data.get("result")

    @result.setter
    def result(self, value: Any) -> None:
        self.data["result"] = value

    @property
    def env(self) -> dict[str, str]:
        return self.data.get("env", {})

    @env.setter
    def env(self, value: dict[str, str]) -> None:
        self.data["env"] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value

    def __getitem__(self, key: str) -> Any:
        return self.data[key]


@dataclass
class PluginInput:
    """Context passed to plugin initialization."""
    directory: str        # project root (os.getcwd())
    config_path: str      # path to aru.json (or "")
    model_ref: str        # current model reference


class Hooks:
    """Collection of hook handlers returned by a plugin.

    Plugins populate this by using the @hooks.on() decorator or
    by adding tools to hooks.tools dict.
    """

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event_name: str) -> Callable:
        """Decorator to register a hook handler.

        Usage::

            @hooks.on("tool.execute.before")
            async def handler(event: HookEvent):
                ...
        """
        if event_name not in VALID_HOOKS:
            logger.warning("Unknown hook name: %s (valid: %s)", event_name, ", ".join(sorted(VALID_HOOKS)))

        def decorator(fn: Callable) -> Callable:
            self._handlers[event_name].append(fn)
            return fn
        return decorator

    def add_handler(self, event_name: str, fn: Callable) -> None:
        """Programmatically add a hook handler."""
        self._handlers[event_name].append(fn)

    def get_handlers(self, event_name: str) -> list[Callable]:
        """Get all handlers for a given hook."""
        return list(self._handlers.get(event_name, []))

    @property
    def all_handlers(self) -> dict[str, list[Callable]]:
        return dict(self._handlers)
