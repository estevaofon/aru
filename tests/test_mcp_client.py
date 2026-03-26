"""Tests for MCP client manager and tool generation."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from aru.tools.mcp_client import (
    McpSessionManager,
    cleanup_mcp,
    init_mcp,
)


@pytest.fixture
def temp_mcp_config(tmp_path):
    """Create a temporary MCP config file."""
    config_path = tmp_path / "mcp_config.json"
    config = {
        "mcpServers": {
            "test-server": {
                "command": "node",
                "args": ["server.js"],
                "env": {"TEST_VAR": "value"}
            }
        }
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


@pytest.fixture
def empty_config(tmp_path):
    """Create an empty MCP config file."""
    config_path = tmp_path / "empty_config.json"
    config_path.write_text("{}", encoding="utf-8")
    return config_path


@pytest.fixture
def invalid_json_config(tmp_path):
    """Create an invalid JSON config file."""
    config_path = tmp_path / "invalid.json"
    config_path.write_text("{ invalid json", encoding="utf-8")
    return config_path


class TestMcpSessionManager:
    """Tests for McpSessionManager class."""

    def test_init_default_path(self):
        """Test initialization with default config path."""
        manager = McpSessionManager()
        assert manager.config_path == "arc.mcp.json"
        assert manager.sessions == {}
        assert manager._exit_stack is not None

    def test_init_custom_path(self):
        """Test initialization with custom config path."""
        custom_path = "custom/path/config.json"
        manager = McpSessionManager(config_path=custom_path)
        assert manager.config_path == custom_path

    @pytest.mark.asyncio
    async def test_initialize_no_config_file(self, tmp_path):
        """Test initialize when config file doesn't exist."""
        non_existent = str(tmp_path / "nonexistent.json")
        manager = McpSessionManager(config_path=non_existent)
        
        await manager.initialize()
        
        assert manager.sessions == {}

    @pytest.mark.asyncio
    async def test_initialize_invalid_json(self, invalid_json_config, capsys):
        """Test initialize with invalid JSON config."""
        manager = McpSessionManager(config_path=str(invalid_json_config))
        
        await manager.initialize()
        
        captured = capsys.readouterr()
        assert "[Warning] Failed to parse" in captured.out
        assert manager.sessions == {}

    @pytest.mark.asyncio
    async def test_initialize_empty_servers(self, empty_config):
        """Test initialize with config containing no servers."""
        manager = McpSessionManager(config_path=str(empty_config))
        
        await manager.initialize()
        
        assert manager.sessions == {}

    @pytest.mark.asyncio
    async def test_initialize_with_servers(self, temp_mcp_config):
        """Test initialize with valid server config."""
        manager = McpSessionManager(config_path=str(temp_mcp_config))
        
        with patch.object(manager, '_start_server', new_callable=AsyncMock) as mock_start:
            await manager.initialize()
            
            mock_start.assert_called_once()
            call_args = mock_start.call_args
            assert call_args[0][0] == "test-server"
            assert call_args[0][1]["command"] == "node"

    @pytest.mark.asyncio
    async def test_initialize_skip_server_without_command(self, tmp_path):
        """Test initialize skips servers without command field."""
        config_path = tmp_path / "config.json"
        config = {
            "mcpServers": {
                "invalid-server": {
                    "args": ["some", "args"]
                    # Missing "command" field
                }
            }
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        
        manager = McpSessionManager(config_path=str(config_path))
        
        with patch.object(manager, '_start_server', new_callable=AsyncMock) as mock_start:
            await manager.initialize()
            
            mock_start.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_server_success(self):
        """Test successful server startup."""
        manager = McpSessionManager()
        
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        
        svr_config = {
            "command": "node",
            "args": ["server.js"],
            "env": {"VAR": "value"}
        }
        
        async def mock_enter_context(cm):
            """Mock context manager entry."""
            if hasattr(cm, '__aenter__'):
                return await cm.__aenter__()
            return cm
        
        with patch.object(manager._exit_stack, 'enter_async_context', side_effect=[
            (AsyncMock(), AsyncMock()),  # read_stream, write_stream
            mock_session  # session
        ]) as mock_enter:
            await manager._start_server("test-server", svr_config)
            
            assert "test-server" in manager.sessions
            assert manager.sessions["test-server"] == mock_session
            mock_session.initialize.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_server_with_env_vars(self):
        """Test server startup with environment variables."""
        manager = McpSessionManager()
        
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        
        svr_config = {
            "command": "python",
            "args": ["-m", "server"],
            "env": {"CUSTOM_VAR": "custom_value"}
        }
        
        with patch('aru.tools.mcp_client.stdio_client') as mock_stdio, \
             patch.object(manager._exit_stack, 'enter_async_context') as mock_enter:
            
            mock_enter.side_effect = [
                (AsyncMock(), AsyncMock()),
                mock_session
            ]
            
            await manager._start_server("env-server", svr_config)
            
            # Verify server params included environment variables
            assert "env-server" in manager.sessions

    @pytest.mark.asyncio
    async def test_start_server_failure(self, capsys):
        """Test server startup failure handling."""
        manager = McpSessionManager()
        
        svr_config = {
            "command": "nonexistent",
            "args": []
        }
        
        with patch.object(manager._exit_stack, 'enter_async_context', side_effect=Exception("Connection failed")):
            await manager._start_server("failing-server", svr_config)
            
            assert "failing-server" not in manager.sessions
            captured = capsys.readouterr()
            assert "[Warning] Failed to start MCP server 'failing-server'" in captured.out
            assert "Connection failed" in captured.out

    @pytest.mark.asyncio
    async def test_get_tools_empty_sessions(self):
        """Test get_tools with no active sessions."""
        manager = McpSessionManager()
        
        tools = await manager.get_tools()
        
        assert tools == []

    @pytest.mark.asyncio
    async def test_get_tools_success(self):
        """Test successful tool fetching from sessions."""
        manager = McpSessionManager()
        
        # Mock tool object
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"
        mock_tool.description = "A test tool"
        mock_tool.inputSchema = {"type": "object", "properties": {}}
        
        # Mock session
        mock_session = AsyncMock()
        mock_list_result = MagicMock()
        mock_list_result.tools = [mock_tool]
        mock_session.list_tools = AsyncMock(return_value=mock_list_result)
        
        manager.sessions["test-server"] = mock_session
        
        tools = await manager.get_tools()
        
        assert len(tools) == 1
        assert tools[0].name == "test_server__test_tool"  # Hyphens replaced with underscores
        assert "[test-server]" in tools[0].description
        mock_session.list_tools.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_tools_multiple_servers(self):
        """Test tool fetching from multiple servers."""
        manager = McpSessionManager()
        
        # Create mock tools for two servers
        tool1 = MagicMock()
        tool1.name = "tool1"
        tool1.description = "Tool 1"
        tool1.inputSchema = {}
        
        tool2 = MagicMock()
        tool2.name = "tool2"
        tool2.description = "Tool 2"
        tool2.inputSchema = {}
        
        session1 = AsyncMock()
        result1 = MagicMock()
        result1.tools = [tool1]
        session1.list_tools = AsyncMock(return_value=result1)
        
        session2 = AsyncMock()
        result2 = MagicMock()
        result2.tools = [tool2]
        session2.list_tools = AsyncMock(return_value=result2)
        
        manager.sessions["server1"] = session1
        manager.sessions["server2"] = session2
        
        tools = await manager.get_tools()
        
        assert len(tools) == 2
        tool_names = [t.name for t in tools]
        assert "server1__tool1" in tool_names
        assert "server2__tool2" in tool_names

    @pytest.mark.asyncio
    async def test_get_tools_with_failure(self, capsys):
        """Test get_tools handles failures gracefully."""
        manager = McpSessionManager()
        
        # One successful session
        tool1 = MagicMock()
        tool1.name = "good_tool"
        tool1.description = "Good tool"
        tool1.inputSchema = {}
        
        session1 = AsyncMock()
        result1 = MagicMock()
        result1.tools = [tool1]
        session1.list_tools = AsyncMock(return_value=result1)
        
        # One failing session
        session2 = AsyncMock()
        session2.list_tools = AsyncMock(side_effect=Exception("Network error"))
        
        manager.sessions["good-server"] = session1
        manager.sessions["bad-server"] = session2
        
        tools = await manager.get_tools()
        
        # Should still get tool from successful server
        assert len(tools) == 1
        assert tools[0].name == "good_server__good_tool"  # Hyphens replaced with underscores
        
        captured = capsys.readouterr()
        assert "[Warning] Failed to fetch tools from MCP server 'bad-server'" in captured.out

    @pytest.mark.asyncio
    async def test_create_agno_function_basic(self):
        """Test creation of Agno function from MCP tool."""
        manager = McpSessionManager()
        
        mock_tool = MagicMock()
        mock_tool.name = "test-tool"
        mock_tool.description = "Test description"
        mock_tool.inputSchema = {"type": "object"}
        
        mock_session = AsyncMock()
        
        function = manager._create_agno_function("server", mock_session, mock_tool)
        
        assert function.name == "server__test_tool"
        assert "[server]" in function.description
        assert "Test description" in function.description
        assert function.parameters == mock_tool.inputSchema

    @pytest.mark.asyncio
    async def test_create_agno_function_execution_success(self):
        """Test successful execution of created Agno function."""
        manager = McpSessionManager()
        
        mock_tool = MagicMock()
        mock_tool.name = "test-tool"
        mock_tool.description = "Test"
        mock_tool.inputSchema = {}
        
        # Mock successful call_tool result
        mock_content = MagicMock()
        mock_content.text = "Success output"
        
        mock_result = MagicMock()
        mock_result.content = [mock_content]
        mock_result.isError = False
        
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        
        function = manager._create_agno_function("server", mock_session, mock_tool)
        
        result = await function.entrypoint(arg1="value1")
        
        assert result == "Success output"
        mock_session.call_tool.assert_called_once_with(
            "test-tool",
            arguments={"arg1": "value1"}
        )

    @pytest.mark.asyncio
    async def test_create_agno_function_execution_error(self):
        """Test error handling in created Agno function."""
        manager = McpSessionManager()
        
        mock_tool = MagicMock()
        mock_tool.name = "error-tool"
        mock_tool.description = "Error tool"
        mock_tool.inputSchema = {}
        
        mock_content = MagicMock()
        mock_content.text = "Error message"
        
        mock_result = MagicMock()
        mock_result.content = [mock_content]
        mock_result.isError = True
        
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        
        function = manager._create_agno_function("server", mock_session, mock_tool)
        
        result = await function.entrypoint()
        
        assert "Error from error-tool" in result
        assert "Error message" in result

    @pytest.mark.asyncio
    async def test_create_agno_function_execution_exception(self):
        """Test exception handling in created Agno function."""
        manager = McpSessionManager()
        
        mock_tool = MagicMock()
        mock_tool.name = "crash-tool"
        mock_tool.description = "Crash tool"
        mock_tool.inputSchema = {}
        
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=RuntimeError("Connection lost"))
        
        function = manager._create_agno_function("server", mock_session, mock_tool)
        
        result = await function.entrypoint()
        
        assert "Error executing crash-tool on server" in result
        assert "Connection lost" in result

    @pytest.mark.asyncio
    async def test_create_agno_function_name_sanitization(self):
        """Test that tool names with hyphens are sanitized."""
        manager = McpSessionManager()
        
        mock_tool = MagicMock()
        mock_tool.name = "my-hyphenated-tool"
        mock_tool.description = ""
        mock_tool.inputSchema = {}
        
        mock_session = AsyncMock()
        
        function = manager._create_agno_function("my-server", mock_session, mock_tool)
        
        # Hyphens should be replaced with underscores (both in server name and tool name)
        assert function.name == "my_server__my_hyphenated_tool"
        assert "-" not in function.name

    @pytest.mark.asyncio
    async def test_cleanup(self):
        """Test cleanup closes the exit stack."""
        manager = McpSessionManager()
        manager._exit_stack.aclose = AsyncMock()
        
        await manager.cleanup()
        
        manager._exit_stack.aclose.assert_called_once()


