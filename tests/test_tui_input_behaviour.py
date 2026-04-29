"""Tests for the multi-line PromptArea input behaviour."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_user_message_persists_to_session_history():
    """Plain-text messages must land in ``session.history`` as user turns.

    Regression guard: before fix/tui-freezing2, ``_dispatch_user_turn``
    did not call ``session.add_message("user", ...)`` (the REPL did, but
    the TUI forwarded straight to ``run_agent_capture_tui``). Session
    files wrote back only ``assistant`` + ``tool`` turns, so a reloaded
    session had no user context and follow-up prompts like ``continue``
    left the agent thinking for a tick and halting — no user side to
    reason against.
    """
    from aru.tui.app import AruApp
    from aru.session import Session

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.session = Session(session_id="test-persist")
        assert app.session.history == []

        app._dispatch_user_turn("hello world")

        user_msgs = [m for m in app.session.history if m.get("role") == "user"]
        assert len(user_msgs) == 1
        blocks = user_msgs[0]["content"]
        text = "".join(
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )
        assert text == "hello world"


@pytest.mark.asyncio
async def test_multiple_user_turns_accumulate_in_history():
    """Successive ``_dispatch_user_turn`` calls all append — no overwrite."""
    from aru.tui.app import AruApp
    from aru.session import Session

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.session = Session(session_id="test-persist-multi")

        for msg in ("first", "second", "continue"):
            app._dispatch_user_turn(msg)

        user_msgs = [m for m in app.session.history if m.get("role") == "user"]
        assert len(user_msgs) == 3
        plain_texts = []
        for m in user_msgs:
            plain_texts.append(
                "".join(
                    b.get("text", "")
                    for b in m["content"]
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            )
        assert plain_texts == ["first", "second", "continue"]


@pytest.mark.asyncio
async def test_slash_help_handled_locally():
    """`/help` prints help inline — does NOT dispatch to the agent."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
    from aru.tui.widgets.prompt_area import PromptArea

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.value = "/help"
        inp.post_message(PromptArea.Submitted("/help"))
        await pilot.pause()
        chat = app.query_one(ChatPane)
        msgs = list(chat.query(ChatMessageWidget))
        joined = " ".join(m.buffer for m in msgs)
        assert "local commands" in joined.lower() or "shortcuts" in joined.lower()
    assert app._busy is False


@pytest.mark.asyncio
async def test_slash_clear_clears_chat():
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane
    from aru.tui.widgets.prompt_area import PromptArea

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        chat.add_user_message("one")
        chat.add_user_message("two")
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted("/clear"))
        await pilot.pause()
        msgs = list(chat.query(ChatMessageWidget))
        # Only the "Chat cleared" system message remains.
        assert len(msgs) == 1
        assert "cleared" in msgs[0].buffer.lower()


@pytest.mark.asyncio
async def test_unknown_slash_falls_through_to_agent_queue():
    """A slash command we don't handle locally should NOT be eaten."""
    from aru.tui.app import AruApp

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Directly test _maybe_run_local_slash — returns False = not handled.
        handled = app._maybe_run_local_slash("/mystery")
        assert handled is False


@pytest.mark.asyncio
async def test_multiline_paste_lands_in_visible_buffer():
    """Multi-line paste goes straight into the PromptArea where the user
    can edit it before submitting.

    The previous single-line ``PromptInput`` had to stash the paste
    invisibly because it couldn't render newlines; the new ``TextArea``
    drops it directly into the visible text. No more ``_pending_paste``
    state — the buffer IS the source of truth.
    """
    from textual import events

    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_area import PromptArea

    app = AruApp()
    pasted = "line one\nline two\nline three"
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.focus()
        await pilot.pause()
        inp.post_message(events.Paste(text=pasted))
        await pilot.pause()
        # Whatever the user pastes is what they see and submit.
        assert pasted in inp.value


