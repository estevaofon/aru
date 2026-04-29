"""Ctrl+Y / Ctrl+Shift+Y copy assistant / full chat to clipboard."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_copy_last_assistant_message(monkeypatch):
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatPane

    captured: list[str] = []

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Stub the App's copy_to_clipboard so we can observe the payload.
        monkeypatch.setattr(app, "copy_to_clipboard", lambda t: captured.append(t))
        chat = app.query_one(ChatPane)
        chat.start_assistant_message()
        chat.append_assistant_delta("hello from the agent")
        await pilot.pause(0.15)
        chat.finalize_assistant_message()
        app.action_copy_last()
        await pilot.pause()
    assert captured == ["hello from the agent"]


@pytest.mark.asyncio
async def test_copy_last_warns_when_no_assistant(monkeypatch):
    from aru.tui.app import AruApp

    calls: list[tuple[str, str]] = []
    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app, "notify", lambda msg, severity="info", **_k: calls.append((msg, severity))
        )
        app.action_copy_last()
        await pilot.pause()
    # Should emit a warning, not crash.
    assert calls
    assert "warning" in calls[-1][1] or "no assistant" in calls[-1][0].lower()


@pytest.mark.asyncio
async def test_copy_all_captures_full_transcript(monkeypatch):
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatPane

    captured: list[str] = []
    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "copy_to_clipboard", lambda t: captured.append(t))
        chat = app.query_one(ChatPane)
        chat.add_user_message("question one")
        chat.start_assistant_message()
        chat.append_assistant_delta("reply one")
        await pilot.pause(0.15)
        chat.finalize_assistant_message()
        chat.add_user_message("question two")
        await pilot.pause()
        app.action_copy_all()
        await pilot.pause()
    assert captured
    transcript = captured[-1]
    assert "question one" in transcript
    assert "reply one" in transcript
    assert "question two" in transcript


@pytest.mark.asyncio
async def test_copy_bindings_registered():
    from aru.tui.app import AruApp

    keys = {b.key: b.action for b in AruApp.BINDINGS if hasattr(b, "key")}
    assert "ctrl+y" in keys
    assert keys["ctrl+y"] == "copy_last"
    assert "ctrl+shift+y" in keys


@pytest.mark.asyncio
async def test_ctrl_c_is_context_sensitive():
    """Ctrl+C is bound, but to ``ctrl_c`` — a context-sensitive action
    matching the REPL: select+Ctrl+C copies; bare Ctrl+C during a turn
    interrupts the agent; bare Ctrl+C at an idle prompt exits."""
    from aru.tui.app import AruApp

    keys = {b.key: b.action for b in AruApp.BINDINGS if hasattr(b, "key")}
    assert "ctrl+c" in keys
    assert keys["ctrl+c"] == "ctrl_c"


@pytest.mark.asyncio
async def test_ctrl_c_idle_never_quits(monkeypatch):
    """Ctrl+C from an idle prompt must never exit the app.

    Background: when ``screen.get_selected_text`` intermittently misses
    a real selection (focus shift, terminal scroll, refresh), an
    "exit on second Ctrl+C" rule turned a retry-to-copy into a quit.
    Users now exit via Ctrl+Q or /quit, both visible in the footer.
    """
    from aru.tui.app import AruApp

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        called = {"abort": 0, "quit": 0}
        notes: list[tuple[str, str]] = []
        monkeypatch.setattr(
            app, "_abort_running_turn",
            lambda: called.__setitem__("abort", called["abort"] + 1),
        )
        monkeypatch.setattr(
            app, "action_quit_app",
            lambda: called.__setitem__("quit", called["quit"] + 1),
        )
        monkeypatch.setattr(
            app, "notify",
            lambda msg, severity="info", **_k: notes.append((msg, severity)),
        )
        try:
            app.screen.clear_selection()
        except Exception:
            pass
        app._busy = False
        # Hammer Ctrl+C repeatedly — none of these should ever quit.
        for _ in range(5):
            app.action_ctrl_c()
        await pilot.pause()
    assert called["abort"] == 0
    assert called["quit"] == 0, "idle Ctrl+C must NEVER quit the app"
    # User got a visible hint at least once.
    assert notes, "expected at least one toast hint"
    assert "ctrl+q" in notes[-1][0].lower() or "/quit" in notes[-1][0].lower()


@pytest.mark.asyncio
async def test_ctrl_c_busy_interrupts_without_quitting(monkeypatch):
    """Agent turn running → Ctrl+C aborts the turn, app stays alive."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        called = {"abort": 0, "quit": 0}
        monkeypatch.setattr(
            app, "_abort_running_turn", lambda: called.__setitem__("abort", called["abort"] + 1)
        )
        monkeypatch.setattr(
            app, "action_quit_app", lambda: called.__setitem__("quit", called["quit"] + 1)
        )
        try:
            app.screen.clear_selection()
        except Exception:
            pass
        app._busy = True
        app.action_ctrl_c()
        await pilot.pause()
        chat = app.query_one(ChatPane)
        system_msgs = [
            m for m in chat.query(ChatMessageWidget) if m.role == "system"
        ]
    assert called["abort"] == 1
    # Crucial: the app MUST NOT quit mid-turn — the user just wants the
    # prompt back to type a correction.
    assert called["quit"] == 0
    assert any("Interrupted" in m.buffer for m in system_msgs)


@pytest.mark.asyncio
async def test_ctrl_c_with_selection_copies(monkeypatch):
    """Active text selection → Ctrl+C copies, does NOT quit."""
    from aru.tui.app import AruApp

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        captured: list[str] = []
        monkeypatch.setattr(app, "copy_to_clipboard", lambda t: captured.append(t))
        # Fake a selection on the screen.
        monkeypatch.setattr(app.screen, "get_selected_text", lambda: "selected!")
        monkeypatch.setattr(app.screen, "clear_selection", lambda: None)
        quit_called = {"n": 0}
        monkeypatch.setattr(
            app, "action_quit_app", lambda: quit_called.__setitem__("n", quit_called["n"] + 1)
        )
        app.action_ctrl_c()
        await pilot.pause()
    assert captured == ["selected!"]
    assert quit_called["n"] == 0


@pytest.mark.asyncio
async def test_sigint_handler_replaced_during_app_lifetime():
    """While the App is running, SIGINT must not raise KeyboardInterrupt.

    On Windows the OS still routes ``CTRL_C_EVENT`` through Python's
    signal module even when Textual disables ``ENABLE_PROCESSED_INPUT``,
    so a stray SIGINT from a sub-process group could tear the App down
    intermittently. We swap in a no-op handler in ``on_mount`` and put
    the original back in ``on_unmount`` so the parent shell isn't
    affected after the TUI exits.
    """
    import signal

    from aru.tui.app import AruApp

    pre = signal.getsignal(signal.SIGINT)
    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        during = signal.getsignal(signal.SIGINT)
        # The no-op handler is a callable that's not the previous one.
        assert during is not pre
        assert callable(during)
    # After the App tears down, we restored the original handler.
    after = signal.getsignal(signal.SIGINT)
    assert after is pre


@pytest.mark.asyncio
async def test_chat_widgets_allow_text_selection():
    """ChatPane and ChatMessageWidget must have ALLOW_SELECT=True."""
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane

    assert ChatPane.ALLOW_SELECT is True
    assert ChatMessageWidget.ALLOW_SELECT is True
