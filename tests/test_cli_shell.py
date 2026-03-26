"""Tests for shell command execution in CLI."""

import subprocess
from unittest.mock import MagicMock, Mock, patch

import pytest
from rich.console import Console

from aru.cli import run_shell


class TestRunShell:
    """Test cases for run_shell function."""

    @pytest.fixture
    def mock_console(self):
        """Mock console for testing."""
        with patch("aru.cli.console") as console:
            yield console

    def test_run_shell_success(self, mock_console):
        """Test successful shell command execution."""
        mock_process = Mock()
        mock_process.stdout = iter(["Hello, World!\n", "Command completed.\n"])
        mock_process.wait.return_value = None
        mock_process.returncode = 0
        
        with patch("subprocess.Popen", return_value=mock_process) as mock_popen:
            run_shell("echo 'Hello, World!' && echo 'Command completed.'")
            
            # Verify Popen called with correct args
            mock_popen.assert_called_once_with(
                "echo 'Hello, World!' && echo 'Command completed.'",
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=mock_popen.call_args[1]["cwd"],  # Use actual cwd
                bufsize=1
            )
            
            # Verify output printed
            assert mock_console.print.call_count >= 3  # Panel + lines + empty line
            
    def test_run_shell_non_zero_exit(self, mock_console):
        """Test shell command with non-zero exit code."""
        mock_process = Mock()
        mock_process.stdout = iter(["Error: Command failed\n"])
        mock_process.wait.return_value = None
        mock_process.returncode = 1
        
        with patch("subprocess.Popen", return_value=mock_process) as mock_popen:
            run_shell("exit 1")
            
            # Verify exit code printed
            mock_console.print.assert_any_call("[red]Exit code: 1[/red]")
            
    def test_run_shell_keyboard_interrupt(self, mock_console):
        """Test handling of keyboard interrupt during command execution."""
        mock_process = Mock()
        mock_process.stdout = iter(["Starting long operation...\n"])
        mock_process.wait.side_effect = KeyboardInterrupt()
        
        with patch("subprocess.Popen", return_value=mock_process) as mock_popen:
            run_shell("sleep 100")
            
            # Verify process was killed
            mock_process.kill.assert_called_once()
            
            # Verify interrupt message printed
            mock_console.print.assert_any_call("\n[yellow]Interrupted.[/yellow]")
            
    def test_run_shell_exception(self, mock_console):
        """Test handling of general exception during command execution."""
        with patch("subprocess.Popen", side_effect=OSError("Command not found")):
            run_shell("nonexistent_command")
            
            # Verify error message printed
            mock_console.print.assert_any_call("[red]Error: Command not found[/red]")
            
    def test_run_shell_streaming_output(self, mock_console):
        """Test that output is streamed line by line."""
        output_lines = ["Line 1\n", "Line 2\n", "Line 3\n"]
        mock_process = Mock()
        mock_process.stdout = iter(output_lines)
        mock_process.wait.return_value = None
        mock_process.returncode = 0
        
        with patch("subprocess.Popen", return_value=mock_process):
            run_shell("cat file.txt")
            
            # Verify each line was printed separately
            from rich.text import Text
            for line in output_lines:
                mock_console.print.assert_any_call(Text(line.rstrip()))
                
    def test_run_shell_empty_output(self, mock_console):
        """Test command with no output."""
        mock_process = Mock()
        mock_process.stdout = iter([])
        mock_process.wait.return_value = None
        mock_process.returncode = 0
        
        with patch("subprocess.Popen", return_value=mock_process):
            run_shell("true")
            
            # Should still print panel and empty line
            assert mock_console.print.call_count >= 2
            
    def test_run_shell_with_special_characters(self, mock_console):
        """Test command with special characters requiring escape."""
        error_msg = "Error: Path </etc/passwd> not found"
        
        with patch("subprocess.Popen", side_effect=Exception(error_msg)):
            run_shell("cat /etc/passwd")
            
            # Verify error was printed (Rich escapes markup automatically)
            # The actual call will have escaped the < and > characters
            calls = [str(call) for call in mock_console.print.call_args_list]
            error_printed = any("[red]Error:" in str(call) for call in calls)
            assert error_printed, f"Error message not found in calls: {calls}"


class TestAskYesNo:
    """Test cases for ask_yes_no function."""
    
    def test_ask_yes_no_yes_variations(self):
        """Test different yes responses."""
        from aru.cli import ask_yes_no
        
        yes_responses = ["y", "Y", "yes", "YES", "Yes", "s", "S", "sim", "SIM"]
        
        for response in yes_responses:
            with patch("aru.cli.console.input", return_value=response):
                assert ask_yes_no("Continue?") is True
                
    def test_ask_yes_no_no_variations(self):
        """Test different no responses."""
        from aru.cli import ask_yes_no
        
        no_responses = ["n", "N", "no", "NO", "No", "nao", "não", "anything", ""]
        
        for response in no_responses:
            with patch("aru.cli.console.input", return_value=response):
                assert ask_yes_no("Continue?") is False
                
    def test_ask_yes_no_keyboard_interrupt(self):
        """Test handling of keyboard interrupt."""
        from aru.cli import ask_yes_no
        
        with patch("aru.cli.console.input", side_effect=KeyboardInterrupt()):
            assert ask_yes_no("Continue?") is False
            
    def test_ask_yes_no_eof_error(self):
        """Test handling of EOF error."""
        from aru.cli import ask_yes_no
        
        with patch("aru.cli.console.input", side_effect=EOFError()):
            assert ask_yes_no("Continue?") is False
            
    def test_ask_yes_no_whitespace_handling(self):
        """Test that whitespace is stripped from response."""
        from aru.cli import ask_yes_no
        
        with patch("aru.cli.console.input", return_value="  y  "):
            assert ask_yes_no("Continue?") is True
            
        with patch("aru.cli.console.input", return_value="  n  "):
            assert ask_yes_no("Continue?") is False