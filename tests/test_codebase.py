import pytest
from pathlib import Path
from aru.tools.codebase import (
    write_file, write_files, glob_search, read_file, grep_search,
    edit_file, edit_files, list_directory,
    get_project_tree, _is_long_running,
    _html_to_text, clear_read_cache,
    read_file_smart, _format_diff,
    resolve_tools, TOOL_REGISTRY, GENERAL_TOOLS,
    delegate_task, set_custom_agents,
)
from aru.permissions import (
    set_skip_permissions, get_skip_permissions, reset_session,
    _shell_split, resolve_permission,
)
from aru.runtime import get_ctx


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


class TestBashPermissionResolve:
    """Tests for bash permission resolution (replaces TestIsSafeCommand)."""

    def setup_method(self):
        get_ctx().skip_permissions = False

    def test_safe_prefixes(self):
        for cmd in ["ls", "git status", "grep foo", "cat file.txt", "git log --oneline"]:
            action, _ = resolve_permission("bash", cmd)
            assert action == "allow", f"{cmd} should be allowed"

    def test_unsafe_commands(self):
        for cmd in ["rm -rf /", "pip install foo"]:
            action, _ = resolve_permission("bash", cmd)
            assert action == "ask", f"{cmd} should require asking"

    def test_chained_all_safe(self):
        action, _ = resolve_permission("bash", "ls && git status")
        assert action == "allow"

    def test_chained_mixed(self):
        action, _ = resolve_permission("bash", "ls && rm foo")
        assert action == "ask"

    def test_piped_all_safe(self):
        action, _ = resolve_permission("bash", "cat file | grep foo")
        assert action == "allow"

    def test_piped_mixed(self):
        action, _ = resolve_permission("bash", "cat file | python")
        assert action == "ask"


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

    def test_edit_files_multiple_edits(self, tmp_path):
        """Test edit_files applying multiple search/replace edits across two temp files."""
        f1 = tmp_path / "module_a.py"
        f2 = tmp_path / "module_b.py"
        f1.write_text(
            "import os\n"
            "import sys\n"
            "\n"
            "def greet(name):\n"
            "    return f'Hello, {name}!'\n"
        )
        f2.write_text(
            "DATABASE_URL = 'sqlite:///dev.db'\n"
            "TIMEOUT = 30\n"
            "RETRIES = 3\n"
        )

        edits = [
            {"path": str(f1), "old_string": "import os\nimport sys", "new_string": "import os\nimport sys\nimport logging"},
            {"path": str(f1), "old_string": "return f'Hello, {name}!'", "new_string": "return f'Hi, {name}!'"},
            {"path": str(f2), "old_string": "DATABASE_URL = 'sqlite:///dev.db'", "new_string": "DATABASE_URL = 'postgresql:///prod.db'"},
            {"path": str(f2), "old_string": "TIMEOUT = 30", "new_string": "TIMEOUT = 60"},
        ]

        set_skip_permissions(True)
        try:
            result = edit_files(edits)
        finally:
            set_skip_permissions(False)

        assert "Successfully" in result

        content_a = f1.read_text()
        assert "import logging" in content_a
        assert "return f'Hi, {name}!'" in content_a
        assert "return f'Hello, {name}!'" not in content_a

        content_b = f2.read_text()
        assert "DATABASE_URL = 'postgresql:///prod.db'" in content_b
        assert "TIMEOUT = 60" in content_b
        assert "RETRIES = 3" in content_b
        assert "sqlite" not in content_b
        assert "TIMEOUT = 30" not in content_b

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
            assert len(get_ctx().read_cache) > 0

            clear_read_cache()
            assert len(get_ctx().read_cache) == 0
        finally:
            clear_read_cache()

    def test_set_on_file_mutation_callback(self, tmp_path):
        calls = []

        def on_mutation():
            calls.append(True)

        ctx = get_ctx()
        ctx.on_file_mutation = on_mutation
        try:
            target = tmp_path / "mutated.txt"
            write_file(str(target), "content")

            assert len(calls) > 0, "Mutation callback should have been invoked"
        finally:
            ctx.on_file_mutation = None

    def test_reset_session(self):
        ctx = get_ctx()
        ctx.session_allowed.add(("edit", "*"))
        assert ("edit", "*") in ctx.session_allowed

        reset_session()
        assert len(ctx.session_allowed) == 0


@pytest.mark.asyncio
async def test_read_file_smart_below_threshold(tmp_path):
    """Test that read_file_smart returns raw content when file is small (≤ 3_000 chars)."""
    f = tmp_path / "small.py"
    # Content well under the 3_000-char threshold
    f.write_text("def add(a, b):\n    return a + b\n")

    result = await read_file_smart(str(f), query="What does this file do?")

    # Should return raw content, not a model-generated answer
    assert "def add(a, b):" in result
    assert "return a + b" in result


class TestSkipPermissions:
    """Tests for set_skip_permissions / get_skip_permissions."""

    def test_default_is_false(self):
        """Initially skip_permissions is False."""
        set_skip_permissions(False)
        assert get_skip_permissions() is False

    def test_set_true_and_read_back(self):
        """Setting to True is reflected immediately by get_skip_permissions."""
        original = get_skip_permissions()
        try:
            set_skip_permissions(True)
            assert get_skip_permissions() is True
        finally:
            set_skip_permissions(original)

    def test_set_false_and_read_back(self):
        """Setting back to False is also reflected immediately."""
        set_skip_permissions(True)
        try:
            set_skip_permissions(False)
            assert get_skip_permissions() is False
        finally:
            set_skip_permissions(False)


