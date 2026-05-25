"""Integration tests for the TuiUI → ModalScreen flow (E7)."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")


async def _wait_for_inline_focus(app, pilot, *, max_iter: int = 50):
    """Block until the InlineChoicePrompt's OptionList owns focus.

    ``query(InlineChoicePrompt)`` returns the widget as soon as it is in
    the DOM, but the OptionList only gains focus (and its default
    highlight) inside ``InlineChoicePrompt.on_mount`` — a message
    dispatched *after* mount completes. Pressing Enter / arrow keys
    before that lands sends the key nowhere useful: ``OptionSelected``
    never fires and the worker thread blocks until its timeout. In
    isolation the gap is sub-tick so the press always lands; under a
    loaded suite the mount lifecycle slips behind detection and the test
    flakes with a TimeoutError. Waiting for focus closes the race.
    """
    from textual.widgets import OptionList

    from aru.tui.widgets.inline_choice import InlineChoicePrompt

    for _ in range(max_iter):
        prompts = list(app.query(InlineChoicePrompt))
        if prompts:
            opts = list(prompts[0].query(OptionList))
            if opts and app.focused is opts[0] and opts[0].highlighted is not None:
                return opts[0]
        await pilot.pause(0.05)
    raise AssertionError("InlineChoicePrompt OptionList never took focus")


@pytest.mark.asyncio
async def test_tui_ask_choice_from_worker_resolves_via_modal():
    """TuiUI.ask_choice invoked from a worker thread returns modal result.

    Simulates the permission prompt path: tool code (sync) runs in
    asyncio.to_thread, calls ctx.ui.ask_choice(...), modal appears in
    the App, user selects option → choice returned synchronously.
    """
    from aru.tui.app import AruApp
    from aru.tui.ui import TuiUI

    app = AruApp()
    result_holder: dict = {}

    async def worker_calls_ask_choice() -> None:
        ui = TuiUI(app)
        # asyncio.to_thread moves us off the App loop, matching how
        # check_permission is called from tool threads.
        choice = await asyncio.to_thread(
            ui.ask_choice,
            ["Allow", "Deny"],
            title="Test",
            default=0,
            cancel_value=None,
        )
        result_holder["choice"] = choice

    async with app.run_test() as pilot:
        await pilot.pause()
        worker_task = asyncio.create_task(worker_calls_ask_choice())
        # Wait for modal to appear
        for _ in range(50):
            await pilot.pause(0.05)
            from aru.tui.screens import ChoiceModal
            if app.screen_stack and isinstance(app.screen, ChoiceModal):
                break
        # Select option 0 (default highlight) via enter
        await pilot.press("enter")
        await asyncio.wait_for(worker_task, timeout=5.0)
    assert result_holder["choice"] == 0


@pytest.mark.asyncio
async def test_ask_choice_with_details_uses_inline_prompt_not_modal():
    """Preview + approval prompt both mount in the ChatPane, no modal.

    The modal overlay would hide the diff behind itself — which defeats
    the purpose of showing it. The inline path lets the user scroll the
    ChatPane freely to read the full preview before pressing Enter on
    the prompt. Mirrors OpenCode's UX.
    """
    from rich.panel import Panel
    from aru.tui.app import AruApp
    from aru.tui.screens import ChoiceModal
    from aru.tui.ui import TuiUI
    from aru.tui.widgets.chat import ChatPane
    from aru.tui.widgets.inline_choice import InlineChoicePrompt

    app = AruApp()
    holder: dict = {}
    diff = Panel(
        "- old line\n+ new line\n+ more lines of diff",
        title="edit: /tmp/foo.py",
        border_style="yellow",
    )

    async def worker() -> None:
        ui = TuiUI(app)
        holder["choice"] = await asyncio.to_thread(
            ui.ask_choice,
            ["Yes", "No"],
            title="Approve?",
            default=0,
            cancel_value=None,
            details=diff,
        )

    async with app.run_test() as pilot:
        await pilot.pause()
        task = asyncio.create_task(worker())
        # Wait for the inline prompt to mount in the ChatPane.
        for _ in range(50):
            await pilot.pause(0.05)
            chat = app.query_one(ChatPane)
            prompts = list(chat.query(InlineChoicePrompt))
            if prompts:
                break
        # Crucial: no ChoiceModal was pushed — the details stay visible.
        assert not any(
            isinstance(s, ChoiceModal) for s in app.screen_stack
        ), "inline path must not push a ChoiceModal"
        # The prompt is present; so is the preview panel above it.
        chat = app.query_one(ChatPane)
        assert list(chat.query(InlineChoicePrompt)), (
            "expected InlineChoicePrompt in ChatPane"
        )
        # Press Enter — OptionList focuses on mount, default=0 highlighted.
        # Wait for that focus to actually land first; querying the prompt
        # only proves it is in the DOM (see _wait_for_inline_focus).
        await _wait_for_inline_focus(app, pilot)
        await pilot.press("enter")
        await asyncio.wait_for(task, timeout=5.0)
    assert holder["choice"] == 0


@pytest.mark.asyncio
async def test_inline_prompt_hides_input_bar_and_restores_on_answer():
    """Claude-Code parity: the text input disappears while the approval
    prompt is awaiting a decision, and returns once the user has answered.

    Without this, the user sees both the approval options AND a blinking
    text box at the bottom, making it ambiguous where to focus; the
    decision surface must be the only one available while a choice is
    pending.
    """
    from rich.panel import Panel

    from aru.tui.app import AruApp
    from aru.tui.ui import TuiUI
    from aru.tui.widgets.chat import ChatPane
    from aru.tui.widgets.inline_choice import InlineChoicePrompt
    from aru.tui.widgets.prompt_area import PromptArea

    app = AruApp()
    holder: dict = {}

    async def worker() -> None:
        ui = TuiUI(app)
        holder["choice"] = await asyncio.to_thread(
            ui.ask_choice,
            ["Yes", "No"],
            title="Approve?",
            default=0,
            cancel_value=None,
            details=Panel("- old\n+ new"),
        )

    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#input", PromptArea)
        assert not inp.has_class("-hidden"), "input should be visible at rest"
        task = asyncio.create_task(worker())
        for _ in range(50):
            await pilot.pause(0.05)
            if list(app.query_one(ChatPane).query(InlineChoicePrompt)):
                break
        # While the prompt is live, the input bar is hidden.
        assert inp.has_class("-hidden"), (
            "input should be hidden while InlineChoicePrompt is mounted"
        )
        await _wait_for_inline_focus(app, pilot)
        await pilot.press("enter")
        await asyncio.wait_for(task, timeout=5.0)
        # After the user answers, the input bar is restored.
        for _ in range(20):
            await pilot.pause(0.05)
            if not inp.has_class("-hidden"):
                break
        assert not inp.has_class("-hidden"), (
            "input should reappear after the prompt is answered"
        )
    assert holder["choice"] == 0


@pytest.mark.asyncio
async def test_ask_choice_inline_esc_cancels_with_cancel_value():
    """Esc on the inline prompt dismisses with ``cancel_value``."""
    from rich.panel import Panel
    from aru.tui.app import AruApp
    from aru.tui.ui import TuiUI
    from aru.tui.widgets.chat import ChatPane
    from aru.tui.widgets.inline_choice import InlineChoicePrompt

    app = AruApp()
    holder: dict = {}

    async def worker() -> None:
        ui = TuiUI(app)
        holder["choice"] = await asyncio.to_thread(
            ui.ask_choice,
            ["Yes", "No"],
            title="Approve?",
            default=0,
            cancel_value=99,
            details=Panel("preview"),
        )

    async with app.run_test() as pilot:
        await pilot.pause()
        task = asyncio.create_task(worker())
        for _ in range(50):
            await pilot.pause(0.05)
            if list(app.query_one(ChatPane).query(InlineChoicePrompt)):
                break
        await _wait_for_inline_focus(app, pilot)
        await pilot.press("escape")
        await asyncio.wait_for(task, timeout=5.0)
    assert holder["choice"] == 99


@pytest.mark.asyncio
async def test_auto_accept_inline_choice_updates_status_pane_mode():
    """Regression: picking "auto-accept edits" from the permission prompt
    must update the StatusPane mode badge via the bus.

    Reproduces the bug where ``check_permission`` assigned
    ``ctx.permission_mode`` directly, bypassing ``set_permission_mode``
    and the ``permission.mode.changed`` publish — so the status bar
    stayed stuck on "default" after the user explicitly opted in.
    """
    from rich.panel import Panel

    from aru.plugins.manager import PluginManager
    from aru.runtime import init_ctx, set_ctx
    from aru.tui.app import AruApp
    from aru.tui.ui import TuiUI
    from aru.tui.widgets.chat import ChatPane
    from aru.tui.widgets.inline_choice import InlineChoicePrompt
    from aru.tui.widgets.status import StatusPane

    ctx = init_ctx()
    ctx.permission_mode = "default"
    mgr = PluginManager()
    mgr._loaded = True  # enable _schedule_publish delivery
    ctx.plugin_manager = mgr

    app = AruApp(ctx=ctx, plugin_manager=mgr)
    ctx.tui_app = app
    holder: dict = {}

    async def worker() -> None:
        set_ctx(ctx)
        from aru.permissions import check_permission

        holder["allowed"] = await asyncio.to_thread(
            check_permission,
            "edit",
            "/tmp/foo.py",
            Panel("- old\n+ new", title="edit: /tmp/foo.py"),
        )

    async with app.run_test() as pilot:
        await pilot.pause()
        ctx.ui = TuiUI(app)
        status = app.query_one(StatusPane)
        assert status.mode == "default"
        task = asyncio.create_task(worker())
        for _ in range(60):
            await pilot.pause(0.05)
            chat = app.query_one(ChatPane)
            if list(chat.query(InlineChoicePrompt)):
                break
        # Option index 1 = "Yes, and auto-accept edits".
        await _wait_for_inline_focus(app, pilot)
        await pilot.press("down")
        await pilot.press("enter")
        await asyncio.wait_for(task, timeout=5.0)
        # Let the publish task + subscriber dispatch land on this loop.
        for _ in range(10):
            await pilot.pause(0.05)
            if status.mode == "acceptEdits":
                break
    assert holder["allowed"] is True
    assert ctx.permission_mode == "acceptEdits"
    assert status.mode == "acceptEdits", (
        f"StatusPane.mode stayed on {status.mode!r} — "
        "permission.mode.changed was never published."
    )


@pytest.mark.asyncio
async def test_thinking_spinner_hidden_while_prompt_open():
    """While a permission prompt owns the screen the spinner must be hidden —
    we're parked waiting on the user, not computing — and it must return once
    the user answers (the turn is still in flight)."""
    from rich.panel import Panel

    from aru.tui.app import AruApp
    from aru.tui.ui import TuiUI
    from aru.tui.widgets.chat import ChatPane
    from aru.tui.widgets.inline_choice import InlineChoicePrompt
    from aru.tui.widgets.thinking import ThinkingIndicator

    app = AruApp()
    holder: dict = {}

    async def worker() -> None:
        ui = TuiUI(app)
        holder["choice"] = await asyncio.to_thread(
            ui.ask_choice,
            ["Yes", "No"],
            title="Approve?",
            default=0,
            cancel_value=None,
            details=Panel("- old\n+ new"),
        )

    async with app.run_test() as pilot:
        await pilot.pause()
        indicator = app.query_one(ThinkingIndicator)
        # Simulate a turn in flight (this is what _run_turn does).
        indicator.busy = True
        await pilot.pause()
        assert indicator.has_class("-busy"), "spinner should show while busy at rest"

        task = asyncio.create_task(worker())
        for _ in range(50):
            await pilot.pause(0.05)
            if list(app.query_one(ChatPane).query(InlineChoicePrompt)):
                break
        await pilot.pause(0.05)
        # Busy is still true, but the spinner is hidden while the prompt is up.
        assert indicator.busy is True
        assert not indicator.has_class("-busy"), (
            "spinner must be hidden while the permission prompt is open"
        )

        await _wait_for_inline_focus(app, pilot)
        await pilot.press("enter")
        await asyncio.wait_for(task, timeout=5.0)
        for _ in range(20):
            await pilot.pause(0.05)
            if indicator.has_class("-busy"):
                break
        assert indicator.has_class("-busy"), (
            "spinner should return after the prompt is answered (turn still busy)"
        )
    assert holder["choice"] == 0


@pytest.mark.asyncio
async def test_tui_confirm_from_worker_returns_bool():
    from aru.tui.app import AruApp
    from aru.tui.ui import TuiUI

    app = AruApp()
    result_holder: dict = {}

    async def worker_confirm() -> None:
        ui = TuiUI(app)
        answer = await asyncio.to_thread(ui.confirm, "Proceed?", False)
        result_holder["answer"] = answer

    async with app.run_test() as pilot:
        await pilot.pause()
        worker_task = asyncio.create_task(worker_confirm())
        for _ in range(50):
            await pilot.pause(0.05)
            from aru.tui.screens import ConfirmModal
            if app.screen_stack and isinstance(app.screen, ConfirmModal):
                break
        await pilot.press("y")
        await asyncio.wait_for(worker_task, timeout=5.0)
    assert result_holder["answer"] is True
