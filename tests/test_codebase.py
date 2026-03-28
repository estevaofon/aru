import pytest
from pathlib import Path
from aru.tools.codebase import (
    write_file, write_files, glob_search, read_file, grep_search,
    edit_file, edit_files, list_directory, set_skip_permissions,
    get_project_tree, _is_safe_command, _shell_split, _is_long_running,
    _html_to_text, clear_read_cache, set_on_file_mutation,
    reset_allowed_actions, _read_cache, _allowed_actions,
)


def test_write_file_creates_file(tmp_path):
    '''Test that write_file creates a new file with correct content.'''
    target = tmp_path / "newfile.txt"

    set_skip_permissions(True)
    try:
        result = write_file(str(target), "hello world")
    finally:
        set_skip_permissions(False)

    assert target.exists()
    assert target.read_text() == "hello world"
    assert "successfully" in result.lower() or "wrote" in result.lower()


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


# ── Group 1: Pure Logic Functions ──────────────────────────────────


class TestShellSplit:
    def test_basic_and(self):
        result = _shell_split("ls && echo hello", ("&&",))
        assert result == ["ls", "echo hello"]

    def test_semicolon(self):
        result = _shell_split("cd /tmp; ls", (";",))
        assert result == ["cd /tmp", "ls"]

    def test_pipe(self):
        result = _shell_split("cat file | grep foo", ("|",))
        assert result == ["cat file", "grep foo"]

    def test_no_separator_returns_none(self):
        result = _shell_split("ls -la", ("&&",))
        assert result is None

    def test_quoted_separator_not_split(self):
        result = _shell_split('echo "a && b"', ("&&",))
        assert result is None


class TestIsSafeCommand:
    def test_safe_prefixes(self):
        for cmd in ["ls", "git status", "grep foo", "cat file.txt", "git log --oneline"]:
            assert _is_safe_command(cmd) is True, f"{cmd} should be safe"

    def test_unsafe_commands(self):
        for cmd in ["rm -rf /", "pip install foo", "python script.py"]:
            assert _is_safe_command(cmd) is False, f"{cmd} should be unsafe"

    def test_chained_all_safe(self):
        assert _is_safe_command("ls && git status") is True

    def test_chained_mixed(self):
        assert _is_safe_command("ls && rm foo") is False

    def test_piped_all_safe(self):
        assert _is_safe_command("cat file | grep foo") is True

    def test_piped_mixed(self):
        assert _is_safe_command("cat file | python") is False


class TestIsLongRunning:
    def test_background_ampersand(self):
        assert _is_long_running("sleep 100 &") is True

    def test_server_patterns(self):
        for cmd in ["uvicorn app:main", "npm start", "flask run", "docker compose up"]:
            assert _is_long_running(cmd) is True, f"{cmd} should be long-running"

    def test_normal_commands(self):
        for cmd in ["ls", "git status", "python script.py"]:
            assert _is_long_running(cmd) is False, f"{cmd} should not be long-running"


class TestHtmlToText:
    def test_basic_paragraphs(self):
        result = _html_to_text("<p>Hello</p><p>World</p>")
        assert "Hello" in result
        assert "World" in result

    def test_strips_scripts(self):
        result = _html_to_text("<div>Visible</div><script>evil()</script>")
        assert "Visible" in result
        assert "evil" not in result

    def test_strips_style(self):
        result = _html_to_text("<h1>Title</h1><style>.cls{color:red}</style><p>Body</p>")
        assert "Title" in result
        assert "Body" in result
        assert "color" not in result

    def test_empty_input(self):
        assert _html_to_text("") == ""


# ── Group 2: get_project_tree ──────────────────────────────────────


class TestGetProjectTree:
    def test_basic_tree(self, tmp_path):
        (tmp_path / "README.md").write_text("# readme")
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.py").write_text("# main")

        result = get_project_tree(str(tmp_path))
        assert "src/" in result or "src" in result
        assert "main.py" in result
        assert "README.md" in result

    def test_max_depth(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "deep.txt").write_text("deep")

        result = get_project_tree(str(tmp_path), max_depth=2)
        # Level 3+ should not appear
        assert "deep.txt" not in result

    def test_max_files_per_dir(self, tmp_path):
        for i in range(35):
            (tmp_path / f"file_{i:02d}.txt").write_text(f"content {i}")

        result = get_project_tree(str(tmp_path), max_files_per_dir=10)
        assert "more files" in result

    def test_nonexistent_path(self, tmp_path):
        result = get_project_tree(str(tmp_path / "does_not_exist"))
        assert result == ""


