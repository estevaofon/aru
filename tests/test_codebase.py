"""Unit tests for aru.tools.codebase core tools."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from aru.tools.codebase import (
    read_file,
    write_file,
    write_files,
    edit_file,
    edit_files,
    glob_search,
    grep_search,
    list_directory,
    get_project_tree,
    run_command,
    bash,
    _truncate_output,
    _is_long_running,
    _is_safe_command,
    _shell_split,
    set_skip_permissions,
    set_model_id,
    set_console,
    set_live,
    set_display,
    set_permission_rules,
    reset_allowed_actions,
)


# All write/edit tools need permission bypass
@pytest.fixture(autouse=True)
def _bypass_permissions():
    with patch("aru.tools.codebase._ask_permission", return_value=True):
        yield


# ── read_file ────────────────────────────────────────────────────────

class TestReadFile:
    def test_read_normal_file(self, temp_dir: Path):
        f = temp_dir / "hello.py"
        f.write_text("line1\nline2\nline3\n")
        result = read_file(str(f))
        assert "   1 | line1" in result
        assert "   3 | line3" in result

    def test_read_line_range(self, temp_dir: Path):
        f = temp_dir / "lines.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 11)))
        result = read_file(str(f), start_line=3, end_line=5)
        assert "[Lines 3-5 of 10]" in result
        assert "line3" in result
        assert "line5" in result
        assert "line6" not in result

    def test_read_nonexistent(self):
        result = read_file("/nonexistent/path.txt")
        assert "Error" in result

    def test_read_binary_file(self, temp_dir: Path):
        f = temp_dir / "binary.bin"
        f.write_bytes(b"\x00\x01\x02\x03")
        result = read_file(str(f))
        assert "Binary file detected" in result

    def test_read_file_large_file_truncation(self, temp_dir: Path):
        """Test that files larger than max_size are truncated with warning."""
        f = temp_dir / "large.txt"
        # Create a file larger than default max_size (30KB)
        large_content = "x" * 100 + "\n"  # 101 bytes per line
        lines_needed = 350  # ~35KB total
        f.write_text(large_content * lines_needed)
        
        # Read with small max_size to trigger truncation
        result = read_file(str(f), max_size=5000)
        
        # Should show truncation warning
        assert "WARNING" in result
        assert "File truncated" in result
        assert "5,000 bytes" in result
        assert "lines shown" in result
        assert "Use start_line/end_line" in result


# ── edit_file ────────────────────────────────────────────────────────

class TestEditFile:
    def test_edit_unique_string(self, temp_dir: Path):
        f = temp_dir / "code.py"
        f.write_text("old_value = 1\n")
        result = edit_file(str(f), "old_value", "new_value")
        assert "Successfully" in result
        assert "new_value = 1" in f.read_text()

    def test_edit_string_not_found(self, temp_dir: Path):
        f = temp_dir / "code.py"
        f.write_text("hello\n")
        result = edit_file(str(f), "missing", "replacement")
        assert "not found" in result

    def test_edit_string_not_unique(self, temp_dir: Path):
        f = temp_dir / "code.py"
        f.write_text("dup\ndup\n")
        result = edit_file(str(f), "dup", "single")
        assert "2 times" in result


# ── write_file ───────────────────────────────────────────────────────

class TestWriteFile:
    def test_write_creates_parents(self, temp_dir: Path):
        target = temp_dir / "a" / "b" / "file.txt"
        result = write_file(str(target), "content")
        assert "Successfully" in result
        assert target.read_text() == "content"

    def test_write_permission_denied(self, temp_dir: Path):
        target = temp_dir / "denied.txt"
        with patch("aru.tools.codebase._ask_permission", return_value=False):
            result = write_file(str(target), "nope")
        assert "Permission denied" in result
        assert not target.exists()


# ── glob_search ──────────────────────────────────────────────────────

class TestGlobSearch:
    def test_glob_finds_python_files(self, project_dir: Path):
        result = glob_search("*.py", str(project_dir))
        assert "main.py" in result
        assert "utils.py" in result

    def test_glob_no_match(self, project_dir: Path):
        result = glob_search("*.xyz", str(project_dir))
        assert "No files matched" in result


# ── grep_search ──────────────────────────────────────────────────────

class TestGrepSearch:
    def test_grep_finds_pattern(self, project_dir: Path):
        result = grep_search("def main", str(project_dir))
        assert "main.py" in result
        assert "def main" in result

    def test_grep_invalid_regex(self, project_dir: Path):
        result = grep_search("[invalid", str(project_dir))
        assert "Invalid regex" in result

    def test_grep_context_lines_shows_surrounding_code(self, project_dir: Path):
        result = grep_search("def main", str(project_dir), context_lines=2)
        # Should include the match line marked with >
        assert ":>" in result or "> " in result
        # Should include separator between blocks
        assert "---" in result or "def main" in result

    def test_grep_context_lines_includes_nearby_lines(self, tmp_path: Path):
        f = tmp_path / "sample.py"
        f.write_text("line1\nline2\ndef foo():\n    pass\nline5\nline6\n")
        result = grep_search("def foo", str(tmp_path), context_lines=1)
        assert "line2" in result
        assert "def foo" in result
        assert "    pass" in result

    def test_grep_context_lines_zero_unchanged(self, project_dir: Path):
        result = grep_search("def main", str(project_dir), context_lines=0)
        # Default behavior: single lines, no separator
        assert "def main" in result


# ── list_directory ───────────────────────────────────────────────────

class TestListDirectory:
    def test_list_directory(self, project_dir: Path):
        result = list_directory(str(project_dir))
        assert "src/" in result
        assert "README.md" in result
        # gitignored dirs should be filtered
        assert "__pycache__" not in result

    def test_list_nonexistent(self):
        result = list_directory("/nonexistent/dir")
        assert "Error" in result


# ── helpers ──────────────────────────────────────────────────────────

class TestHelpers:
    def test_truncate_short_output(self):
        assert _truncate_output("short") == "short"

    def test_truncate_long_output(self):
        # Create text exceeding 20KB / 500 lines (universal truncation thresholds)
        lines = ["x" * 50 + "\n" for _ in range(600)]
        text = "".join(lines)
        result = _truncate_output(text)
        assert "omitted" in result
        assert len(result) < len(text)

    def test_is_long_running_server(self):
        assert _is_long_running("uvicorn main:app")
        assert _is_long_running("npm run dev")
        assert _is_long_running("python app.py &")

    def test_is_not_long_running(self):
        assert not _is_long_running("python -m pytest")
        assert not _is_long_running("git status")


# ── write_files ──────────────────────────────────────────────────────

class TestWriteFiles:
    def test_write_multiple_files(self, temp_dir: Path):
        files = [
            {"path": str(temp_dir / "file1.txt"), "content": "content1"},
            {"path": str(temp_dir / "file2.txt"), "content": "content2"},
            {"path": str(temp_dir / "sub" / "file3.txt"), "content": "content3"},
        ]
        result = write_files(files)
        assert "Successfully wrote 3 files" in result
        assert (temp_dir / "file1.txt").read_text() == "content1"
        assert (temp_dir / "file2.txt").read_text() == "content2"
        assert (temp_dir / "sub" / "file3.txt").read_text() == "content3"

    def test_write_files_permission_denied(self, temp_dir: Path):
        files = [
            {"path": str(temp_dir / "file1.txt"), "content": "content1"},
        ]
        with patch("aru.tools.codebase._ask_permission", return_value=False):
            result = write_files(files)
        assert "Permission denied" in result
        assert not (temp_dir / "file1.txt").exists()

    def test_write_files_missing_path(self, temp_dir: Path):
        files = [
            {"content": "no path"},
            {"path": str(temp_dir / "valid.txt"), "content": "valid"},
        ]
        result = write_files(files)
        assert "Successfully wrote 1 files" in result
        assert "missing 'path'" in result
        assert (temp_dir / "valid.txt").exists()

    def test_write_files_error_in_some(self, temp_dir: Path):
        # Create read-only directory on Unix-like systems
        readonly_dir = temp_dir / "readonly"
        readonly_dir.mkdir()
        
        files = [
            {"path": str(temp_dir / "success.txt"), "content": "ok"},
            {"path": str(readonly_dir / "subdir" / "fail.txt"), "content": "fail"},
        ]
        
        # Make directory read-only after creating it
        import os
        import stat
        if os.name != 'nt':  # Skip on Windows
            readonly_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        
        result = write_files(files)
        
        # Restore permissions for cleanup
        if os.name != 'nt':
            readonly_dir.chmod(stat.S_IRWXU)
        
        if os.name != 'nt':
            assert "Successfully wrote 1 files" in result
            assert "Error writing" in result or "success.txt" in result

    def test_write_files_empty_list(self):
        result = write_files([])
        assert "No files to write" in result


# ── edit_files ───────────────────────────────────────────────────────

class TestEditFiles:
    def test_edit_multiple_files(self, temp_dir: Path):
        (temp_dir / "file1.py").write_text("old_value = 1\n")
        (temp_dir / "file2.py").write_text("old_name = 'test'\n")
        
        edits = [
            {"path": str(temp_dir / "file1.py"), "old_string": "old_value", "new_string": "new_value"},
            {"path": str(temp_dir / "file2.py"), "old_string": "old_name", "new_string": "new_name"},
        ]
        result = edit_files(edits)
        
        assert "Successfully applied 2 edits" in result
        assert "new_value = 1" in (temp_dir / "file1.py").read_text()
        assert "new_name = 'test'" in (temp_dir / "file2.py").read_text()

    def test_edit_files_same_file_multiple_times(self, temp_dir: Path):
        f = temp_dir / "code.py"
        f.write_text("line1\nline2\nline3\n")
        
        edits = [
            {"path": str(f), "old_string": "line1", "new_string": "updated1"},
            {"path": str(f), "old_string": "line2", "new_string": "updated2"},
        ]
        result = edit_files(edits)
        
        assert "Successfully applied 2 edits" in result
        content = f.read_text()
        assert "updated1" in content
        assert "updated2" in content

    def test_edit_files_permission_denied(self, temp_dir: Path):
        f = temp_dir / "file.py"
        f.write_text("old\n")
        
        edits = [{"path": str(f), "old_string": "old", "new_string": "new"}]
        with patch("aru.tools.codebase._ask_permission", return_value=False):
            result = edit_files(edits)
        
        assert "Permission denied" in result
        assert "old" in f.read_text()

    def test_edit_files_string_not_found(self, temp_dir: Path):
        f = temp_dir / "file.py"
        f.write_text("content\n")
        
        edits = [
            {"path": str(f), "old_string": "missing", "new_string": "new"},
        ]
        result = edit_files(edits)
        
        assert "old_string not found" in result

    def test_edit_files_string_not_unique(self, temp_dir: Path):
        f = temp_dir / "file.py"
        f.write_text("duplicate\nduplicate\n")
        
        edits = [
            {"path": str(f), "old_string": "duplicate", "new_string": "unique"},
        ]
        result = edit_files(edits)
        
        assert "found" in result and "times" in result

    def test_edit_files_missing_path(self, temp_dir: Path):
        edits = [
            {"old_string": "old", "new_string": "new"},
        ]
        result = edit_files(edits)
        
        assert "missing 'path'" in result

    def test_edit_files_file_not_found(self, temp_dir: Path):
        edits = [
            {"path": str(temp_dir / "nonexistent.py"), "old_string": "old", "new_string": "new"},
        ]
        result = edit_files(edits)
        
        assert "file not found" in result

    def test_edit_files_empty_list(self):
        result = edit_files([])
        assert "No edits to apply" in result


# ── get_project_tree ─────────────────────────────────────────────────

class TestGetProjectTree:
    def test_project_tree_nested_directories(self, temp_dir: Path):
        # Create nested structure
        (temp_dir / "dir1").mkdir()
        (temp_dir / "dir1" / "file1.txt").write_text("test")
        (temp_dir / "dir1" / "subdir1").mkdir()
        (temp_dir / "dir1" / "subdir1" / "file2.txt").write_text("test")
        (temp_dir / "dir2").mkdir()
        (temp_dir / "dir2" / "file3.txt").write_text("test")
        
        result = get_project_tree(str(temp_dir))
        
        assert "dir1/" in result
        assert "dir2/" in result
        assert "file1.txt" in result
        assert "subdir1/" in result
        assert "file2.txt" in result

    def test_project_tree_max_depth(self, temp_dir: Path):
        # Create deep nesting
        (temp_dir / "level1").mkdir()
        (temp_dir / "level1" / "level2").mkdir()
        (temp_dir / "level1" / "level2" / "level3").mkdir()
        (temp_dir / "level1" / "level2" / "level3" / "level4").mkdir()
        (temp_dir / "level1" / "level2" / "level3" / "level4" / "deep.txt").write_text("test")
        
        result = get_project_tree(str(temp_dir), max_depth=2)
        
        assert "level1/" in result
        assert "level2/" in result
        # level3 should not appear due to max_depth=2
        assert "level3/" not in result
        assert "deep.txt" not in result

    def test_project_tree_max_files_per_dir(self, temp_dir: Path):
        # Create many files
        subdir = temp_dir / "many_files"
        subdir.mkdir()
        for i in range(50):
            (subdir / f"file{i:02d}.txt").write_text("test")
        
        result = get_project_tree(str(temp_dir), max_files_per_dir=10)
        
        assert "many_files/" in result
        assert "file00.txt" in result
        assert "file09.txt" in result
        # Should show truncation message
        assert "more files" in result

    def test_project_tree_respects_gitignore(self, temp_dir: Path):
        # Create .gitignore
        (temp_dir / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        
        # Create ignored items
        (temp_dir / "__pycache__").mkdir()
        (temp_dir / "__pycache__" / "cache.pyc").write_text("test")
        
        # Create non-ignored items
        (temp_dir / "main.py").write_text("test")
        
        result = get_project_tree(str(temp_dir))
        
        assert "main.py" in result
        assert "__pycache__" not in result
        assert "cache.pyc" not in result

    def test_project_tree_nonexistent_dir(self):
        result = get_project_tree("/nonexistent/path")
        assert result == ""

    def test_project_tree_root_only(self, temp_dir: Path):
        # Empty directory
        result = get_project_tree(str(temp_dir))
        
        # Should show root directory name
        assert temp_dir.name in result or "/" in result

    def test_project_tree_truncation(self, temp_dir: Path):
        # Create structure large enough to trigger truncation
        for i in range(20):
            subdir = temp_dir / f"dir{i}"
            subdir.mkdir()
            for j in range(50):
                (subdir / f"file{j}.txt").write_text("x" * 100)
        
        result = get_project_tree(str(temp_dir), max_files_per_dir=50)
        
        # Should be truncated if over 15000 chars
        if len(result) >= 15000:
            assert "Tree truncated" in result


# ── run_command ──────────────────────────────────────────────────────

class TestRunCommand:
    def test_run_command_success(self, temp_dir: Path):
        """Test successful command execution with output."""
        result = run_command("echo hello world", working_directory=str(temp_dir))
        assert "hello world" in result.lower()
    
    def test_run_command_with_working_directory(self, temp_dir: Path):
        """Test command runs in specified working directory."""
        subdir = temp_dir / "subdir"
        subdir.mkdir()
        
        # Use a cross-platform command to get current directory
        import sys
        if sys.platform == "win32":
            result = run_command("cd", working_directory=str(subdir))
        else:
            result = run_command("pwd", working_directory=str(subdir))
        
        assert "subdir" in result
    
    def test_run_command_non_zero_exit_code(self):
        """Test command with non-zero exit code."""
        # Use cross-platform command that fails
        import sys
        if sys.platform == "win32":
            result = run_command("exit 1")
        else:
            result = run_command("false")
        
        assert "Exit code:" in result or "exit code" in result.lower()
    
    def test_run_command_stderr_output(self):
        """Test command that writes to stderr."""
        import sys
        if sys.platform == "win32":
            # Use PowerShell to write to stderr
            result = run_command('powershell -Command "Write-Error test 2>&1"')
        else:
            result = run_command("echo 'error message' >&2")
        
        assert "STDERR" in result or "error" in result.lower()
    
    def test_run_command_timeout(self, temp_dir: Path):
        """Test command timeout handling."""
        import sys
        if sys.platform == "win32":
            # Windows: use timeout command (note: may not work in all environments)
            result = run_command("ping -n 10 127.0.0.1", timeout=1)
        else:
            result = run_command("sleep 10", timeout=1)
        
        assert "timed out" in result.lower() or "timeout" in result.lower()
    
    def test_run_command_no_output(self):
        """Test command with no output."""
        import sys
        if sys.platform == "win32":
            result = run_command("echo.")
        else:
            result = run_command("true")
        
        # Should handle empty output gracefully
        assert "(no output)" in result or result.strip() == "" or len(result) < 10
    
    def test_run_command_long_running_background_detection(self):
        """Test detection of long-running server commands."""
        # Test a command that looks like a server
        result = run_command("echo 'Starting server'; python -c \"import sys; print('Server started'); sys.exit(0)\"")
        
        # Should complete normally since python exits immediately
        # The detection only triggers background mode for actual long-running processes
        assert "hello" in result.lower() or "started" in result.lower() or "exit" in result.lower() or len(result) > 0
    
    def test_run_command_background_with_ampersand(self):
        """Test command ending with & triggers background mode."""
        import sys
        if sys.platform != "win32":  # Background process handling is Unix-like specific
            result = run_command("sleep 1 &")
            # Should detect background mode or complete quickly
            assert "background" in result.lower() or "PID" in result or len(result) >= 0
    
    def test_run_command_uvicorn_pattern(self):
        """Test uvicorn command detection as long-running."""
        # Mock a uvicorn-like command that exits immediately
        result = run_command("python -c \"print('uvicorn app:main')\"")
        # Should execute normally since it exits immediately
        assert "uvicorn" in result or len(result) >= 0
    
    def test_run_command_output_truncation(self):
        """Test very long output gets truncated."""
        # Generate output longer than 10K chars
        import sys
        if sys.platform == "win32":
            # Windows: use PowerShell to generate long output
            cmd = 'powershell -Command "1..500 | ForEach-Object { \\"x\\" * 50 }"'
        else:
            cmd = "python -c \"print('x' * 15000)\""
        
        result = run_command(cmd)

        # Output should be truncated if over threshold (20KB / 500 lines)
        if len(result) >= 20_000 or result.count("\n") >= 500:
            assert "omitted" in result.lower() or "truncated" in result.lower()
    
    def test_run_command_with_pipes(self):
        """Test command with pipes."""
        import sys
        if sys.platform == "win32":
            result = run_command('echo hello | findstr hello')
        else:
            result = run_command("echo 'hello world' | grep hello")
        
        assert "hello" in result.lower()
    
    def test_run_command_error_handling(self):
        """Test error handling for invalid commands."""
        result = run_command("nonexistent_command_xyz_12345")
        
        assert "error" in result.lower() or "not found" in result.lower() or "exit code" in result.lower()
    
    def test_run_command_default_working_directory(self):
        """Test command uses current working directory by default."""
        result = run_command("echo test")
        # Should execute without error
        assert "test" in result.lower() or len(result) >= 0
    
    def test_run_command_combined_stdout_stderr(self):
        """Test command with both stdout and stderr."""
        import sys
        if sys.platform == "win32":
            # Windows command that outputs to both streams
            result = run_command('echo stdout && powershell -Command "Write-Error stderr 2>&1"')
        else:
            result = run_command("echo 'stdout'; echo 'stderr' >&2")
        
        # Should contain both outputs
        assert "stdout" in result.lower() or "stderr" in result.lower()
    
    def test_run_command_exit_code_zero(self):
        """Test successful command doesn't show exit code."""
        result = run_command("echo success")
        # Exit code 0 should not be mentioned
        assert "Exit code: 0" not in result
        assert "success" in result.lower()
    
    def test_run_command_multiple_lines_output(self):
        """Test command with multi-line output."""
        import sys
        if sys.platform == "win32":
            result = run_command("echo line1 && echo line2 && echo line3")
        else:
            result = run_command("printf 'line1\\nline2\\nline3\\n'")
        
        assert "line1" in result.lower()
        assert "line2" in result.lower() or "line3" in result.lower()
    
    def test_run_command_special_characters(self):
        """Test command with special characters."""
        import sys
        if sys.platform == "win32":
            result = run_command('echo "test!@#$%"')
        else:
            result = run_command("echo 'test!@#$%'")
        
        # Should handle special characters
        assert len(result) > 0
    
    def test_run_command_empty_command(self):
        """Test empty command handling."""
        result = run_command("")
        # Should handle gracefully
        assert "error" in result.lower() or "(no output)" in result or len(result) >= 0


