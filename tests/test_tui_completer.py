"""Tests for SlashCompleter (E6c full)."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_completer_opens_on_slash_and_filters():
    from aru.tui.app import AruApp
    from aru.tui.widgets.completer import SlashCompleter

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        completer = app.query_one(SlashCompleter)
        completer.update_for("/he")
        await pilot.pause()
        assert completer.is_open() is True
        # Filtered to /help only.
        from textual.widgets import OptionList
        opts = completer.query_one(OptionList)
        ids = [opts.get_option_at_index(i).id for i in range(opts.option_count)]
        assert "help" in ids


@pytest.mark.asyncio
async def test_completer_closes_for_plain_text():
    from aru.tui.app import AruApp
    from aru.tui.widgets.completer import SlashCompleter

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        completer = app.query_one(SlashCompleter)
        completer.update_for("just chatting")
        await pilot.pause()
        assert completer.is_open() is False


@pytest.mark.asyncio
async def test_completer_accept_returns_injected_text():
    from aru.tui.app import AruApp
    from aru.tui.widgets.completer import SlashCompleter
    from textual.widgets import OptionList

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        completer = app.query_one(SlashCompleter)
        completer.update_for("/hel")
        await pilot.pause()
        opts = completer.query_one(OptionList)
        opts.highlighted = 0
        accepted = completer.accept()
        assert accepted == "/help "


@pytest.mark.asyncio
async def test_completer_moves_through_options():
    from aru.tui.app import AruApp
    from aru.tui.widgets.completer import SlashCompleter
    from textual.widgets import OptionList

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        completer = app.query_one(SlashCompleter)
        completer.update_for("/")
        await pilot.pause()
        opts = completer.query_one(OptionList)
        assert opts.option_count > 1
        opts.highlighted = 0
        completer.move_down()
        assert opts.highlighted == 1
        completer.move_up()
        assert opts.highlighted == 0
        # Wraps
        completer.move_up()
        assert opts.highlighted == opts.option_count - 1


@pytest.mark.asyncio
async def test_completer_at_prefix_shows_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "foo.py").write_text("x")
    (tmp_path / "bar.md").write_text("y")

    from aru.tui.app import AruApp
    from aru.tui.widgets.completer import SlashCompleter
    from textual.widgets import OptionList

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        completer = app.query_one(SlashCompleter)
        completer.update_for("@")
        await pilot.pause()
        opts = completer.query_one(OptionList)
        ids = [opts.get_option_at_index(i).id for i in range(opts.option_count)]
        assert "foo.py" in ids
        assert "bar.md" in ids


@pytest.mark.asyncio
async def test_app_dispatches_local_slash_cost():
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
    from aru.tui.widgets.prompt_area import PromptArea

    # Provide a minimal session so cost_summary works.
    from aru.session import Session
    session = Session()

    app = AruApp(session=session)
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("/cost"))
        await pilot.pause(0.1)
        chat = app.query_one(ChatPane)
        text = " ".join(m.buffer for m in chat.query(ChatMessageWidget))
        assert "/cost" in text


@pytest.mark.asyncio
async def test_app_dispatches_local_slash_model_no_args():
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
    from aru.tui.widgets.prompt_area import PromptArea
    from aru.session import Session

    app = AruApp(session=Session())
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("/model"))
        await pilot.pause(0.1)
        chat = app.query_one(ChatPane)
        text = " ".join(m.buffer for m in chat.query(ChatMessageWidget))
        assert "Current model" in text or "model" in text.lower()


@pytest.mark.asyncio
async def test_app_dispatches_local_slash_model_switch():
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
    from aru.session import Session
    from aru.tui.widgets.prompt_area import PromptArea

    session = Session()
    app = AruApp(session=session)
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("/model anthropic/claude-haiku-4-5"))
        await pilot.pause(0.1)
        assert session.model_ref == "anthropic/claude-haiku-4-5"


@pytest.mark.asyncio
async def test_enter_submits_when_completer_open_does_not_eat_enter():
    """Regression: typing /help + Enter should run /help on the first Enter.

    The old on_key intercepted Enter to accept the suggestion in addition
    to Input.Submitted firing, so the user had to press Enter three times
    before the command ran. Tab is now the only accept key.
    """
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
    from aru.tui.widgets.completer import SlashCompleter
    from aru.tui.widgets.prompt_area import PromptArea

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        completer = app.query_one(SlashCompleter)
        # Simulate typing /help — completer opens.
        inp.value = "/help"
        completer.update_for("/help")
        await pilot.pause()
        assert completer.is_open() is True
        # Single Enter should submit and run /help.
        inp.post_message(PromptArea.Submitted("/help"))
        await pilot.pause(0.1)
        chat = app.query_one(ChatPane)
        text = " ".join(m.buffer for m in chat.query(ChatMessageWidget))
        # /help adds a system message with "local commands" in it
        assert "local commands" in text.lower() or "shortcuts" in text.lower()


@pytest.mark.asyncio
async def test_tab_accepts_completer_suggestion():
    from aru.tui.app import AruApp
    from aru.tui.widgets.completer import SlashCompleter
    from aru.tui.widgets.prompt_area import PromptArea
    from textual.widgets import OptionList

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.focus()
        inp.value = "/he"
        completer = app.query_one(SlashCompleter)
        completer.update_for("/he")
        await pilot.pause()
        opts = completer.query_one(OptionList)
        opts.highlighted = 0
        # Press Tab — should accept and fill "/help ".
        await pilot.press("tab")
        await pilot.pause()
        assert inp.value == "/help "
        assert completer.is_open() is False


@pytest.mark.asyncio
async def test_app_dispatches_local_slash_agents_empty():
    from dataclasses import dataclass, field
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
    from aru.tui.widgets.prompt_area import PromptArea

    @dataclass
    class _Cfg:
        custom_agents: dict = field(default_factory=dict)

    app = AruApp(config=_Cfg())
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("/agents"))
        await pilot.pause(0.1)
        chat = app.query_one(ChatPane)
        text = " ".join(m.buffer for m in chat.query(ChatMessageWidget))
        assert "custom agents" in text.lower() or "no custom" in text.lower()
