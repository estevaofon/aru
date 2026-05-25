"""Tests for the TUI shell boot (E2)."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_app_mounts_and_exits_cleanly():
    """The minimal AruApp should start, be interactive, and exit via action."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatPane

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Verify chat pane is composed (E3b — replaced the placeholder).
        assert app.query_one(ChatPane) is not None
        # Trigger quit action — should exit cleanly.
        app.action_quit_app()
        await pilot.pause()
    assert app.return_code == 0


@pytest.mark.asyncio
async def test_ctrl_q_exits():
    """Ctrl+Q binding triggers the quit action."""
    from aru.tui.app import AruApp

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
    assert app.return_code == 0


@pytest.mark.asyncio
async def test_resumed_history_is_replayed_in_chat_pane():
    """A session with prior text turns should have them mounted in the ChatPane.

    The TUI uses ``session.history`` as its replay source — the user
    should see the last prompt (and context) as chat bubbles, plus a
    "Resumed session" banner, not a blank chat like a fresh start.
    """
    from aru.session import Session
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane

    session = Session()
    session.add_message("user", "first prompt")
    session.add_message("assistant", "first reply")
    session.add_message("user", "last prompt")

    app = AruApp(session=session)
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        msgs = chat.query(ChatMessageWidget)
        roles = [w.role for w in msgs]
        buffers = [w.buffer for w in msgs]
        # Banner + 3 text turns replayed (2 user + 1 assistant).
        assert "system" in roles
        assert roles.count("user") == 2
        assert roles.count("assistant") == 1
        assert any("last prompt" in b for b in buffers)
        assert any("Resumed session" in b for b in buffers)
        app.action_quit_app()
        await pilot.pause()


def test_compose_terminal_title_blank_session_returns_plain_aru():
    """A brand-new session with no history should yield the bare ``aru``."""
    from aru.session import Session
    from aru.tui.app import _compose_terminal_title

    assert _compose_terminal_title(Session()) == "aru"


def test_compose_terminal_title_uses_session_summary():
    """A session with history should surface its title in the tab."""
    from aru.session import Session
    from aru.tui.app import _compose_terminal_title

    session = Session()
    session.add_message("user", "fix the flaky test in auth")
    title = _compose_terminal_title(session)
    assert title.startswith("aru — ")
    assert "fix the flaky test" in title


def test_compose_terminal_title_pending_wins_over_history():
    """A pending prompt should replace the stored title immediately."""
    from aru.session import Session
    from aru.tui.app import _compose_terminal_title

    session = Session()
    session.add_message("user", "old prompt")
    title = _compose_terminal_title(session, pending="new prompt just sent")
    assert "new prompt just sent" in title


def test_set_terminal_title_writes_osc_to_underscore_stdout(monkeypatch):
    """The helper writes OSC 0 to ``sys.__stdout__`` (not ``sys.stdout``).

    On Windows, ``cli.py`` replaces ``sys.stdout`` with a fresh
    ``TextIOWrapper`` that can swallow OSC escapes depending on the
    host terminal. ``sys.__stdout__`` is the original handle captured
    by the Python interpreter and is what Textual's own driver writes
    to, so title sequences need to land on that same stream to reach
    Windows Terminal / PowerShell.
    """
    import io
    from aru.tui import app as app_mod

    underscore = io.StringIO()
    # ``sys.stdout`` is a *different* stream; if the helper accidentally
    # wrote there the title would never reach the real terminal.
    wrapper_stdout = io.StringIO()
    monkeypatch.setattr(app_mod.sys, "stdout", wrapper_stdout)
    monkeypatch.setattr(app_mod.sys, "__stdout__", underscore)

    app_mod._set_terminal_title("aru — hello")

    written = underscore.getvalue()
    assert written.startswith("\033]0;"), "expected OSC 0 prefix"
    assert "aru — hello" in written
    assert written.endswith("\a"), "expected BEL terminator"
    assert wrapper_stdout.getvalue() == "", (
        "helper must not write to sys.stdout (the wrapped handle)"
    )


