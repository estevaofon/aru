import pytest
from pathlib import Path
from aru.tools.codebase import write_files, glob_search, read_file, grep_search, edit_file, list_directory, set_skip_permissions


def test_write_files(tmp_path):
    '''Test writing multiple files with write_files'''
    file1 = tmp_path / "test1.txt"
    file2 = tmp_path / "subdir/test2.py"

    files = [
        {
            "path": str(file1),
            "content": "Hello from file1"
        },
        {
            "path": str(file2),
            "content": "print('Hello from file2')"
        }
    ]

    set_skip_permissions(True)
    try:
        write_files(files)
    finally:
        set_skip_permissions(False)

    assert file1.exists()
    assert file2.exists()
    assert file1.read_text() == "Hello from file1"
    assert file2.read_text() == "print('Hello from file2')"


def test_glob_search(temp_dir):
    """Test glob_search returns correct matches for given patterns."""
    # Create files with known names/extensions
    (temp_dir / "main.py").write_text("# main")
    (temp_dir / "utils.py").write_text("# utils")
    (temp_dir / "README.md").write_text("# readme")
    sub = temp_dir / "src"
    sub.mkdir()
    (sub / "app.py").write_text("# app")
    (sub / "config.json").write_text("{}")

    # **/*.py should match all .py files recursively
    result = glob_search("**/*.py", directory=str(temp_dir))
    assert "main.py" in result or any("main.py" in r for r in result.splitlines())
    assert "utils.py" in result or any("utils.py" in r for r in result.splitlines())
    assert any("app.py" in r for r in result.splitlines())
    # .md files should not appear in **/*.py results
    assert "README.md" not in result

    # *.md should match only top-level markdown files
    result_md = glob_search("*.md", directory=str(temp_dir))
    assert "README.md" in result_md or any("README.md" in r for r in result_md.splitlines())
    assert "main.py" not in result_md

    # Pattern with no matches returns appropriate message
    result_none = glob_search("**/*.ts", directory=str(temp_dir))
    assert "No files matched" in result_none


def test_read_file_full(tmp_path):
    """Test that read_file returns numbered lines with [Lines 1-N of N] header."""
    content = "alpha\nbeta\ngamma\n"
    f = tmp_path / "sample.txt"
    f.write_text(content)

    result = read_file(str(f), start_line=1, end_line=0)

    lines = content.splitlines()
    n = len(lines)
    assert f"[Lines 1-{n} of {n}]" in result
    for i, line_text in enumerate(lines, start=1):
        assert f"{i:4d} | {line_text}" in result


def test_read_file_line_range(tmp_path):
    """Test that read_file with start_line=2, end_line=4 returns only those lines."""
    content = "line1\nline2\nline3\nline4\nline5\n"
    f = tmp_path / "multiline.txt"
    f.write_text(content)

    result = read_file(str(f), start_line=2, end_line=4)

    assert "line2" in result
    assert "line3" in result
    assert "line4" in result
    assert "line1" not in result
    assert "line5" not in result


def test_read_file_not_found(tmp_path):
    nonexistent = tmp_path / "does_not_exist.txt"
    result = read_file(str(nonexistent))
    assert "error" in result.lower() or "not found" in result.lower() or "no such" in result.lower()


def test_read_file_binary_detection(tmp_path):
    """Test that read_file detects binary files via null byte in first 1KB."""
    binary_file = tmp_path / "data.bin"
    binary_file.write_bytes(b"some text\x00more data")

    result = read_file(str(binary_file))

    assert "binary" in result.lower()


def test_read_file_truncation(tmp_path):
    """Test that read_file returns first chunk + outline when file exceeds max_size bytes."""
    large_file = tmp_path / "large.txt"
    max_size = 500
    # Write content larger than max_size (multiple lines so chunking works)
    large_file.write_text("\n".join(f"line {i}" for i in range(200)))

    result = read_file(str(large_file), max_size=max_size)

    assert "[Showing lines" in result
    assert "Remaining definitions" in result
    assert "To read more:" in result


def test_grep_search_with_context_lines(temp_dir):
    """Test that grep_search returns correct context lines when context_lines=2."""
    content = (
        "line one\n"
        "line two\n"
        "line three\n"
        "TARGET match here\n"
        "line five\n"
        "line six\n"
        "line seven\n"
    )
    target_file = temp_dir / "sample.txt"
    target_file.write_text(content)

    result = grep_search("TARGET", directory=str(temp_dir), context_lines=2)

    # The matched line should be marked with ">"
    assert "> TARGET match here" in result

    # Two lines before the match should appear
    assert "line two" in result
    assert "line three" in result

    # Two lines after the match should appear
    assert "line five" in result
    assert "line six" in result

    # Lines outside the context window should not appear
    assert "line one" not in result
    assert "line seven" not in result


def test_list_directory(temp_dir):
    """Test list_directory returns known files/subdirs and excludes .git hidden dir."""
    (temp_dir / "README.md").write_text("# readme")
    (temp_dir / "main.py").write_text("# main")
    sub = temp_dir / "src"
    sub.mkdir()
    (sub / "app.py").write_text("# app")
    hidden = temp_dir / ".git"
    hidden.mkdir()

    result = list_directory(str(temp_dir))

    assert "README.md" in result
    assert "main.py" in result
    assert "src/" in result
    assert ".git" not in result


def test_edit_file_basic(tmp_path):
    """Test that edit_file replaces a unique string and writes the updated content."""
    f = tmp_path / "greet.py"
    f.write_text("def hello():\n    return 'world'\n")

    set_skip_permissions(True)
    try:
        result = edit_file(str(f), "return 'world'", "return 'earth'")
    finally:
        set_skip_permissions(False)

    assert "Successfully edited" in result
    assert f.read_text() == "def hello():\n    return 'earth'\n"


def test_edit_file_search_replace(tmp_path):
    """Test edit_file with a multi-line search/replace block on a temp file."""
    f = tmp_path / "config.py"
    original = (
        "DB_HOST = 'localhost'\n"
        "DB_PORT = 5432\n"
        "DB_NAME = 'mydb'\n"
        "DEBUG = True\n"
    )
    f.write_text(original)

    set_skip_permissions(True)
    try:
        result = edit_file(
            str(f),
            "DB_HOST = 'localhost'\nDB_PORT = 5432",
            "DB_HOST = 'production.example.com'\nDB_PORT = 5433",
        )
    finally:
        set_skip_permissions(False)

    assert "Successfully edited" in result
    updated = f.read_text()
    assert "DB_HOST = 'production.example.com'" in updated
    assert "DB_PORT = 5433" in updated
    assert "DB_NAME = 'mydb'" in updated
    assert "DEBUG = True" in updated
    assert "localhost" not in updated