# ── Group 3: edit_file Error Cases + edit_files ────────────────────


class TestEditFileErrors:
    def test_old_string_not_found(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello world")

        set_skip_permissions(True)
        try:
            result = edit_file(str(f), "NONEXISTENT", "replacement")
        finally:
            set_skip_permissions(False)

        assert "not found" in result.lower()

    def test_old_string_multiple_occurrences(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("foo bar foo baz foo")

        set_skip_permissions(True)
        try:
            result = edit_file(str(f), "foo", "qux")
        finally:
            set_skip_permissions(False)

        assert "3 times" in result

    def test_file_not_found(self, tmp_path):
        set_skip_permissions(True)
        try:
            result = edit_file(str(tmp_path / "nonexistent.py"), "old", "new")
        finally:
            set_skip_permissions(False)

        assert "not found" in result.lower()


class TestEditFiles:
    def test_basic_two_files(self, tmp_path):
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("alpha = 1")
        f2.write_text("beta = 2")

        edits = [
            {"path": str(f1), "old_string": "alpha = 1", "new_string": "alpha = 10"},
            {"path": str(f2), "old_string": "beta = 2", "new_string": "beta = 20"},
        ]

        set_skip_permissions(True)
        try:
            result = edit_files(edits)
        finally:
            set_skip_permissions(False)

        assert "Successfully" in result
        assert f1.read_text() == "alpha = 10"
        assert f2.read_text() == "beta = 20"

    def test_same_file_multiple_edits(self, tmp_path):
        f = tmp_path / "config.py"
        f.write_text("HOST = 'localhost'\nPORT = 3000\n")

        edits = [
            {"path": str(f), "old_string": "HOST = 'localhost'", "new_string": "HOST = 'prod.example.com'"},
            {"path": str(f), "old_string": "PORT = 3000", "new_string": "PORT = 8080"},
        ]

        set_skip_permissions(True)
        try:
            result = edit_files(edits)
        finally:
            set_skip_permissions(False)

        assert "Successfully" in result
        content = f.read_text()
        assert "HOST = 'prod.example.com'" in content
        assert "PORT = 8080" in content

    def test_missing_old_string_partial_success(self, tmp_path):
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("alpha = 1")
        f2.write_text("beta = 2")

        edits = [
            {"path": str(f1), "old_string": "alpha = 1", "new_string": "alpha = 10"},
            {"path": str(f2), "old_string": "NONEXISTENT", "new_string": "whatever"},
        ]

        set_skip_permissions(True)
        try:
            result = edit_files(edits)
        finally:
            set_skip_permissions(False)

        assert "not found" in result.lower()
        assert f1.read_text() == "alpha = 10"

    def test_missing_path_key(self):
        edits = [{"old_string": "foo", "new_string": "bar"}]

        set_skip_permissions(True)
        try:
            result = edit_files(edits)
        finally:
            set_skip_permissions(False)

        assert "missing" in result.lower() or "error" in result.lower()


# ── Group 4: Cache and Callbacks ───────────────────────────────────


class TestCacheAndCallbacks:
    def test_read_file_cache_hit(self, tmp_path):
        f = tmp_path / "cached.txt"
        f.write_text("line1\nline2\nline3\nline4\n")

        try:
            first = read_file(str(f), start_line=1, end_line=3)
            assert "[cached]" not in first

            second = read_file(str(f), start_line=1, end_line=3)
            assert "[cached]" in second
        finally:
            clear_read_cache()

    def test_clear_read_cache(self, tmp_path):
        f = tmp_path / "cached2.txt"
        f.write_text("content\n")

        try:
            read_file(str(f), start_line=1, end_line=1)
            assert len(_read_cache) > 0

            clear_read_cache()
            assert len(_read_cache) == 0
        finally:
            clear_read_cache()

    def test_set_on_file_mutation_callback(self, tmp_path):
        calls = []

        def on_mutation():
            calls.append(True)

        set_on_file_mutation(on_mutation)
        try:
            target = tmp_path / "mutated.txt"
            set_skip_permissions(True)
            try:
                write_file(str(target), "content")
            finally:
                set_skip_permissions(False)

            assert len(calls) > 0, "Mutation callback should have been invoked"
        finally:
            set_on_file_mutation(None)

    def test_reset_allowed_actions(self):
        _allowed_actions.add("test_action")
        assert "test_action" in _allowed_actions

        reset_allowed_actions()
        assert len(_allowed_actions) == 0