class TestFormatDiff:
    """Tests for _format_diff — unified diff rendering of old/new strings."""

    def test_multiline_deletion(self):
        """Multiline old_string produces a red '- ' line per original line."""
        old = "line1\nline2\nline3"
        group = _format_diff(old, "")

        rendered = "\n".join(str(r) for r in group.renderables)
        assert rendered.count("- line") == 3
        assert "- line1" in rendered
        assert "- line2" in rendered
        assert "- line3" in rendered
        assert "+ " not in rendered

    def test_multiline_addition(self):
        """Multiline new_string produces a green '+ ' line per new line."""
        new = "alpha\nbeta\ngamma"
        group = _format_diff("", new)

        rendered = "\n".join(str(r) for r in group.renderables)
        assert rendered.count("+ alpha") == 1
        assert rendered.count("+ beta") == 1
        assert rendered.count("+ gamma") == 1
        assert "+ alpha" in rendered
        assert "+ beta" in rendered
        assert "+ gamma" in rendered
        assert "- " not in rendered

    def test_both_sides_produces_combined_output(self):
        """Providing both old and new strings renders deletions followed by additions."""
        old = "foo\nbar"
        new = "foo\nbaz"
        group = _format_diff(old, new)

        rendered = "\n".join(str(r) for r in group.renderables)
        # The function renders ALL old lines as deletions, ALL new lines as additions.
        # It does not perform line-level diffing, so even unchanged lines appear twice.
        assert "- foo" in rendered
        assert "- bar" in rendered
        assert "+ foo" in rendered
        assert "+ baz" in rendered
        # Deletions come first, then additions
        minus_idx = rendered.index("- ")
        plus_idx = rendered.index("+ ")
        assert minus_idx < plus_idx

    def test_no_empty_diff_both_empty(self):
        """When both old and new are empty the diff group is empty (no-empty-diff guard)."""
        group = _format_diff("", "")
        assert len(group.renderables) == 0

    def test_no_empty_diff_both_none_equivalent(self):
        """Passing empty strings (not None) still results in no empty-diff."""
        group = _format_diff("", "")
        assert len(group.renderables) == 0

    def test_empty_old_string_only_new(self):
        """Empty old_string with new content renders only additions."""
        group = _format_diff("", "only added")
        rendered = "\n".join(str(r) for r in group.renderables)
        assert "+ only added" in rendered

    def test_empty_new_string_only_old(self):
        """Empty new_string with old content renders only deletions."""
        group = _format_diff("only removed", "")
        rendered = "\n".join(str(r) for r in group.renderables)
        assert "- only removed" in rendered

    def test_single_line_deletion(self):
        """Single-line old_string produces exactly one deletion line."""
        group = _format_diff("solo line\n", "")
        rendered = "\n".join(str(r) for r in group.renderables)
        assert rendered.count("- ") == 1
        assert "- solo line" in rendered

    def test_single_line_addition(self):
        """Single-line new_string produces exactly one addition line."""
        group = _format_diff("", "brand new\n")
        rendered = "\n".join(str(r) for r in group.renderables)
        assert rendered.count("+ ") == 1
        assert "+ brand new" in rendered

    def test_line_counting_matches_actual_lines(self):
        """Line count in rendered output matches the number of non-empty lines in input."""
        old_lines = ["def foo():", "    pass", "    return None"]
        new_lines = ["def foo():", "    return True", "    raise NotImplemented"]
        group = _format_diff("\n".join(old_lines), "\n".join(new_lines))

        rendered = "\n".join(str(r) for r in group.renderables)
        # 3 old lines → 3 deletion lines; 3 new lines → 3 addition lines
        assert rendered.count("- ") == 3
        assert rendered.count("+ ") == 3


class TestResolveTools:
    """Test resolve_tools function."""

    def test_empty_returns_general_tools(self):
        result = resolve_tools([])
        assert result == list(GENERAL_TOOLS)

    def test_allowlist(self):
        result = resolve_tools(["read_file", "bash"])
        assert len(result) == 2
        assert all(f.__name__ in ("read_file", "bash") for f in result)

    def test_dict_disable(self):
        result = resolve_tools({"bash": False})
        names = [f.__name__ for f in result]
        assert "bash" not in names
        assert "read_file" in names  # other tools still present

    def test_dict_enable_extra(self):
        result = resolve_tools({"find_dependencies": True})
        names = [f.__name__ for f in result]
        assert "find_dependencies" in names

    def test_unknown_tool_ignored(self):
        result = resolve_tools(["read_file", "nonexistent_tool"])
        assert len(result) == 1
        assert result[0].__name__ == "read_file"

    def test_registry_has_core_tools(self):
        for name in ("read_file", "write_file", "edit_file", "bash",
                      "glob_search", "grep_search", "delegate_task"):
            assert name in TOOL_REGISTRY


class TestDelegateTaskDocstring:
    """Tests for dynamic delegate_task docstring with available subagents."""

    def test_docstring_includes_subagents(self):
        from aru.config import CustomAgent
        agents = {
            "reviewer": CustomAgent(
                name="Reviewer", description="Review code for quality",
                system_prompt="p", source_path="/f.md", mode="subagent",
            ),
            "primary_agent": CustomAgent(
                name="Primary", description="Primary agent",
                system_prompt="p", source_path="/f.md", mode="primary",
            ),
        }
        set_custom_agents(agents)
        doc = delegate_task.__doc__
        assert 'agent="reviewer"' in doc
        assert "Review code for quality" in doc
        # Primary agents should not be listed (only subagents are registered)
        assert "Primary" not in doc

    def test_docstring_without_subagents(self):
        set_custom_agents({})
        doc = delegate_task.__doc__
        assert "Available specialized agents" not in doc
        assert "delegate" in doc.lower()