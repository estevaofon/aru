"""Tests for file_link — path detection + click action wiring."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("textual")


def test_path_re_matches_unix_relative_with_line():
    from aru.tui.widgets.file_link import _PATH_RE

    m = list(_PATH_RE.finditer("check aru/foo.py:42 for the bug"))
    assert len(m) == 1
    assert m[0].group("path") == "aru/foo.py"
    assert m[0].group("line") == "42"


def test_path_re_matches_unix_relative_without_line():
    from aru.tui.widgets.file_link import _PATH_RE

    m = list(_PATH_RE.finditer("see ./scripts/run.sh and tests/test_x.py"))
    paths = [match.group("path") for match in m]
    assert "./scripts/run.sh" in paths
    assert "tests/test_x.py" in paths


def test_path_re_skips_bare_word_like_main_py():
    """``main.py`` standing alone (no directory component) should NOT match."""
    from aru.tui.widgets.file_link import _PATH_RE

    m = list(_PATH_RE.finditer("just main.py here"))
    assert m == []


def test_path_re_matches_windows_path_with_line():
    from aru.tui.widgets.file_link import _PATH_RE

    m = list(_PATH_RE.finditer(r"open D:\proj\src\app.py:7"))
    assert len(m) == 1
    assert m[0].group("path") == r"D:\proj\src\app.py"
    assert m[0].group("line") == "7"


def test_add_path_links_records_target_and_stylises():
    from rich.text import Text

    from aru.tui.widgets.file_link import add_path_links

    txt = Text("Edit aru/foo.py:42 to fix")
    targets: list[tuple[str, int | None]] = []
    add_path_links(txt, targets)
    assert ("aru/foo.py", 42) in targets


def test_add_path_links_skips_url_neighbours():
    """Don't make a URL's path part clickable."""
    from rich.text import Text

    from aru.tui.widgets.file_link import add_path_links

    txt = Text("see https://example.com/foo.py:42")
    targets: list[tuple[str, int | None]] = []
    add_path_links(txt, targets)
    # The lookbehind already rejects URL contexts; targets stays empty.
    assert targets == []


def test_open_in_editor_resolves_relative_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "subdir" / "foo.py"
    target.parent.mkdir()
    target.write_text("x = 1\n")
    monkeypatch.setenv("ARU_EDITOR", "echo")
    # ``echo`` is on PATH on Linux/macOS and via Git Bash on Windows.
    # If unavailable, the test skips silently rather than fail.
    import shutil

    if shutil.which("echo") is None:
        pytest.skip("no echo on PATH")
    from aru.tui.widgets.file_link import open_in_editor

    ok = open_in_editor("subdir/foo.py", line=1)
    assert ok is True


@pytest.mark.asyncio
async def test_finalize_render_populates_link_targets():
    """An assistant bubble closed with a path in the buffer should
    expose the path as a clickable target."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        chat.start_assistant_message()
        chat.append_assistant_delta("I changed `aru/foo.py:42` to fix the bug.")
        chat.finalize_assistant_message()
        await pilot.pause()
        bubbles = [
            m for m in chat.query(ChatMessageWidget) if m.role == "assistant"
        ]
        assert bubbles, "no assistant bubble created"
        assert ("aru/foo.py", 42) in bubbles[-1]._link_targets
