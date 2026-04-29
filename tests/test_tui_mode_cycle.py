"""Regression: Ctrl+A and /yolo update StatusPane.mode immediately."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_action_cycle_mode_updates_status_pane():
    """Ctrl+A cycles mode and StatusPane reflects the new value."""
    from aru.runtime import init_ctx
    from aru.tui.app import AruApp
    from aru.tui.widgets.status import StatusPane

    ctx = init_ctx()
    app = AruApp(ctx=ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.query_one(StatusPane)
        assert status.mode == "default"
        app.action_cycle_mode()
        await pilot.pause()
        assert status.mode == "acceptEdits"
        app.action_cycle_mode()
        await pilot.pause()
        assert status.mode == "yolo"
        app.action_cycle_mode()
        await pilot.pause()
        assert status.mode == "default"


@pytest.mark.asyncio
async def test_slash_yolo_toggles_status_pane():
    from aru.runtime import init_ctx
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_area import PromptArea
    from aru.tui.widgets.status import StatusPane

    ctx = init_ctx()
    app = AruApp(ctx=ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.query_one(StatusPane)
        assert status.mode == "default"
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("/yolo"))
        await pilot.pause()
        assert status.mode == "yolo"
        inp.post_message(PromptArea.Submitted("/yolo"))
        await pilot.pause()
        assert status.mode == "default"
