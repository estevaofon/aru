"""Tests for the slash-command bridge (E6b)."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")


def test_bridge_lists_expected_commands():
    from aru.tui.slash_bridge import supported_commands

    names = supported_commands()
    for expected in ("help", "memory", "worktree", "subagents", "plugin", "debug"):
        assert expected in names, f"/{expected} should be bridged"


@pytest.mark.asyncio
async def test_bridge_runs_help_and_captures_output():
    """/help uses the REPL's ``_show_help`` and returns non-empty text."""
    from aru.tui.app import AruApp
    from aru.tui.slash_bridge import run_bridged

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Fake a minimal config so _show_help doesn't crash
        from dataclasses import dataclass
        @dataclass
        class _Cfg:
            commands: dict = None
            custom_agents: dict = None
        app.config = _Cfg(commands={}, custom_agents={})
        handled, text = run_bridged("help", "", app)
    assert handled is True
    assert text  # non-empty
    # Help text should contain a known keyword.
    assert "/" in text  # lists slash commands


@pytest.mark.asyncio
async def test_bridge_returns_false_for_unknown_command():
    from aru.tui.app import AruApp
    from aru.tui.slash_bridge import run_bridged

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        handled, text = run_bridged("nonexistent", "", app)
    assert handled is False
    assert text == ""


@pytest.mark.asyncio
async def test_bridge_handles_handler_exceptions():
    """A raising handler returns a user-visible error string, not a crash."""
    from aru.tui.app import AruApp
    from aru.tui import slash_bridge as sb

    # Inject a fake command that raises.
    def _boom():
        def _h(*a, **kw):
            raise ValueError("boom")
        return _h

    sb.BRIDGED_COMMANDS["boom"] = (_boom, lambda _app, _body: ((), {}))
    try:
        app = AruApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            handled, text = sb.run_bridged("boom", "", app)
        assert handled is True
        assert "boom" in text.lower() or "ValueError" in text
    finally:
        del sb.BRIDGED_COMMANDS["boom"]


@pytest.mark.asyncio
async def test_app_dispatches_bridged_command():
    """Typing `/memory` in the TUI invokes the bridge and adds a system msg."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
    from aru.tui.widgets.prompt_area import PromptArea

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("/memory"))
        await pilot.pause(0.2)
        chat = app.query_one(ChatPane)
        msgs = list(chat.query(ChatMessageWidget))
        joined = " ".join(m.buffer for m in msgs)
        # Even if /memory prints "nothing yet", the header line was appended.
        assert "/memory" in joined


@pytest.mark.asyncio
async def test_restores_original_console_even_on_error():
    """Bridge must restore ``aru.commands.console`` after any handler run."""
    from aru.tui.app import AruApp
    from aru.tui import slash_bridge as sb
    import aru.commands as cmds

    original = cmds.console

    def _boom():
        def _h(*a, **kw):
            raise RuntimeError("handler error")
        return _h

    sb.BRIDGED_COMMANDS["boom2"] = (_boom, lambda _app, _body: ((), {}))
    try:
        app = AruApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            sb.run_bridged("boom2", "", app)
        assert cmds.console is original
    finally:
        del sb.BRIDGED_COMMANDS["boom2"]