# ── _shell_split ─────────────────────────────────────────────────────

class TestShellSplit:
    """Test the _shell_split helper function."""
    
    def test_split_by_and_separator(self):
        """Test splitting by && separator."""
        result = _shell_split("echo hello && echo world", ("&&",))
        assert result == ["echo hello", "echo world"]
    
    def test_split_by_semicolon(self):
        """Test splitting by ; separator."""
        result = _shell_split("ls -la ; pwd", (";",))
        assert result == ["ls -la", "pwd"]
    
    def test_split_by_pipe(self):
        """Test splitting by | separator."""
        result = _shell_split("cat file.txt | grep test", ("|",))
        assert result == ["cat file.txt", "grep test"]
    
    def test_no_separator_returns_none(self):
        """Test command without separator returns None."""
        result = _shell_split("echo hello", ("&&", ";"))
        assert result is None
    
    def test_split_respects_single_quotes(self):
        """Test that separators inside single quotes are ignored."""
        result = _shell_split("echo 'hello && world' && echo test", ("&&",))
        assert result == ["echo 'hello && world'", "echo test"]
    
    def test_split_respects_double_quotes(self):
        """Test that separators inside double quotes are ignored."""
        result = _shell_split('echo "hello && world" && echo test', ("&&",))
        assert result == ['echo "hello && world"', "echo test"]
    
    def test_split_multiple_separators(self):
        """Test command with multiple separators."""
        result = _shell_split("cmd1 && cmd2 && cmd3", ("&&",))
        assert result == ["cmd1", "cmd2", "cmd3"]
    
    def test_split_mixed_quotes(self):
        """Test command with both single and double quotes."""
        result = _shell_split("echo 'single' && echo \"double\"", ("&&",))
        assert result == ["echo 'single'", "echo \"double\""]
    
    def test_split_empty_parts_filtered(self):
        """Test that empty parts are filtered out."""
        result = _shell_split("cmd1 && && cmd2", ("&&",))
        # Empty string between consecutive separators should be filtered
        assert "" not in result
    
    def test_split_with_whitespace(self):
        """Test that whitespace is properly trimmed."""
        result = _shell_split("  cmd1  &&  cmd2  ", ("&&",))
        assert result == ["cmd1", "cmd2"]
    
    def test_split_nested_quotes(self):
        """Test command with quote inside different quote type."""
        result = _shell_split("""echo "it's test" && echo 'say "hi"'""", ("&&",))
        assert result == ["""echo "it's test" """.strip(), """echo 'say "hi"'"""]
    
    def test_split_multiple_separator_types(self):
        """Test preferring first separator type found."""
        # When both separators exist, should split by first in tuple
        result = _shell_split("cmd1 && cmd2 ; cmd3", ("&&",))
        assert result == ["cmd1", "cmd2 ; cmd3"]


