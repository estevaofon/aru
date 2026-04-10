"""Tests for the plugin system (Phases 1-3)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aru.plugins.tool_api import get_tool_meta, is_custom_tool, tool
from aru.plugins.hooks import HookEvent, Hooks, PluginInput, VALID_HOOKS
from aru.plugins.custom_tools import (
    _build_parameters_from_function,
    _extract_tools_from_module,
    _load_module_from_path,
    _parse_arg_descriptions,
    discover_custom_tools,
    register_custom_tools,
)
from aru.plugins.manager import PluginManager, _parse_plugin_spec


# ── Phase 1: Custom Tools ──────────────────────────────────────────────


class TestToolDecorator:
    def test_tool_decorator_marks_function(self):
        @tool(description="Test tool")
        def my_tool(x: str) -> str:
            return x

        assert is_custom_tool(my_tool)
        meta = get_tool_meta(my_tool)
        assert meta is not None
        assert meta["description"] == "Test tool"
        assert meta["override"] is False

    def test_tool_decorator_with_override(self):
        @tool(description="Override bash", override=True)
        def bash(command: str) -> str:
            return command

        meta = get_tool_meta(bash)
        assert meta["override"] is True

    def test_bare_function_not_marked(self):
        def my_func(x: str) -> str:
            return x

        assert not is_custom_tool(my_func)
        assert get_tool_meta(my_func) is None


class TestParameterBuilding:
    def test_simple_function(self):
        def func(name: str, count: int = 5) -> str:
            """Do something.

            Args:
                name: The name to use.
                count: How many times.
            """
            return ""

        params = _build_parameters_from_function(func)
        assert params["type"] == "object"
        assert "name" in params["properties"]
        assert params["properties"]["name"]["type"] == "string"
        assert params["properties"]["name"]["description"] == "The name to use."
        assert params["properties"]["count"]["type"] == "integer"
        assert params["properties"]["count"]["default"] == 5
        assert "name" in params["required"]
        assert "count" not in params["required"]

    def test_bool_parameter(self):
        def func(dry_run: bool = False) -> str:
            return ""

        params = _build_parameters_from_function(func)
        assert params["properties"]["dry_run"]["type"] == "boolean"

    def test_no_annotations(self):
        def func(x) -> str:
            return ""

        params = _build_parameters_from_function(func)
        assert params["properties"]["x"]["type"] == "string"  # default


class TestArgDescriptionParsing:
    def test_google_style_args(self):
        doc = """Do something.

        Args:
            name: The name.
            count: How many times to repeat.
        """
        descs = _parse_arg_descriptions(doc)
        assert descs["name"] == "The name."
        assert descs["count"] == "How many times to repeat."

    def test_args_with_types(self):
        doc = """Tool.

        Args:
            path (str): The file path.
        """
        descs = _parse_arg_descriptions(doc)
        assert descs["path"] == "The file path."


class TestModuleLoading:
    def test_load_decorated_tool(self, tmp_path):
        tool_file = tmp_path / "tools" / "greet.py"
        tool_file.parent.mkdir(parents=True)
        tool_file.write_text(
            'from aru.plugins import tool\n\n'
            '@tool(description="Greet someone")\n'
            'def greet(name: str) -> str:\n'
            '    return f"Hello, {name}!"\n',
            encoding="utf-8",
        )
        mod = _load_module_from_path(tool_file)
        assert mod is not None
        tools = _extract_tools_from_module(mod)
        assert len(tools) == 1
        assert tools[0][0] == "greet"
        assert tools[0][2] is not None  # has meta

    def test_load_bare_function(self, tmp_path):
        tool_file = tmp_path / "tools" / "hello.py"
        tool_file.parent.mkdir(parents=True)
        tool_file.write_text(
            'def hello(name: str) -> str:\n'
            '    """Say hello."""\n'
            '    return f"Hello, {name}!"\n',
            encoding="utf-8",
        )
        mod = _load_module_from_path(tool_file)
        assert mod is not None
        tools = _extract_tools_from_module(mod)
        assert len(tools) == 1
        assert tools[0][0] == "hello"
        assert tools[0][2] is None  # no meta (bare function)

    def test_skip_non_str_return(self, tmp_path):
        tool_file = tmp_path / "tools" / "bad.py"
        tool_file.parent.mkdir(parents=True)
        tool_file.write_text(
            'def bad_func(x: str) -> int:\n'
            '    return 42\n',
            encoding="utf-8",
        )
        mod = _load_module_from_path(tool_file)
        tools = _extract_tools_from_module(mod)
        assert len(tools) == 0  # int return, not str

    def test_multiple_functions(self, tmp_path):
        tool_file = tmp_path / "tools" / "ci.py"
        tool_file.parent.mkdir(parents=True)
        tool_file.write_text(
            'def build(target: str = "all") -> str:\n'
            '    """Build the project."""\n'
            '    return "built"\n\n'
            'def test(suite: str = "unit") -> str:\n'
            '    """Run tests."""\n'
            '    return "tested"\n',
            encoding="utf-8",
        )
        mod = _load_module_from_path(tool_file)
        tools = _extract_tools_from_module(mod)
        assert len(tools) == 2


class TestDiscoverCustomTools:
    def test_discover_from_directory(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        (tools_dir / "greet.py").write_text(
            'def greet(name: str) -> str:\n'
            '    """Greet someone."""\n'
            '    return f"Hello, {name}!"\n',
            encoding="utf-8",
        )
        result = discover_custom_tools(search_roots=[tmp_path])
        assert len(result) == 1
        assert result[0]["name"] == "greet"
        assert result[0]["description"] == "Greet someone."

    def test_naming_multiple_exports(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        (tools_dir / "ci.py").write_text(
            'def build(target: str = "all") -> str:\n'
            '    """Build the project."""\n'
            '    return "built"\n\n'
            'def test_suite(suite: str = "unit") -> str:\n'
            '    """Run tests."""\n'
            '    return "tested"\n',
            encoding="utf-8",
        )
        result = discover_custom_tools(search_roots=[tmp_path])
        names = {r["name"] for r in result}
        assert "ci_build" in names
        assert "ci_test_suite" in names

    def test_disabled_tools_skipped(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        (tools_dir / "secret.py").write_text(
            'def secret() -> str:\n'
            '    """Secret tool."""\n'
            '    return "secret"\n',
            encoding="utf-8",
        )
        result = discover_custom_tools(search_roots=[tmp_path], disabled=["secret"])
        assert len(result) == 0

    def test_later_root_overrides_earlier(self, tmp_path):
        global_dir = tmp_path / "global"
        local_dir = tmp_path / "local"
        for d in (global_dir, local_dir):
            (d / "tools").mkdir(parents=True)
            (d / "tools" / "greet.py").write_text(
                f'def greet(name: str) -> str:\n'
                f'    """Greet from {d.name}."""\n'
                f'    return "hello"\n',
                encoding="utf-8",
            )
        result = discover_custom_tools(search_roots=[global_dir, local_dir])
        assert len(result) == 1
        assert "local" in result[0]["description"]

    def test_skip_underscore_files(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        (tools_dir / "__init__.py").write_text("", encoding="utf-8")
        (tools_dir / "_helpers.py").write_text(
            'def helper() -> str:\n    return ""\n', encoding="utf-8"
        )
        result = discover_custom_tools(search_roots=[tmp_path])
        assert len(result) == 0


class TestRegisterCustomTools:
    def test_register_adds_to_registry(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        (tools_dir / "greet.py").write_text(
            'def greet(name: str) -> str:\n'
            '    """Greet someone."""\n'
            '    return f"Hello, {name}!"\n',
            encoding="utf-8",
        )
        descs = discover_custom_tools(search_roots=[tmp_path])
        with patch("aru.tools.codebase.ALL_TOOLS", []), \
             patch("aru.tools.codebase.GENERAL_TOOLS", []), \
             patch("aru.tools.codebase.EXECUTOR_TOOLS", []), \
             patch("aru.tools.codebase.TOOL_REGISTRY", {}):
            count = register_custom_tools(descs)
            assert count == 1


# ── Phase 2: Hooks & Plugin Manager ────────────────────────────────────


class TestHooks:
    def test_on_decorator_registers_handler(self):
        hooks = Hooks()

        @hooks.on("tool.execute.before")
        async def handler(event):
            pass

        handlers = hooks.get_handlers("tool.execute.before")
        assert len(handlers) == 1
        assert handlers[0] is handler

    def test_add_handler_programmatic(self):
        hooks = Hooks()
        fn = lambda event: None
        hooks.add_handler("shell.env", fn)
        assert fn in hooks.get_handlers("shell.env")

    def test_tools_dict(self):
        hooks = Hooks()
        hooks.tools["my_tool"] = lambda x: f"result: {x}"
        assert "my_tool" in hooks.tools


class TestHookEvent:
    def test_basic_access(self):
        event = HookEvent(hook="tool.execute.before", data={
            "tool_name": "bash",
            "args": {"command": "ls"},
        })
        assert event.tool_name == "bash"
        assert event.args == {"command": "ls"}

    def test_mutation(self):
        event = HookEvent(hook="tool.execute.after", data={
            "tool_name": "bash",
            "result": "old",
        })
        event.result = "new"
        assert event.result == "new"
        assert event.data["result"] == "new"

    def test_env_access(self):
        event = HookEvent(hook="shell.env", data={"env": {"PATH": "/usr/bin"}})
        assert event.env["PATH"] == "/usr/bin"
        event.env = {"PATH": "/custom/bin"}
        assert event.data["env"]["PATH"] == "/custom/bin"


class TestPluginManager:
    @pytest.fixture
    def plugin_input(self, tmp_path):
        return PluginInput(
            directory=str(tmp_path),
            config_path="",
            model_ref="anthropic/claude-sonnet-4-5",
        )

    async def test_load_from_file(self, tmp_path, plugin_input):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "test_plugin.py").write_text(
            'from aru.plugins.hooks import Hooks, PluginInput\n\n'
            'def plugin(ctx: PluginInput, options=None) -> Hooks:\n'
            '    hooks = Hooks()\n'
            '    hooks.tools["hello"] = lambda name="world": f"Hello, {name}!"\n'
            '    return hooks\n',
            encoding="utf-8",
        )

        mgr = PluginManager()
        count = await mgr.load_all(plugin_input, search_roots=[tmp_path])
        assert count == 1
        assert "test_plugin" in mgr.plugin_names

        plugin_tools = mgr.get_plugin_tools()
        assert len(plugin_tools) == 1
        assert plugin_tools[0]["name"] == "hello"

    async def test_fire_hook(self, tmp_path, plugin_input):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "env_plugin.py").write_text(
            'from aru.plugins.hooks import Hooks, PluginInput\n\n'
            'def plugin(ctx: PluginInput, options=None) -> Hooks:\n'
            '    hooks = Hooks()\n'
            '    @hooks.on("shell.env")\n'
            '    def add_env(event):\n'
            '        event.env["MY_VAR"] = "injected"\n'
            '    return hooks\n',
            encoding="utf-8",
        )

        mgr = PluginManager()
        await mgr.load_all(plugin_input, search_roots=[tmp_path])

        event = await mgr.fire("shell.env", {"env": {}})
        assert event.data["env"]["MY_VAR"] == "injected"

    async def test_fire_async_hook(self, tmp_path, plugin_input):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "async_plugin.py").write_text(
            'from aru.plugins.hooks import Hooks, PluginInput\n\n'
            'async def plugin(ctx: PluginInput, options=None) -> Hooks:\n'
            '    hooks = Hooks()\n'
            '    @hooks.on("tool.execute.before")\n'
            '    async def log_tool(event):\n'
            '        event["logged"] = True\n'
            '    return hooks\n',
            encoding="utf-8",
        )

        mgr = PluginManager()
        await mgr.load_all(plugin_input, search_roots=[tmp_path])

        event = await mgr.fire("tool.execute.before", {"tool_name": "bash"})
        assert event.data.get("logged") is True

    async def test_no_plugins_fires_safely(self, plugin_input):
        mgr = PluginManager()
        await mgr.load_all(plugin_input, plugin_specs=[])
        event = await mgr.fire("tool.execute.before", {"tool_name": "bash"})
        assert event.tool_name == "bash"

    async def test_bad_plugin_skipped(self, tmp_path, plugin_input):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "bad.py").write_text(
            'raise RuntimeError("intentional error")\n',
            encoding="utf-8",
        )
        mgr = PluginManager()
        count = await mgr.load_all(plugin_input, search_roots=[tmp_path])
        assert count == 0  # bad plugin skipped


class TestPluginSpecParsing:
    def test_string_spec(self):
        name, opts = _parse_plugin_spec("my-plugin")
        assert name == "my-plugin"
        assert opts is None

    def test_list_spec_with_options(self):
        name, opts = _parse_plugin_spec(["./plugin.py", {"key": "val"}])
        assert name == "./plugin.py"
        assert opts == {"key": "val"}

    def test_list_spec_without_options(self):
        name, opts = _parse_plugin_spec(["my-plugin"])
        assert name == "my-plugin"
        assert opts is None


class TestConfigIntegration:
    def test_disabled_tools_parsed(self, tmp_path):
        config_file = tmp_path / "aru.json"
        config_file.write_text('{"tools": {"disabled": ["bash", "web_search"]}}', encoding="utf-8")

        with patch("os.getcwd", return_value=str(tmp_path)):
            from aru.config import load_config
            config = load_config(str(tmp_path))
            assert "bash" in config.disabled_tools
            assert "web_search" in config.disabled_tools

    def test_plugin_specs_parsed(self, tmp_path):
        config_file = tmp_path / "aru.json"
        config_file.write_text(
            '{"plugins": ["my-plugin", ["./local.py", {"opt": 1}]]}',
            encoding="utf-8",
        )

        with patch("os.getcwd", return_value=str(tmp_path)):
            from aru.config import load_config
            config = load_config(str(tmp_path))
            assert len(config.plugin_specs) == 2
            assert config.plugin_specs[0] == "my-plugin"
