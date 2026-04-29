"""Task list and plan steps panels render into the TUI sidebar (Tier 2.6).

Before fix/tui-tier-improvements, every ``create_task_list`` /
``update_task`` call mounted a fresh Rich panel inside the chat —
producing N stacked snapshots over a turn. The new contract:

* Tasklist + plan changes publish ``tasklist.updated`` / ``plan.updated``
  events.
* The ``TasklistPanel`` sidebar subscribes and renders the current
  snapshot in one place.
* The chat fallback only fires when the sidebar is explicitly hidden.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_create_task_list_publishes_tasklist_event():
    """``create_task_list`` should publish ``tasklist.updated`` on the bus."""
    from aru.runtime import init_ctx, set_ctx
    from aru.plugins.manager import PluginManager
    from aru.tools.tasklist import create_task_list

    received: list[dict] = []

    ctx = init_ctx()
    mgr = PluginManager()
    ctx.plugin_manager = mgr
    set_ctx(ctx)
    mgr.subscribe("tasklist.updated", lambda p: received.append(p))

    create_task_list(["first thing", "second thing"])
    # publish is scheduled on the running loop; yield once to let it run.
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert received, "tasklist.updated never fired"
    last = received[-1]
    descs = [t.get("description") for t in last.get("tasks", [])]
    assert "first thing" in descs
    assert "second thing" in descs


@pytest.mark.asyncio
async def test_tasklist_panel_renders_when_event_received():
    """When the bus publishes ``tasklist.updated``, the sidebar lights up."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.tasklist_panel import TasklistPanel

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(TasklistPanel)
        assert panel.task_count() == 0
        panel.on_tasklist_updated(
            {
                "tasks": [
                    {"index": 1, "description": "alpha", "status": "pending"},
                    {"index": 2, "description": "beta", "status": "in_progress"},
                ]
            }
        )
        await pilot.pause()
        assert panel.task_count() == 2
        assert panel.has_class("-busy")


@pytest.mark.asyncio
async def test_plan_panel_renders_when_event_received():
    from aru.tui.app import AruApp
    from aru.tui.widgets.tasklist_panel import TasklistPanel

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(TasklistPanel)
        panel.on_plan_updated(
            {
                "steps": [
                    {"index": 1, "description": "phase one", "status": "completed"},
                    {"index": 2, "description": "phase two", "status": "in_progress"},
                ]
            }
        )
        await pilot.pause()
        assert panel.plan_step_count() == 2


@pytest.mark.asyncio
async def test_tasklist_event_clears_panel_when_empty():
    from aru.tui.app import AruApp
    from aru.tui.widgets.tasklist_panel import TasklistPanel

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(TasklistPanel)
        panel.on_tasklist_updated({"tasks": [{"index": 1, "description": "x"}]})
        await pilot.pause()
        assert panel.task_count() == 1
        panel.on_tasklist_updated({"tasks": []})
        await pilot.pause()
        assert panel.task_count() == 0
        assert not panel.has_class("-busy")


@pytest.mark.asyncio
async def test_toggle_visibility_hides_panel():
    from aru.tui.app import AruApp
    from aru.tui.widgets.tasklist_panel import TasklistPanel

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(TasklistPanel)
        panel.on_tasklist_updated({"tasks": [{"index": 1, "description": "x"}]})
        await pilot.pause()
        assert panel.has_class("-busy")
        hidden = panel.toggle_visibility()
        await pilot.pause()
        assert hidden is True
        assert panel.has_class("-hidden")