@pytest.mark.asyncio
async def test_submitted_carries_full_multiline_text():
    """``PromptArea.Submitted`` payload preserves embedded newlines."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatPane
    from aru.tui.widgets.prompt_area import PromptArea

    captured: dict = {}

    class _Probe(AruApp):
        def _dispatch_user_turn(self, text: str) -> None:  # type: ignore[override]
            captured["text"] = text
            self.query_one(ChatPane).add_user_message(text)

    app = _Probe()
    multiline = "first line\nsecond line\nthird line"
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.post_message(PromptArea.Submitted(multiline))
        await pilot.pause()
    assert captured["text"] == multiline


@pytest.mark.asyncio
async def test_ctrl_j_inserts_newline_instead_of_submitting():
    """Ctrl+J is the universal newline fallback for terminals that drop
    the shift modifier on Enter (Windows Terminal, conhost, Git Bash).

    Pressing Ctrl+J must insert a literal LF at the cursor and must NOT
    fire ``PromptArea.Submitted``.
    """
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_area import PromptArea

    app = AruApp()
    submitted: list[str] = []

    class _Probe(AruApp):
        def _dispatch_user_turn(self, text: str) -> None:  # type: ignore[override]
            submitted.append(text)

    app = _Probe()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.focus()
        await pilot.pause()
        inp.value = "first"
        await pilot.pause()
        # Move cursor to end of buffer so insert lands after "first".
        try:
            inp.move_cursor((0, len("first")))
        except Exception:
            pass
        await pilot.press("ctrl+j")
        await pilot.pause()
        # Newline inserted; no Submitted dispatched.
        assert "\n" in inp.value, f"expected newline, got {inp.value!r}"
        assert submitted == [], f"unexpected submit: {submitted}"


@pytest.mark.asyncio
async def test_trailing_backslash_enter_inserts_newline_not_submit():
    """`\\<Enter>` inserts a newline and strips the backslash.

    Mirrors shell line continuation. Gives users a one-handed way to
    compose multi-line prompts when the terminal silently drops
    shift+enter.
    """
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_area import PromptArea

    submitted: list[str] = []

    class _Probe(AruApp):
        def _dispatch_user_turn(self, text: str) -> None:  # type: ignore[override]
            submitted.append(text)

    app = _Probe()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.focus()
        await pilot.pause()
        inp.value = "first\\"
        try:
            inp.move_cursor((0, len("first\\")))
        except Exception:
            pass
        await pilot.pause()
        inp.action_submit_prompt()
        await pilot.pause()
        # The trailing ``\`` was consumed and replaced by a newline.
        assert inp.value == "first\n", f"got {inp.value!r}"
        assert submitted == [], f"unexpected submit: {submitted}"


@pytest.mark.asyncio
async def test_double_backslash_still_submits():
    """`\\\\<Enter>` (two backslashes) means the user wants a literal
    backslash at the end of the message — submit normally."""
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_area import PromptArea

    submitted: list[str] = []

    class _Probe(AruApp):
        def _dispatch_user_turn(self, text: str) -> None:  # type: ignore[override]
            submitted.append(text)

    app = _Probe()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        inp.value = "ends with \\\\"
        try:
            inp.move_cursor((0, len("ends with \\\\")))
        except Exception:
            pass
        await pilot.pause()
        inp.action_submit_prompt()
        await pilot.pause()
    assert submitted == ["ends with \\\\"]


@pytest.mark.asyncio
async def test_history_up_down_cycles_submitted_inputs():
    from aru.tui.app import AruApp
    from aru.tui.widgets.prompt_area import PromptArea

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one(PromptArea)
        # Simulate two submits to populate history (use /clear so no agent runs)
        inp.post_message(PromptArea.Submitted("/clear"))
        await pilot.pause()
        inp.post_message(PromptArea.Submitted("/help"))
        await pilot.pause()
        assert app._history == ["/clear", "/help"]
        assert app._history_cursor is None
        # Simulate Up → should recall the latest entry
        inp.focus()
        await pilot.pause()
        app.action_history_prev()
        await pilot.pause()
        assert inp.value == "/help"
        app.action_history_prev()
        await pilot.pause()
        assert inp.value == "/clear"
        app.action_history_next()
        await pilot.pause()
        assert inp.value == "/help"
        app.action_history_next()
        await pilot.pause()
        # Past last entry — returns to empty.
        assert inp.value == ""