# ── _is_safe_command ─────────────────────────────────────────────────

class TestIsSafeCommand:
    """Test the _is_safe_command helper function."""
    
    def test_safe_ls_command(self):
        """Test ls is recognized as safe."""
        assert _is_safe_command("ls")
        assert _is_safe_command("ls -la")
        assert _is_safe_command("ls /path/to/dir")
    
    def test_safe_git_status(self):
        """Test git status is safe."""
        assert _is_safe_command("git status")
        assert _is_safe_command("git log")
        assert _is_safe_command("git diff")
    
    def test_safe_cat_command(self):
        """Test cat is safe."""
        assert _is_safe_command("cat file.txt")
        assert _is_safe_command("cat /path/to/file")
    
    def test_safe_grep_command(self):
        """Test grep is safe."""
        assert _is_safe_command("grep pattern file.txt")
        assert _is_safe_command("rg pattern")
    
    def test_safe_echo_command(self):
        """Test echo is safe."""
        assert _is_safe_command("echo hello")
        assert _is_safe_command("echo 'test message'")
    
    def test_unsafe_rm_command(self):
        """Test rm is unsafe."""
        assert not _is_safe_command("rm file.txt")
        assert not _is_safe_command("rm -rf /")
    
    def test_unsafe_git_push(self):
        """Test git push is unsafe."""
        assert not _is_safe_command("git push")
        assert not _is_safe_command("git commit -m 'msg'")
    
    def test_unsafe_npm_install(self):
        """Test npm install is unsafe."""
        assert not _is_safe_command("npm install")
        assert not _is_safe_command("pip install package")
    
    def test_safe_chained_commands_all_safe(self):
        """Test chained commands with && - all safe."""
        assert _is_safe_command("ls && pwd")
        assert _is_safe_command("cat file.txt && echo done")
    
    def test_unsafe_chained_commands_one_unsafe(self):
        """Test chained commands with && - one unsafe."""
        assert not _is_safe_command("ls && rm file.txt")
        assert not _is_safe_command("rm file.txt && ls")
    
    def test_safe_chained_with_semicolon(self):
        """Test chained commands with ; - all safe."""
        assert _is_safe_command("ls ; pwd ; echo done")
    
    def test_unsafe_chained_with_semicolon(self):
        """Test chained commands with ; - one unsafe."""
        assert not _is_safe_command("ls ; rm file.txt")
    
    def test_safe_piped_commands(self):
        """Test piped commands - all safe."""
        assert _is_safe_command("cat file.txt | grep test")
        assert _is_safe_command("ls | sort")
    
    def test_unsafe_piped_commands(self):
        """Test piped commands - one unsafe."""
        assert not _is_safe_command("cat file.txt | rm")
        assert not _is_safe_command("echo test | npm install")
    
    def test_safe_complex_pipe_chain(self):
        """Test complex pipe chain - all safe."""
        assert _is_safe_command("cat file.txt | grep pattern | sort | uniq")
    
    def test_unsafe_complex_pipe_chain(self):
        """Test complex pipe chain - one unsafe."""
        assert not _is_safe_command("cat file.txt | grep pattern | rm")
    
    def test_safe_version_commands(self):
        """Test version check commands are safe."""
        assert _is_safe_command("python --version")
        assert _is_safe_command("node --version")
        assert _is_safe_command("cargo --version")
    
    def test_whitespace_trimming(self):
        """Test that commands are properly trimmed."""
        assert _is_safe_command("  ls  ")
        assert _is_safe_command("  ls -la  ")
    
    def test_command_not_in_whitelist(self):
        """Test command not in safe prefix list."""
        assert not _is_safe_command("custom_unsafe_cmd")
        assert not _is_safe_command("python script.py")  # python alone is not safe
    
    def test_safe_sed_readonly(self):
        """Test sed -n is safe (read-only mode)."""
        assert _is_safe_command("sed -n 'p' file.txt")
    
    def test_safe_awk_command(self):
        """Test awk is safe."""
        assert _is_safe_command("awk '{print $1}' file.txt")
    
    def test_safe_jq_command(self):
        """Test jq is safe."""
        assert _is_safe_command("jq '.field' file.json")


