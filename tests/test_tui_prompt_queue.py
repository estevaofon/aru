"""Tests for PromptQueueWidget — visible queue of pending prompts."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_queue_is_hidden_when_empty():
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_queue import PromptQueueWidget

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        q = app.query_one(PromptQueueWidget)
        assert q.is_empty()
        assert not q.has_class("-busy")


@pytest.mark.asyncio
async def test_enqueue_adds_visible_row():
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_queue import PromptQueueWidget

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        q = app.query_one(PromptQueueWidget)
        q.enqueue("first")
        q.enqueue("second")
        await pilot.pause()
        items = q.items()
        assert [t for _, t in items] == ["first", "second"]
        assert q.has_class("-busy")


@pytest.mark.asyncio
async def test_pop_next_drains_oldest_first():
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_queue import PromptQueueWidget

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        q = app.query_one(PromptQueueWidget)
        q.enqueue("a")
        q.enqueue("b")
        q.enqueue("c")
        await pilot.pause()
        assert q.pop_next() == "a"
        assert q.pop_next() == "b"
        assert q.pop_next() == "c"
        assert q.pop_next() is None
        assert not q.has_class("-busy")


@pytest.mark.asyncio
async def test_cancel_removes_specific_entry():
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_queue import PromptQueueWidget

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        q = app.query_one(PromptQueueWidget)
        id_a = q.enqueue("a")
        id_b = q.enqueue("b")
        await pilot.pause()
        assert q.cancel(id_a) is True
        await pilot.pause()
        items = q.items()
        assert [t for _, t in items] == ["b"]
        assert q.cancel(99999) is False  # unknown id
        assert q.cancel(id_b) is True


@pytest.mark.asyncio
async def test_busy_dispatch_routes_to_queue_not_agent():
    """When the agent is busy, Submitted prompts queue instead of running."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_queue import PromptQueueWidget
    from aru.tui.widgets.prompt_area import PromptArea

    dispatched: list[str] = []

    class _Probe(AruApp):
        def _dispatch_user_turn(self, text: str) -> None:  # type: ignore[override]
            dispatched.append(text)

    app = _Probe()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Simulate a busy agent so the next Submitted is queued.
        app._busy = True
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("queue-me"))
        await pilot.pause()
        q = app.query_one(PromptQueueWidget)
        assert [t for _, t in q.items()] == ["queue-me"]
        assert dispatched == []
        # After turn ends, drain
        app._busy = False
        app._drain_prompt_queue()
        await pilot.pause()
        assert dispatched == ["queue-me"]
        assert q.is_empty()