class TestGlobalFunctions:
    """Tests for module-level global functions."""

    @pytest.mark.asyncio
    async def test_init_mcp_no_config(self, tmp_path, monkeypatch):
        """Test init_mcp when no config file exists."""
        # Change to temp directory with no config files
        monkeypatch.chdir(tmp_path)
        
        # Reset global manager
        import aru.tools.mcp_client as mcp_module
        mcp_module._manager = None
        
        tools = await init_mcp()
        
        assert tools == []
        assert mcp_module._manager is not None

    @pytest.mark.asyncio
    async def test_init_mcp_with_config(self, tmp_path, monkeypatch):
        """Test init_mcp with valid config file."""
        monkeypatch.chdir(tmp_path)
        
        # Create config in one of the search paths
        config_path = tmp_path / ".aru" / "mcp_servers.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        
        # Reset global manager
        import aru.tools.mcp_client as mcp_module
        mcp_module._manager = None
        
        with patch.object(McpSessionManager, 'initialize', new_callable=AsyncMock) as mock_init, \
             patch.object(McpSessionManager, 'get_tools', new_callable=AsyncMock, return_value=[]):
            
            tools = await init_mcp()
            
            mock_init.assert_called_once()
            assert isinstance(tools, list)

    @pytest.mark.asyncio
    async def test_init_mcp_config_priority(self, tmp_path, monkeypatch):
        """Test init_mcp config file search priority."""
        monkeypatch.chdir(tmp_path)
        
        # Create multiple config files
        (tmp_path / ".aru").mkdir()
        (tmp_path / ".aru" / "mcp_servers.json").write_text("{}", encoding="utf-8")
        (tmp_path / "aru.mcp.json").write_text("{}", encoding="utf-8")
        
        # Reset global manager
        import aru.tools.mcp_client as mcp_module
        mcp_module._manager = None
        
        with patch.object(McpSessionManager, 'initialize', new_callable=AsyncMock), \
             patch.object(McpSessionManager, 'get_tools', new_callable=AsyncMock, return_value=[]):
            
            await init_mcp()
            
            # Should use .aru/mcp_servers.json (first in priority)
            assert mcp_module._manager.config_path == ".aru/mcp_servers.json"

    @pytest.mark.asyncio
    async def test_init_mcp_singleton_pattern(self, tmp_path, monkeypatch):
        """Test that init_mcp returns singleton manager."""
        monkeypatch.chdir(tmp_path)
        
        # Reset global manager
        import aru.tools.mcp_client as mcp_module
        mcp_module._manager = None
        
        with patch.object(McpSessionManager, 'initialize', new_callable=AsyncMock), \
             patch.object(McpSessionManager, 'get_tools', new_callable=AsyncMock, return_value=[]):
            
            # First call
            await init_mcp()
            first_manager = mcp_module._manager
            
            # Second call
            await init_mcp()
            second_manager = mcp_module._manager
            
            # Should be the same instance
            assert first_manager is second_manager

    @pytest.mark.asyncio
    async def test_cleanup_mcp_with_manager(self):
        """Test cleanup_mcp when manager exists."""
        import aru.tools.mcp_client as mcp_module
        
        # Create mock manager
        mock_manager = AsyncMock()
        mock_manager.cleanup = AsyncMock()
        mcp_module._manager = mock_manager
        
        await cleanup_mcp()
        
        mock_manager.cleanup.assert_called_once()
        assert mcp_module._manager is None

    @pytest.mark.asyncio
    async def test_cleanup_mcp_no_manager(self):
        """Test cleanup_mcp when no manager exists."""
        import aru.tools.mcp_client as mcp_module
        mcp_module._manager = None
        
        # Should not raise an exception
        await cleanup_mcp()
        
        assert mcp_module._manager is None

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path, monkeypatch):
        """Test complete init -> use -> cleanup lifecycle."""
        monkeypatch.chdir(tmp_path)
        
        # Create config
        config_path = tmp_path / ".aru" / "mcp_servers.json"
        config_path.parent.mkdir()
        config = {
            "mcpServers": {
                "test": {
                    "command": "echo",
                    "args": ["test"]
                }
            }
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        
        # Reset global manager
        import aru.tools.mcp_client as mcp_module
        mcp_module._manager = None
        
        with patch.object(McpSessionManager, '_start_server', new_callable=AsyncMock), \
             patch.object(McpSessionManager, 'get_tools', new_callable=AsyncMock, return_value=[]):
            
            # Initialize
            tools = await init_mcp()
            assert mcp_module._manager is not None
            
            # Cleanup
            await cleanup_mcp()
            assert mcp_module._manager is None