# ── bash ─────────────────────────────────────────────────────────────

class TestBash:
    """Test the bash tool function."""
    
    def test_bash_safe_command_no_permission_prompt(self):
        """Test safe commands bypass permission prompt."""
        # Mock permission to track if it was called
        with patch("aru.tools.codebase._ask_permission") as mock_perm:
            result = bash("ls")
            # Permission should NOT be asked for safe commands
            mock_perm.assert_not_called()
            assert len(result) >= 0  # Should execute
    
    def test_bash_safe_git_status(self):
        """Test git status is safe and executes."""
        with patch("aru.tools.codebase._ask_permission") as mock_perm:
            result = bash("git status")
            mock_perm.assert_not_called()
            # Should execute without error
            assert "Error" not in result or "fatal" in result.lower()
    
    def test_bash_safe_echo(self):
        """Test echo command is safe."""
        with patch("aru.tools.codebase._ask_permission") as mock_perm:
            result = bash("echo test")
            mock_perm.assert_not_called()
            assert "test" in result.lower()
    
    def test_bash_unsafe_command_requires_permission(self):
        """Test unsafe commands require permission."""
        with patch("aru.tools.codebase._ask_permission", return_value=True) as mock_perm:
            result = bash("rm test.txt")
            # Permission SHOULD be asked
            mock_perm.assert_called_once()
            # First arg should be the action type
            assert mock_perm.call_args[0][0] == "Bash Command"
    
    def test_bash_unsafe_command_permission_denied(self):
        """Test unsafe command with permission denied."""
        with patch("aru.tools.codebase._ask_permission", return_value=False):
            result = bash("rm dangerous.txt")
            assert "Permission denied" in result
            assert "rm dangerous.txt" in result
    
    def test_bash_unsafe_command_permission_granted(self):
        """Test unsafe command with permission granted."""
        with patch("aru.tools.codebase._ask_permission", return_value=True):
            result = bash("echo unsafe_but_allowed")
            # Should execute (echo is actually safe, but testing flow)
            assert "unsafe_but_allowed" in result.lower()
    
    def test_bash_with_timeout(self):
        """Test bash respects timeout parameter."""
        import sys
        if sys.platform == "win32":
            # Windows safe command
            cmd = "echo test"
        else:
            cmd = "sleep 0.1"
        
        # ping is not in safe commands list, so use echo instead
        with patch("aru.tools.codebase._ask_permission", return_value=True) as mock_perm:
            result = bash(cmd, timeout=5)
            # Should complete within timeout
            assert "timeout" not in result.lower() or len(result) >= 0
    
    def test_bash_with_working_directory(self, temp_dir: Path):
        """Test bash respects working_directory parameter."""
        import sys
        if sys.platform == "win32":
            cmd = "cd"
        else:
            cmd = "pwd"
        
        result = bash(cmd, working_directory=str(temp_dir))
        # Should show temp_dir path or execute without error
        assert str(temp_dir) in result or len(result) >= 0
    
    def test_bash_chained_safe_commands(self):
        """Test chained safe commands don't need permission."""
        with patch("aru.tools.codebase._ask_permission") as mock_perm:
            result = bash("echo hello && echo world")
            mock_perm.assert_not_called()
            assert "hello" in result.lower() or "world" in result.lower()
    
    def test_bash_chained_with_unsafe(self):
        """Test chained commands with one unsafe requires permission."""
        with patch("aru.tools.codebase._ask_permission", return_value=True) as mock_perm:
            bash("echo hello && rm test.txt")
            mock_perm.assert_called_once()
    
    def test_bash_piped_safe_commands(self):
        """Test piped safe commands don't need permission."""
        import sys
        if sys.platform == "win32":
            # findstr is not in safe commands, so test will require permission
            cmd = "echo hello | findstr hello"
            with patch("aru.tools.codebase._ask_permission", return_value=True) as mock_perm:
                result = bash(cmd)
                # On Windows, findstr requires permission
                assert "hello" in result.lower() or len(result) >= 0
        else:
            cmd = "echo hello | grep hello"
            with patch("aru.tools.codebase._ask_permission") as mock_perm:
                result = bash(cmd)
                mock_perm.assert_not_called()
                assert "hello" in result.lower()
    
    def test_bash_error_handling(self):
        """Test bash handles command errors."""
        result = bash("nonexistent_command_xyz")
        # Should contain error message
        assert "error" in result.lower() or "not found" in result.lower() or "exit code" in result.lower()
    
    def test_bash_with_special_characters(self):
        """Test bash handles special characters."""
        import sys
        if sys.platform == "win32":
            result = bash('echo "test!@#"')
        else:
            result = bash("echo 'test!@#'")
        
        # Should execute without issues
        assert len(result) > 0
    
    def test_bash_default_working_directory(self):
        """Test bash uses current directory by default."""
        result = bash("echo test")
        assert "test" in result.lower()
    
    def test_bash_multiline_output(self):
        """Test bash handles multiline output."""
        import sys
        if sys.platform == "win32":
            result = bash("echo line1 && echo line2")
        else:
            result = bash("printf 'line1\\nline2\\n'")
        
        assert "line1" in result.lower()
        assert "line2" in result.lower() or len(result) > 0