def test_set_terminal_title_noop_when_underscore_stdout_is_none(monkeypatch):
    """``pythonw.exe`` with no console leaves ``sys.__stdout__`` as None."""
    from aru.tui import app as app_mod

    monkeypatch.setattr(app_mod.sys, "__stdout__", None)
    # Must not raise.
    app_mod._set_terminal_title("aru")
    app_mod._push_terminal_title()
    app_mod._pop_terminal_title()


@pytest.mark.asyncio
async def test_fresh_session_renders_no_replay_banner():
    """A fresh session (no history) shouldn't show the 'Resumed session' banner."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        buffers = [w.buffer for w in chat.query(ChatMessageWidget)]
        assert not any("Resumed session" in b for b in buffers)
        app.action_quit_app()
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_ui_notify_safe():
    """TuiUI.notify / print should not raise even with a running app."""
    from aru.tui.app import AruApp
    from aru.tui.ui import TuiUI

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        ui = TuiUI(app)
        ui.notify("hello")
        ui.print("info")
        app.action_quit_app()
        await pilot.pause()


def test_app_bindings_contain_quit():
    """Sanity: binding registry has a Ctrl+Q entry for quit."""
    from aru.tui.app import AruApp

    keys = {
        b.key for b in AruApp.BINDINGS if hasattr(b, "key")
    }
    assert "ctrl+q" in keys


def test_runtime_ctx_has_tui_slots():
    """RuntimeContext must expose tui_app and ui fields (E2)."""
    from aru.runtime import RuntimeContext

    ctx = RuntimeContext()
    assert ctx.tui_app is None
    assert ctx.ui is None
    # Assignable
    ctx.tui_app = object()
    ctx.ui = object()
    assert ctx.tui_app is not None
    assert ctx.ui is not None


def test_main_wires_tui_flag(monkeypatch):
    """main() should route `--tui` to run_tui (the only interactive interface).

    We stub asyncio.run so main() can observe the call synchronously
    (main is a sync function; we don't want pytest's event loop here).
    """
    import asyncio as _asyncio
    import sys

    called = {"run_tui": 0, "run_oneshot": 0}

    async def fake_run_tui(**kwargs):
        called["run_tui"] += 1

    async def fake_run_oneshot(*args, **kwargs):
        called["run_oneshot"] += 1

    def fake_asyncio_run(coro):
        # Inspect which coroutine we were handed.
        name = getattr(coro, "__qualname__", "") or getattr(coro, "cr_code", object()).__repr__()
        if "run_tui" in name:
            called["run_tui"] += 1
        elif "run_oneshot" in name:
            called["run_oneshot"] += 1
        # Consume the coro so no RuntimeWarning leaks.
        coro.close()

    from aru import cli as cli_mod

    monkeypatch.setattr(cli_mod, "run_oneshot", fake_run_oneshot)
    monkeypatch.setattr("aru.tui.run_tui", fake_run_tui)
    monkeypatch.setattr(cli_mod.asyncio, "run", fake_asyncio_run)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "argv", ["aru", "--tui"])

    cli_mod.main()

    assert called["run_tui"] == 1
    assert called["run_oneshot"] == 0


def test_main_without_any_flag_routes_to_tui(monkeypatch):
    """No args means main() launches the TUI (the only interactive interface)."""
    import sys

    called = {"run_tui": 0}

    def fake_asyncio_run(coro):
        name = getattr(coro, "__qualname__", "")
        if "run_tui" in name:
            called["run_tui"] += 1
        coro.close()

    from aru import cli as cli_mod
    monkeypatch.setattr(cli_mod.asyncio, "run", fake_asyncio_run)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "argv", ["aru"])

    cli_mod.main()

    assert called["run_tui"] == 1
