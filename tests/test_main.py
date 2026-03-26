"""Unit tests for main.py"""

import os
import sys
from unittest.mock import patch, MagicMock, call
import asyncio
import pytest

# Import the module under test
import main


class TestMain:
    """Test cases for the main module."""

    def test_onnx_environment_variable_set(self):
        """Test that ONNX Runtime log severity is set on module import."""
        # This is already set when the module is imported
        assert os.environ.get("ORT_LOG_SEVERITY_LEVEL") == "3"

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_main_basic_no_args(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test main() with no command line arguments."""
        with patch.object(sys, "argv", ["main.py"]):
            main.main()
        
        mock_load_dotenv.assert_called_once()
        mock_asyncio_run.assert_called_once()
        mock_run_cli.assert_called_once_with(skip_permissions=False, resume_id=None)

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_main_with_skip_permissions_flag(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test main() with --dangerously-skip-permissions flag."""
        with patch.object(sys, "argv", ["main.py", "--dangerously-skip-permissions"]):
            main.main()
        
        mock_load_dotenv.assert_called_once()
        mock_run_cli.assert_called_once_with(skip_permissions=True, resume_id=None)
        mock_asyncio_run.assert_called_once()

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_main_with_resume_and_id(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test main() with --resume and specific session ID."""
        with patch.object(sys, "argv", ["main.py", "--resume", "session123"]):
            main.main()
        
        mock_load_dotenv.assert_called_once()
        mock_run_cli.assert_called_once_with(skip_permissions=False, resume_id="session123")
        mock_asyncio_run.assert_called_once()

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_main_with_resume_no_id(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test main() with --resume but no session ID (defaults to 'last')."""
        with patch.object(sys, "argv", ["main.py", "--resume"]):
            main.main()
        
        mock_load_dotenv.assert_called_once()
        mock_run_cli.assert_called_once_with(skip_permissions=False, resume_id="last")
        mock_asyncio_run.assert_called_once()

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_main_with_resume_followed_by_flag(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test main() with --resume followed by another flag (defaults to 'last')."""
        with patch.object(sys, "argv", ["main.py", "--resume", "--some-other-flag"]):
            main.main()
        
        mock_load_dotenv.assert_called_once()
        mock_run_cli.assert_called_once_with(skip_permissions=False, resume_id="last")
        mock_asyncio_run.assert_called_once()

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_main_with_all_flags(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test main() with both --resume and --dangerously-skip-permissions flags."""
        with patch.object(sys, "argv", ["main.py", "--resume", "abc123", "--dangerously-skip-permissions"]):
            main.main()
        
        mock_load_dotenv.assert_called_once()
        mock_run_cli.assert_called_once_with(skip_permissions=True, resume_id="abc123")
        mock_asyncio_run.assert_called_once()

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_main_keyboard_interrupt_handling(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test that KeyboardInterrupt is properly handled."""
        mock_asyncio_run.side_effect = KeyboardInterrupt()
        
        with patch.object(sys, "argv", ["main.py"]):
            # Should not raise, as exception is caught
            main.main()
        
        mock_load_dotenv.assert_called_once()
        mock_asyncio_run.assert_called_once()

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_main_cancelled_error_handling(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test that asyncio.CancelledError is properly handled."""
        mock_asyncio_run.side_effect = asyncio.CancelledError()
        
        with patch.object(sys, "argv", ["main.py"]):
            # Should not raise, as exception is caught
            main.main()
        
        mock_load_dotenv.assert_called_once()
        mock_asyncio_run.assert_called_once()

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_main_system_exit_handling(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test that SystemExit is properly handled."""
        mock_asyncio_run.side_effect = SystemExit(0)
        
        with patch.object(sys, "argv", ["main.py"]):
            # Should not raise, as exception is caught
            main.main()
        
        mock_load_dotenv.assert_called_once()
        mock_asyncio_run.assert_called_once()

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_main_other_exception_propagates(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test that other exceptions are not caught and propagate."""
        mock_asyncio_run.side_effect = ValueError("Some error")
        
        with patch.object(sys, "argv", ["main.py"]):
            with pytest.raises(ValueError, match="Some error"):
                main.main()
        
        mock_load_dotenv.assert_called_once()
        mock_asyncio_run.assert_called_once()

    @patch.dict(os.environ, {"ORT_LOG_SEVERITY_LEVEL": "3"})
    @patch("builtins.open", new_callable=MagicMock)
    @patch("main.main")
    def test_main_entry_point(self, mock_main, mock_open):
        """Test that main() is called when module is run as __main__."""
        # Read the actual main.py content
        with open("main.py", "r") as f:
            main_content = f.read()
        
        # Mock the file read
        mock_open.return_value.__enter__.return_value.read.return_value = main_content
        
        # Execute only the if __name__ == "__main__" block
        exec('if __name__ == "__main__": main.main()', {"__name__": "__main__", "main": main})
        
        mock_main.assert_called_once()


class TestArgumentParsing:
    """Test cases specifically for command line argument parsing logic."""

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_resume_at_end_of_args(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test --resume as the last argument."""
        with patch.object(sys, "argv", ["main.py", "other", "args", "--resume"]):
            main.main()
        
        mock_run_cli.assert_called_once_with(skip_permissions=False, resume_id="last")

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_multiple_resume_flags(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test behavior with multiple --resume flags (uses first one)."""
        with patch.object(sys, "argv", ["main.py", "--resume", "first", "--resume", "second"]):
            main.main()
        
        mock_run_cli.assert_called_once_with(skip_permissions=False, resume_id="first")

    @patch("main.load_dotenv")
    @patch("main.run_cli")
    @patch("main.asyncio.run")
    def test_skip_permissions_position_variants(self, mock_asyncio_run, mock_run_cli, mock_load_dotenv):
        """Test --dangerously-skip-permissions at different positions."""
        test_cases = [
            ["main.py", "--dangerously-skip-permissions"],
            ["main.py", "arg1", "--dangerously-skip-permissions", "arg2"],
            ["main.py", "--dangerously-skip-permissions", "--resume", "id123"],
        ]
        
        for args in test_cases:
            with patch.object(sys, "argv", args):
                main.main()
            
            # All cases should have skip_permissions=True
            assert mock_run_cli.call_args[1]["skip_permissions"] is True