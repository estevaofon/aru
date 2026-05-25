"""Wiring test: ``/connect`` in the TUI routes to ``_slash_connect`` and is a
registered local slash command (so it never gets forwarded to the agent)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

pytest.importorskip("textual")


@dataclass
class _Cfg:
    model_aliases: dict = field(default_factory=dict)
    commands: dict = field(default_factory=dict)
    custom_agents: dict = field(default_factory=dict)
    skills: dict = field(default_factory=dict)


def test_connect_is_a_local_slash():
    from aru.tui.app import AruApp

    assert "connect" in AruApp._LOCAL_SLASH


@pytest.mark.asyncio
async def test_slash_connect_dispatch_routes_with_args():
    from aru.runtime import RuntimeContext
    from aru.session import Session
    from aru.tui.app import AruApp

    app = AruApp(session=Session(), config=_Cfg(), ctx=RuntimeContext())
    async with app.run_test() as pilot:
        await pilot.pause()
        calls: list[str] = []
        app._slash_connect = lambda body: calls.append(body)  # type: ignore[method-assign]

        handled = app._maybe_run_local_slash("/connect anthropic")

        assert handled is True
        assert calls == ["anthropic"]
