"""Plugin manager — loads plugins and fires hooks.

The PluginManager is stored on RuntimeContext and accessible from anywhere
via get_ctx().plugin_manager.

Plugin sources:
  1. aru.json -> "plugins": ["name", ["./path.py", {options}]]
  2. Auto-discovery: .aru/plugins/*.py, .agents/plugins/*.py
  3. Installed packages: entry_points(group="aru.plugins")
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import importlib.util
import inspect
import logging
import sys
import types
from pathlib import Path
from typing import Any, Callable

from aru.plugins.hooks import VALID_HOOKS, HookEvent, Hooks, PluginInput

logger = logging.getLogger("aru.plugins")


@property
def _noop_manager():
    """Fallback for when no plugin manager is active."""
    return None


class PluginManager:
    """Loads plugins, aggregates hooks, and fires events."""

    def __init__(self) -> None:
        self._hooks: list[Hooks] = []
        self._plugin_names: list[str] = []
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def plugin_count(self) -> int:
        return len(self._plugin_names)

    @property
    def plugin_names(self) -> list[str]:
        return list(self._plugin_names)

    async def load_all(
        self,
        plugin_input: PluginInput,
        plugin_specs: list[str | list] | None = None,
        search_roots: list[Path] | None = None,
    ) -> int:
        """Load all plugins from config specs + auto-discovery.

        Args:
            plugin_input: Context passed to each plugin's init function.
            plugin_specs: Plugin specs from aru.json "plugins" key.
            search_roots: Directories to scan for plugins/ subdirectories.

        Returns:
            Number of plugins loaded.
        """
        count = 0

        # 1. Config-specified plugins
        for spec in (plugin_specs or []):
            name, options = _parse_plugin_spec(spec)
            try:
                hooks = await self._load_one(name, plugin_input, options)
                if hooks:
                    self._hooks.append(hooks)
                    self._plugin_names.append(name)
                    count += 1
            except Exception as e:
                logger.error("Failed to load plugin %s: %s", name, e)

        # 2. Auto-discovery from directories
        if search_roots is None:
            search_roots = _default_plugin_roots()

        for root in search_roots:
            plugins_dir = root / "plugins"
            if not plugins_dir.is_dir():
                continue
            for filepath in sorted(plugins_dir.glob("*.py")):
                if filepath.name.startswith("_"):
                    continue
                name = filepath.stem
                if name in self._plugin_names:
                    continue  # already loaded via config
                try:
                    hooks = await self._load_from_file(filepath, plugin_input)
                    if hooks:
                        self._hooks.append(hooks)
                        self._plugin_names.append(name)
                        count += 1
                except Exception as e:
                    logger.error("Failed to load plugin %s: %s", filepath, e)

        # 3. Entry points (installed packages)
        try:
            eps = importlib.metadata.entry_points(group="aru.plugins")
            for ep in eps:
                if ep.name in self._plugin_names:
                    continue
                try:
                    mod = ep.load()
                    hooks = await self._init_plugin_module(mod, ep.name, plugin_input, None)
                    if hooks:
                        self._hooks.append(hooks)
                        self._plugin_names.append(ep.name)
                        count += 1
                except Exception as e:
                    logger.error("Failed to load entry-point plugin %s: %s", ep.name, e)
        except Exception:
            pass  # entry_points may fail on older Python

        self._loaded = True
        return count

    async def fire(self, event_name: str, data: dict[str, Any] | None = None) -> HookEvent:
        """Fire a hook event through all registered handlers.

        Handlers run sequentially (not parallel) so they can mutate the event.

        Returns:
            The (possibly mutated) HookEvent.
        """
        event = HookEvent(hook=event_name, data=data or {})

        for hooks in self._hooks:
            handlers = hooks.get_handlers(event_name)
            for handler in handlers:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(event)
                    else:
                        handler(event)
                except PermissionError:
                    raise  # let blocking signals propagate
                except Exception as e:
                    logger.error("Hook handler error (%s): %s", event_name, e)

        return event

    def get_plugin_tools(self) -> list[dict[str, Any]]:
        """Collect all tools registered by plugins.

        Returns list of dicts compatible with custom_tools.register_custom_tools().
        """
        from aru.plugins.custom_tools import _build_parameters_from_function

        tools: list[dict[str, Any]] = []
        for hooks in self._hooks:
            for name, tool_def in hooks.tools.items():
                if callable(tool_def):
                    # Plain function registered as tool
                    fn = tool_def
                    from aru.plugins.tool_api import get_tool_meta
                    meta = get_tool_meta(fn)
                    desc = (meta.get("description") if meta else None) or \
                           (fn.__doc__ or "").strip().split("\n")[0] or \
                           f"Plugin tool: {name}"

                    if asyncio.iscoroutinefunction(fn):
                        async def entrypoint(*, _fn=fn, **kwargs) -> str:
                            result = await _fn(**kwargs)
                            return str(result) if result is not None else ""
                    else:
                        async def entrypoint(*, _fn=fn, **kwargs) -> str:
                            result = _fn(**kwargs)
                            return str(result) if result is not None else ""
                    entrypoint.__name__ = name
                    entrypoint.__doc__ = fn.__doc__

                    tools.append({
                        "name": name,
                        "description": desc,
                        "parameters": _build_parameters_from_function(fn),
                        "entrypoint": entrypoint,
                        "source": "plugin",
                        "override": False,
                    })
                elif isinstance(tool_def, dict):
                    # Dict-based tool definition
                    execute_fn = tool_def.get("execute")
                    if not callable(execute_fn):
                        continue
                    desc = tool_def.get("description", f"Plugin tool: {name}")

                    if asyncio.iscoroutinefunction(execute_fn):
                        async def entrypoint(*, _fn=execute_fn, **kwargs) -> str:
                            result = await _fn(**kwargs)
                            return str(result) if result is not None else ""
                    else:
                        async def entrypoint(*, _fn=execute_fn, **kwargs) -> str:
                            result = _fn(**kwargs)
                            return str(result) if result is not None else ""
                    entrypoint.__name__ = name

                    params = tool_def.get("parameters")
                    if params is None:
                        params = _build_parameters_from_function(execute_fn)

                    tools.append({
                        "name": name,
                        "description": desc,
                        "parameters": params,
                        "entrypoint": entrypoint,
                        "source": "plugin",
                        "override": False,
                    })

        return tools

    # -- Internal loading helpers --

    async def _load_one(
        self, name: str, plugin_input: PluginInput, options: dict | None,
    ) -> Hooks | None:
        """Load a single plugin by name or path."""
        # File path?
        if name.startswith(".") or name.startswith("/") or name.startswith("file://"):
            path_str = name.replace("file://", "")
            filepath = Path(plugin_input.directory) / path_str
            return await self._load_from_file(filepath, plugin_input, options)

        # Try as installed package
        try:
            mod = importlib.import_module(name)
            return await self._init_plugin_module(mod, name, plugin_input, options)
        except ImportError:
            logger.warning("Plugin not found: %s", name)
            return None

    async def _load_from_file(
        self, filepath: Path, plugin_input: PluginInput, options: dict | None = None,
    ) -> Hooks | None:
        """Load a plugin from a .py file."""
        if not filepath.is_file():
            logger.warning("Plugin file not found: %s", filepath)
            return None

        module_name = f"aru_plugin_{filepath.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(filepath))
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            parent = str(filepath.parent)
            added = parent not in sys.path
            if added:
                sys.path.insert(0, parent)
            try:
                spec.loader.exec_module(mod)
            finally:
                if added and parent in sys.path:
                    sys.path.remove(parent)
            return await self._init_plugin_module(mod, filepath.stem, plugin_input, options)
        except Exception as e:
            logger.error("Failed to load plugin file %s: %s", filepath, e)
            return None

    async def _init_plugin_module(
        self, mod: types.ModuleType, name: str,
        plugin_input: PluginInput, options: dict | None,
    ) -> Hooks | None:
        """Initialize a plugin module by calling its plugin() function."""
        plugin_fn = getattr(mod, "plugin", None)
        if plugin_fn is None:
            # Check for a default export
            plugin_fn = getattr(mod, "default", None)
        if plugin_fn is None or not callable(plugin_fn):
            logger.warning("Plugin %s has no plugin() or default() function", name)
            return None

        try:
            if asyncio.iscoroutinefunction(plugin_fn):
                hooks = await plugin_fn(plugin_input, options)
            else:
                hooks = plugin_fn(plugin_input, options)
            if isinstance(hooks, Hooks):
                logger.info("Loaded plugin: %s", name)
                return hooks
            else:
                logger.warning("Plugin %s returned %s instead of Hooks", name, type(hooks).__name__)
                return None
        except Exception as e:
            logger.error("Plugin %s init failed: %s", name, e)
            return None


def _parse_plugin_spec(spec: str | list) -> tuple[str, dict | None]:
    """Parse a plugin spec from config.

    Formats:
        "plugin-name"                    -> ("plugin-name", None)
        ["./path/plugin.py", {options}]  -> ("./path/plugin.py", {options})
    """
    if isinstance(spec, str):
        return spec, None
    if isinstance(spec, list) and len(spec) >= 1:
        name = str(spec[0])
        options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else None
        return name, options
    return str(spec), None


def _default_plugin_roots() -> list[Path]:
    """Default plugin search roots: global dirs first, then project-local."""
    import os
    roots: list[Path] = []
    home = Path.home()
    for dirname in (".agents", ".aru"):
        d = home / dirname
        if d.is_dir():
            roots.append(d)
    cwd = Path(os.getcwd())
    for dirname in (".agents", ".aru"):
        d = cwd / dirname
        if d.is_dir():
            roots.append(d)
    return roots
