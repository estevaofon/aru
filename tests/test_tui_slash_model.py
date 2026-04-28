"""Tests for the TUI ``/model`` slash handler.

Regression for the bug where ``/model <alias>`` in the TUI kept
``session.model_ref`` raw (e.g. ``"minimax"``), which then
``resolve_model_ref`` falsely interpreted as ``("anthropic", "minimax")``
— so the status bar showed "Anthropic" and the agent crashed on the next
turn because the Anthropic client was instantiated with an unknown id.

The TUI handler must mirror the REPL handler in ``aru/cli.py``:

* resolve user-defined ``config.model_aliases``
* resolve built-in ``MODEL_ALIASES``
* validate the provider exists
* update ``ctx.model_id`` AND ``ctx.small_model_ref``
"""

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


@pytest.mark.asyncio
async def test_slash_model_resolves_user_alias_to_full_ref():
    """`/model minimax` with a config alias must rewrite session.model_ref."""
    from aru.runtime import RuntimeContext
    from aru.session import Session
    from aru.tui.app import AruApp

    session = Session()
    ctx = RuntimeContext()
    config = _Cfg(
        model_aliases={"minimax": "openrouter/minimax/minimax-m2.5"},
    )

    app = AruApp(session=session, config=config, ctx=ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._slash_model("minimax")

    assert session.model_ref == "openrouter/minimax/minimax-m2.5"
    # The display must surface OpenRouter, not Anthropic — the original bug.
    assert "openrouter" in session.model_display.lower()
    assert "anthropic" not in session.model_display.lower()
    # Runtime context must follow the session.
    assert ctx.model_id == session.model_id
    # Small model should default to the same provider family (OpenRouter
    # is not in _small_defaults so it falls back to the session ref).
    assert ctx.small_model_ref


@pytest.mark.asyncio
async def test_slash_model_rejects_unknown_provider():
    """`/model garbage/foo` must not silently corrupt session state."""
    from aru.runtime import RuntimeContext
    from aru.session import Session
    from aru.tui.app import AruApp

    session = Session()
    original_ref = session.model_ref
    ctx = RuntimeContext()
    app = AruApp(session=session, config=_Cfg(), ctx=ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._slash_model("garbage_provider/foo")

    # Unknown provider → session must stay untouched.
    assert session.model_ref == original_ref


@pytest.mark.asyncio
async def test_slash_model_resolves_legacy_alias():
    """Built-in `sonnet` alias must resolve to the full anthropic ref."""
    from aru.runtime import RuntimeContext
    from aru.session import Session
    from aru.tui.app import AruApp

    session = Session()
    ctx = RuntimeContext()
    app = AruApp(session=session, config=_Cfg(), ctx=ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._slash_model("sonnet")

    assert session.model_ref.startswith("anthropic/")
    assert "/" in session.model_ref


@pytest.mark.asyncio
async def test_slash_model_full_ref_passes_through():
    """A fully-qualified `provider/model` ref must be accepted as-is."""
    from aru.runtime import RuntimeContext
    from aru.session import Session
    from aru.tui.app import AruApp

    session = Session()
    ctx = RuntimeContext()
    app = AruApp(session=session, config=_Cfg(), ctx=ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._slash_model("anthropic/claude-haiku-4-5")

    assert session.model_ref == "anthropic/claude-haiku-4-5"


@pytest.mark.asyncio
async def test_slash_model_empty_body_lists_aliases():
    """Bare `/model` should list user aliases + built-in aliases + providers."""
    from aru.runtime import RuntimeContext
    from aru.session import Session
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane

    session = Session()
    ctx = RuntimeContext()
    config = _Cfg(model_aliases={"minimax": "openrouter/minimax/minimax-m2.5"})
    app = AruApp(session=session, config=config, ctx=ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._slash_model("")
        await pilot.pause()
        chat = app.query_one(ChatPane)
        joined = " ".join(w.buffer for w in chat.query(ChatMessageWidget))

    assert "minimax" in joined
    assert "openrouter/minimax/minimax-m2.5" in joined
    # Built-in aliases must show too.
    assert "sonnet" in joined.lower()
    # Provider list must show.
    assert "anthropic" in joined.lower()
