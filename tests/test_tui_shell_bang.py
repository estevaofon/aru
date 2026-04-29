"""Tests for the ``! <command>`` shell escape in the TUI.

The TUI mirrors the REPL's ``! cmd`` path: typing ``! echo hi`` runs
``echo hi`` locally (in the session cwd), streams output into the chat
pane, and reports the exit code — without invoking the agent.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_bang_dispatches_shell_not_agent():
    """``! cmd`` should run via _dispatch_shell_command, NOT _dispatch_user_turn."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_area import PromptArea

    captured: dict = {}

    class _Probe(AruApp):
        def _dispatch_user_turn(self, text: str) -> None:  # type: ignore[override]
            captured["agent_text"] = text

        def _dispatch_shell_command(self, cmd: str) -> None:  # type: ignore[override]
            captured["shell_cmd"] = cmd

    app = _Probe()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("! echo hi"))
        await pilot.pause()

    assert captured.get("shell_cmd") == "echo hi"
    assert "agent_text" not in captured


@pytest.mark.asyncio
async def test_bang_empty_command_warns():
    """``! `` alone should show a usage message and not dispatch anything."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
    from aru.tui.widgets.prompt_area import PromptArea

    captured: dict = {}

    class _Probe(AruApp):
        def _dispatch_user_turn(self, text: str) -> None:  # type: ignore[override]
            captured["agent_text"] = text

        def _dispatch_shell_command(self, cmd: str) -> None:  # type: ignore[override]
            captured["shell_cmd"] = cmd

    app = _Probe()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("!   "))
        await pilot.pause()
        chat = app.query_one(ChatPane)
        msgs = list(chat.query(ChatMessageWidget))
        joined = " ".join(m.buffer for m in msgs)

    assert "Usage:" in joined
    assert "shell_cmd" not in captured
    assert "agent_text" not in captured


@pytest.mark.asyncio
async def test_bang_busy_blocks_dispatch():
    """If the app is already busy, ``! cmd`` should refuse to start."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_area import PromptArea

    captured: dict = {}

    class _Probe(AruApp):
        def _dispatch_shell_command(self, cmd: str) -> None:  # type: ignore[override]
            captured["shell_cmd"] = cmd

    app = _Probe()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._busy = True
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("! echo hi"))
        await pilot.pause()

    assert "shell_cmd" not in captured


@pytest.mark.asyncio
async def test_bang_runs_real_command_and_streams_output():
    """End-to-end: a real shell command's output reaches the chat pane."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
    from aru.tui.widgets.prompt_area import PromptArea

    # Pick a command that works on both Windows and POSIX. ``python -c``
    # avoids shell-specific syntax (echo behaves differently between
    # cmd.exe and bash) and forces a known output line.
    py = sys.executable.replace("\\", "/")
    command = f'{py} -c "print(\'aru-shell-marker\')"'

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted(f"! {command}"))
        # Wait for the worker to finish — _busy flips back to False once
        # the subprocess exits and the finally block runs.
        for _ in range(200):
            await pilot.pause(0.05)
            if not app._busy:
                break
        chat = app.query_one(ChatPane)
        msgs = list(chat.query(ChatMessageWidget))
        joined = "\n".join(m.buffer for m in msgs)

    assert "aru-shell-marker" in joined
    assert "[exit 0]" in joined


@pytest.mark.asyncio
async def test_bang_does_not_persist_to_session_history():
    """Shell runs are local — they must not land in session.history.

    Otherwise the agent would see ``! ls``-style turns on the next
    prompt and try to reason about them as if the user had said them.
    """
    from aru.tui.app import AruApp
    from aru.session import Session
    from aru.tui.widgets.prompt_area import PromptArea

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.session = Session(session_id="test-shell-no-history")
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("! echo hi"))
        # Don't even need to wait for the worker — persistence (or lack
        # thereof) is decided synchronously during dispatch.
        await pilot.pause()
        # Stop the worker promptly so the test exits cleanly.
        try:
            for w in list(app.workers):
                w.cancel()
        except Exception:
            pass

    user_msgs = [m for m in app.session.history if m.get("role") == "user"]
    assert user_msgs == []


@pytest.mark.asyncio
async def test_bang_failing_command_reports_nonzero_exit():
    """A command that exits non-zero should still surface an exit-code line."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
    from aru.tui.widgets.prompt_area import PromptArea

    py = sys.executable.replace("\\", "/")
    command = f'{py} -c "import sys; sys.exit(7)"'

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted(f"! {command}"))
        for _ in range(200):
            await pilot.pause(0.05)
            if not app._busy:
                break
        chat = app.query_one(ChatPane)
        msgs = list(chat.query(ChatMessageWidget))
        joined = "\n".join(m.buffer for m in msgs)

    assert "[exit 7]" in joined
