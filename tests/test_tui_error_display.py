"""TUI must surface LLM/runner errors instead of silencing them.

Two layers of coverage:

1. **Sink-routed errors.** When the runner's catch-all (``runner.py:693``)
   fires, it now calls ``sink.on_error(...)``. The TUI sink must mount
   a system message in the ChatPane so the user sees "Error: ...".

2. **Logging bridge.** Agno frequently catches API errors (e.g. OpenAI
   429), logs them at ERROR level, and continues without re-raising.
   In REPL the records reach the terminal via stderr; in TUI Textual
   captures stderr and the user sees nothing. The bridge installed by
   ``install_chat_log_bridge`` must convert those records into ChatPane
   system messages.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("textual")


# ── Sink-routed errors ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_textual_bus_sink_on_error_lands_in_chat_pane():
    """``TextualBusSink.on_error`` must mount a system message."""
    from aru.tui.app import AruApp
    from aru.tui.sinks import TextualBusSink
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        sink = TextualBusSink(app=app, chat_pane=chat)
        sink.on_error("Provider returned error")
        await pilot.pause()
        msgs = list(chat.query(ChatMessageWidget))
        joined = " ".join(m.buffer for m in msgs)

    assert "Provider returned error" in joined
    assert "Error" in joined
    # Must be a system-role message, not silently dropped.
    assert any(m.role == "system" for m in msgs)


# ── Logging bridge ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agno_error_log_reaches_chat_via_bridge():
    """An Agno ``logger.error(...)`` call must appear in the chat."""
    from aru.tui.app import AruApp
    from aru.tui.log_bridge import (
        install_chat_log_bridge,
        uninstall_chat_log_bridge,
    )
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane

    app = AruApp()
    handlers: list = []
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers = install_chat_log_bridge(app)
        try:
            logging.getLogger("agno").error(
                "Rate limit error from OpenAI API: Error code: 429"
            )
            await pilot.pause()
            chat = app.query_one(ChatPane)
            msgs = list(chat.query(ChatMessageWidget))
            joined = " ".join(m.buffer for m in msgs)
        finally:
            uninstall_chat_log_bridge(handlers)

    assert "429" in joined
    assert "Rate limit error" in joined


@pytest.mark.asyncio
async def test_aru_logger_error_also_bridged():
    """Aru's own loggers should also surface errors in the chat."""
    from aru.tui.app import AruApp
    from aru.tui.log_bridge import (
        install_chat_log_bridge,
        uninstall_chat_log_bridge,
    )
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers = install_chat_log_bridge(app)
        try:
            logging.getLogger("aru.runner").error("runner blew up: %s", "boom")
            await pilot.pause()
            chat = app.query_one(ChatPane)
            joined = " ".join(
                m.buffer for m in chat.query(ChatMessageWidget)
            )
        finally:
            uninstall_chat_log_bridge(handlers)

    assert "boom" in joined


@pytest.mark.asyncio
async def test_warning_level_records_are_filtered_out():
    """Bridge floor is ERROR; routine WARNINGs should not pollute chat."""
    from aru.tui.app import AruApp
    from aru.tui.log_bridge import (
        install_chat_log_bridge,
        uninstall_chat_log_bridge,
    )
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers = install_chat_log_bridge(app)
        try:
            logging.getLogger("agno").warning("schema coerced to dict")
            await pilot.pause()
            chat = app.query_one(ChatPane)
            joined = " ".join(
                m.buffer for m in chat.query(ChatMessageWidget)
            )
        finally:
            uninstall_chat_log_bridge(handlers)

    assert "schema coerced" not in joined


@pytest.mark.asyncio
async def test_install_is_idempotent():
    """Re-installing the bridge must not double-attach handlers."""
    from aru.tui.app import AruApp
    from aru.tui.log_bridge import (
        _ChatPaneLogHandler,
        install_chat_log_bridge,
        uninstall_chat_log_bridge,
    )

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        first = install_chat_log_bridge(app)
        second = install_chat_log_bridge(app)
        try:
            agno_handlers = [
                h for h in logging.getLogger("agno").handlers
                if isinstance(h, _ChatPaneLogHandler)
            ]
            # Exactly one bridge handler attached, regardless of install count.
            assert len(agno_handlers) == 1
            assert second == []  # second call returned no new handlers
        finally:
            uninstall_chat_log_bridge(first)
            uninstall_chat_log_bridge(second)


@pytest.mark.asyncio
async def test_uninstall_removes_handlers():
    """After uninstall, ERROR records must not leak into chat anymore."""
    from aru.tui.app import AruApp
    from aru.tui.log_bridge import (
        _ChatPaneLogHandler,
        install_chat_log_bridge,
        uninstall_chat_log_bridge,
    )

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers = install_chat_log_bridge(app)
        uninstall_chat_log_bridge(handlers)
        # No bridge handler should remain.
        for name in ("agno", "aru"):
            for h in logging.getLogger(name).handlers:
                assert not isinstance(h, _ChatPaneLogHandler)


# ── Rich sink continues to work for REPL ─────────────────────────────


def test_rich_live_sink_on_error_prints_via_console():
    """REPL path: the existing console.print behavior is preserved."""
    from io import StringIO

    from rich.console import Console

    from aru.sinks import RichLiveSink

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, color_system=None, width=120)
    sink = RichLiveSink(console=console)
    sink.on_error("Provider returned error")
    out = buf.getvalue()
    assert "Provider returned error" in out
    assert "Error" in out
