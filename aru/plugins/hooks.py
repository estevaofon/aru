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
    # Lifecycle
    "config",                    # After config loaded
    "event",                     # Subscribe to all bus events

    # Tool lifecycle
    "tool.execute.before",       # Before any tool runs
    "tool.execute.after",        # After any tool runs
    "tool.execute.failure",      # Tool raised / returned error (Tier 2 #3)
    "tool.definition",           # When tools are resolved (can modify desc/params)

    # Chat lifecycle
    "chat.message",              # Before user message is sent to LLM (can modify)
    "chat.params",               # Before LLM call (can modify model, temperature)
    "chat.system.transform",     # Before LLM call (can modify system prompt)
    "chat.messages.transform",   # Before LLM call (can modify message history)

    # Command lifecycle
    "command.execute.before",    # Before slash command runs (can block/modify)

    # Permission / shell
    "permission.ask",            # Before permission prompt (can auto-allow/deny)
    "permission.denied",         # After user rejection (Tier 2 #3)
    "shell.env",                 # Before bash subprocess
    "session.compact",           # Before context compaction (alias of compact.before)
    "session.compact.before",    # Explicit pre-compact (Tier 2 #3)
    "session.compact.after",     # Post-compact notification (Tier 2 #3)

    # File / workspace (Tier 2 #3)
    "file.changed",              # write/edit/delete/apply_patch notified via _notify_file_mutation
    "cwd.changed",               # enter/exit worktree or manual chdir

    # Worktree (Tier 2 #3)
    "worktree.create",           # After git worktree add
    "worktree.remove",           # After git worktree remove

    # Sub-agent (Tier 2 #3)
    "subagent.start",            # After delegate_task spawns a sub-agent
    "subagent.complete",         # After sub-agent terminates (ok, error, cancelled)
    "subagent.tool.started",     # Inside a sub-agent: a tool call started
    "subagent.tool.completed",   # Inside a sub-agent: a tool call completed

    # Turn lifecycle (Tier 2 #3)
    "turn.start",                # Beginning of runner.prompt for a new user turn
    "turn.end",                  # End of runner.prompt; payload has assistant reply + metrics

    # Intra-turn observability
    "metrics.updated",           # After every internal LLM API call (cache_patch);
                                 # lets the TUI refresh tokens/cost mid-turn so long
                                 # implementation runs don't sit silent for minutes.

    # Tasklist / plan visibility (Tier 2 #6 sidebar)
    "tasklist.updated",          # create_task_list / update_task — full snapshot
    "plan.updated",              # enter_plan_mode / update_plan_step — full snapshot
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

    # -- Chat hook accessors --

    @property
    def message(self) -> str:
        return self.data.get("message", "")

    @message.setter
    def message(self, value: str) -> None:
        self.data["message"] = value

    @property
    def messages(self) -> list:
        return self.data.get("messages", [])

    @messages.setter
    def messages(self, value: list) -> None:
        self.data["messages"] = value

    @property
    def system_prompt(self) -> str:
        return self.data.get("system_prompt", "")

    @system_prompt.setter
    def system_prompt(self, value: str) -> None:
        self.data["system_prompt"] = value

    @property
    def params(self) -> dict[str, Any]:
        return self.data.get("params", {})

    @params.setter
    def params(self, value: dict[str, Any]) -> None:
        self.data["params"] = value

    # -- Command hook accessors --

    @property
    def command(self) -> str:
        return self.data.get("command", "")

    @property
    def command_args(self) -> str:
        return self.data.get("command_args", "")

    @property
    def blocked(self) -> bool:
        return self.data.get("blocked", False)

    @blocked.setter
    def blocked(self, value: bool) -> None:
        self.data["blocked"] = value

    # -- Generic accessors --

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
    config: dict[str, Any] = field(default_factory=dict)  # full config dict
    session: Any = None   # session object (if available at init time)


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
