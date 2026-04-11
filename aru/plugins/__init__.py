"""Aru plugin system — custom tools, hooks, and OpenCode TS bridge.

Public API for plugin authors:

    from aru.plugins import tool          # @tool decorator for custom tools
    from aru.plugins import PluginInput, Hooks  # Full plugin API (Phase 2)
"""

from aru.plugins.tool_api import tool
from aru.plugins.hooks import Hooks, HookEvent, PluginInput
from aru.plugins.manager import PluginManager

__all__ = ["tool", "Hooks", "HookEvent", "PluginInput", "PluginManager"]