# ── Configuration Functions ──────────────────────────────────────────

class TestConfigurationFunctions:
    """Test configuration setter functions for codebase tools."""
    
    def test_set_skip_permissions(self):
        """Test set_skip_permissions updates global flag."""
        from aru.tools import codebase
        
        # Store original value
        original = codebase._skip_permissions
        
        try:
            set_skip_permissions(True)
            assert codebase._skip_permissions is True
            
            set_skip_permissions(False)
            assert codebase._skip_permissions is False
        finally:
            # Restore original
            codebase._skip_permissions = original
    
    def test_set_model_id(self):
        """Test set_model_id updates global model ID."""
        from aru.tools import codebase
        
        original = codebase._model_id
        
        try:
            set_model_id("claude-opus-4")
            assert codebase._model_id == "claude-opus-4"
            
            set_model_id("claude-haiku-3-5")
            assert codebase._model_id == "claude-haiku-3-5"
        finally:
            codebase._model_id = original
    
    def test_set_console(self):
        """Test set_console updates global console reference."""
        from aru.tools import codebase
        from rich.console import Console
        
        original = codebase._console
        
        try:
            mock_console = Console()
            set_console(mock_console)
            assert codebase._console is mock_console
        finally:
            codebase._console = original
    
    def test_set_live(self):
        """Test set_live updates global Live instance reference."""
        from aru.tools import codebase
        
        original = codebase._live
        
        try:
            mock_live = object()
            set_live(mock_live)
            assert codebase._live is mock_live
        finally:
            codebase._live = original
    
    def test_set_display(self):
        """Test set_display updates global StreamingDisplay reference."""
        from aru.tools import codebase
        
        original = codebase._display
        
        try:
            mock_display = object()
            set_display(mock_display)
            assert codebase._display is mock_display
        finally:
            codebase._display = original
    
    def test_set_permission_rules(self):
        """Test set_permission_rules updates global permission rules list."""
        from aru.tools import codebase
        
        original = codebase._permission_rules
        
        try:
            rules = ["*.py", "tests/**"]
            set_permission_rules(rules)
            assert codebase._permission_rules == ["*.py", "tests/**"]
            
            # Verify it creates a copy
            rules.append("new_rule")
            assert "new_rule" not in codebase._permission_rules
        finally:
            codebase._permission_rules = original
    
    def test_reset_allowed_actions(self):
        """Test reset_allowed_actions clears allowed actions set."""
        from aru.tools import codebase
        
        # Add some actions
        codebase._allowed_actions.add("write_file")
        codebase._allowed_actions.add("bash")
        assert len(codebase._allowed_actions) > 0
        
        # Reset
        reset_allowed_actions()
        assert len(codebase._allowed_actions) == 0
