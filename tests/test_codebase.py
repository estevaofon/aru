"""Unit tests for arc.tools.codebase core tools."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from arc.tools.codebase import (
    read_file,
    write_file,
    edit_file,
    glob_search,
    grep_search,
    list_directory,
    _truncate_output,
    _is_long_running,
)


# All write/edit tools need permission bypass
@pytest.fixture(autouse=True)
def _bypass_permissions():
    with patch("arc.tools.codebase._ask_permission", return_value=True):
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
        with patch("arc.tools.codebase._ask_permission", return_value=False):
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
        text = "x" * 20_000
        result = _truncate_output(text)
        assert "truncated" in result
        assert len(result) < len(text)

    def test_is_long_running_server(self):
        assert _is_long_running("uvicorn main:app")
        assert _is_long_running("npm run dev")
        assert _is_long_running("python app.py &")

    def test_is_not_long_running(self):
        assert not _is_long_running("python -m pytest")
        assert not _is_long_running("git status")
