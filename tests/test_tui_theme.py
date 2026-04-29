"""Tests for the /theme slash command and Aru-alias resolution."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")


def test_resolve_theme_alias_short_to_canonical():
    from aru.tui.themes import resolve_theme

    assert resolve_theme("dark") == "textual-dark"
    assert resolve_theme("light") == "textual-light"
    assert resolve_theme("solarized") == "solarized-dark"
    assert resolve_theme("DARK") == "textual-dark"  # case-insensitive
    assert resolve_theme("  dark ") == "textual-dark"  # trimmed
    assert resolve_theme("") is None
    # Canonical names pass through.
    assert resolve_theme("nord") == "nord"


@pytest.mark.asyncio
async def test_apply_theme_switches_app_theme_attribute():
    """``apply_theme`` must mutate ``App.theme`` (the reactive attr) —
    that's how Textual triggers a repaint of every widget."""
    from aru.tui.app import AruApp
    from aru.tui.themes import apply_theme

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        before = app.theme
        ok = apply_theme(app, "dracula")
        assert ok is True
        assert app.theme == "dracula"
        assert app.theme != before


@pytest.mark.asyncio
async def test_apply_theme_unknown_returns_false_and_no_change():
    from aru.tui.app import AruApp
    from aru.tui.themes import apply_theme

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        before = app.theme
        ok = apply_theme(app, "no-such-theme")
        assert ok is False
        assert app.theme == before


@pytest.mark.asyncio
async def test_slash_theme_lists_available_when_no_arg():
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
    from aru.tui.widgets.prompt_area import PromptArea

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("/theme"))
        await pilot.pause()
        chat = app.query_one(ChatPane)
        msgs = list(chat.query(ChatMessageWidget))
        joined = " ".join(m.buffer for m in msgs)
    assert "Available themes" in joined
    assert "dracula" in joined
    assert "Active:" in joined


@pytest.mark.asyncio
async def test_slash_theme_switches_live():
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_area import PromptArea

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("/theme nord"))
        await pilot.pause()
    assert app.theme == "nord"
