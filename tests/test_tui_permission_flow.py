"""Integration tests for the TuiUI → ModalScreen flow (E7)."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")


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
