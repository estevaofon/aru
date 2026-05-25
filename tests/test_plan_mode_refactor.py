"""Tests for the plan-mode refactor.

Covers the new flag-flip design where `enter_plan_mode` mutates
`session.plan_mode` instead of spawning a nested planner runner, and
`exit_plan_mode` handles the approval flow.

Scope:
1. Session persistence — plan_mode round-trips through to_dict/from_dict
   and old sessions without the field load cleanly as False.
2. enter_plan_mode — flips the flag, returns instructions, is idempotent.
3. exit_plan_mode — requires the flag, requires a non-empty plan,
   clears the flag on approval, preserves it on rejection.
4. Plan-mode gate in the tool wrapper — mutating tools short-circuit
   with BLOCKED; read-only tools pass through untouched.
5. /undo-style clear — session.plan_mode = False resets the gate.
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

import pytest

from aru.session import Session


# ── Session persistence ───────────────────────────────────────────────

class TestPlanModePersistence:
    """plan_mode must survive serialization and degrade gracefully for
    older sessions that never wrote the field."""

    def test_default_is_false(self):
        s = Session()
        assert s.plan_mode is False

    def test_to_dict_from_dict_roundtrip_true(self):
        s = Session(session_id="test-roundtrip")
        s.plan_mode = True
        data = s.to_dict()
        assert data["plan_mode"] is True
        restored = Session.from_dict(data)
        assert restored.plan_mode is True

    def test_to_dict_from_dict_roundtrip_false(self):
        s = Session(session_id="test-off")
        data = s.to_dict()
        assert data["plan_mode"] is False
        restored = Session.from_dict(data)
        assert restored.plan_mode is False

    def test_legacy_session_without_field_loads_false(self):
        """Sessions saved before plan_mode existed must load as False."""
        legacy_data = {
            "session_id": "legacy",
            "history": [],
            "current_plan": None,
            "plan_task": None,
            "plan_steps": [],
            "model_ref": "anthropic/claude-sonnet-4-5",
            "cwd": "/tmp",
            "created_at": "",
            "updated_at": "",
            # Note: no "plan_mode" key at all
        }
        s = Session.from_dict(legacy_data)
        assert s.plan_mode is False


# ── enter_plan_mode tool ──────────────────────────────────────────────

class TestEnterPlanMode:
    """enter_plan_mode must be a cheap flag flip with no side effects
    beyond session mutation. It must NOT invoke a nested runner."""

    @pytest.mark.asyncio
    async def test_flips_flag_and_returns_instructions(self):
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx
        from aru.tools.plan_mode import enter_plan_mode

        session = Session()
        ctx = RuntimeContext()
        ctx.session = session
        token = _ctx.set(ctx)
        try:
            result = await enter_plan_mode()
        finally:
            _ctx.reset(token)

        assert session.plan_mode is True
        assert "Plan mode active" in result
        assert "exit_plan_mode" in result
        # Critical: no nested runner invoked. If it had been, the result
        # would reference a plan the planner generated.
        assert "Mutating tools" in result

    @pytest.mark.asyncio
    async def test_is_idempotent(self):
        """Calling enter_plan_mode twice is safe — no exception, same state."""
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx
        from aru.tools.plan_mode import enter_plan_mode

        session = Session()
        ctx = RuntimeContext()
        ctx.session = session
        token = _ctx.set(ctx)
        try:
            await enter_plan_mode()
            result2 = await enter_plan_mode()
        finally:
            _ctx.reset(token)

        assert session.plan_mode is True
        assert "Already in plan mode" in result2

    @pytest.mark.asyncio
    async def test_clears_stale_plan_steps(self):
        """Entering plan mode wipes any stale plan_steps from a prior plan."""
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx
        from aru.tools.plan_mode import enter_plan_mode

        session = Session()
        session.set_plan(task="old", plan_content="## Steps\n1. old step\n2. another\n")
        assert len(session.plan_steps) > 0

        ctx = RuntimeContext()
        ctx.session = session
        token = _ctx.set(ctx)
        try:
            await enter_plan_mode()
        finally:
            _ctx.reset(token)

        assert session.plan_mode is True
        assert session.plan_steps == []

    @pytest.mark.asyncio
    async def test_no_session_returns_error(self):
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx
        from aru.tools.plan_mode import enter_plan_mode

        ctx = RuntimeContext()
        ctx.session = None
        token = _ctx.set(ctx)
        try:
            result = await enter_plan_mode()
        finally:
            _ctx.reset(token)
        assert "Error" in result


# ── exit_plan_mode tool ───────────────────────────────────────────────

class TestExitPlanMode:
    """exit_plan_mode handles approval. On approval, the flag clears;
    on rejection, the flag stays on so the agent can revise."""

    @pytest.mark.asyncio
    async def test_requires_plan_mode_active(self):
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx
        from aru.tools.plan_mode import exit_plan_mode

        session = Session()  # plan_mode defaults to False
        ctx = RuntimeContext()
        ctx.session = session
        token = _ctx.set(ctx)
        try:
            result = await exit_plan_mode(plan="## Steps\n1. do thing")
        finally:
            _ctx.reset(token)
        assert "Error" in result
        assert "not in plan mode" in result

    @pytest.mark.asyncio
    async def test_requires_non_empty_plan(self):
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx
        from aru.tools.plan_mode import exit_plan_mode

        session = Session()
        session.plan_mode = True
        ctx = RuntimeContext()
        ctx.session = session
        token = _ctx.set(ctx)
        try:
            result = await exit_plan_mode(plan="   ")
        finally:
            _ctx.reset(token)
        assert "Error" in result
        assert "non-empty plan" in result
        # Flag stays on because we didn't actually exit
        assert session.plan_mode is True

    @pytest.mark.asyncio
    async def test_approval_clears_flag(self):
        """Non-interactive mode (no TTY) auto-approves."""
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx
        from aru.tools.plan_mode import exit_plan_mode

        session = Session()
        session.plan_mode = True
        ctx = RuntimeContext()
        ctx.session = session
        token = _ctx.set(ctx)

        plan_text = "## Goal\nTest\n\n## Steps\n1. First step\n2. Second step\n"
        # Force non-interactive path so _prompt_plan_approval auto-approves
        with patch.object(sys.stdin, "isatty", return_value=False):
            try:
                result = await exit_plan_mode(plan=plan_text)
            finally:
                _ctx.reset(token)

        assert session.plan_mode is False
        assert "approved" in result.lower()
        assert "Plan mode is now OFF" in result

    @pytest.mark.asyncio
    async def test_rejection_preserves_flag(self):
        """Rejection keeps plan_mode active so the agent can revise."""
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx
        from aru.tools import plan_mode as plan_mode_module

        session = Session()
        session.plan_mode = True
        ctx = RuntimeContext()
        ctx.session = session
        token = _ctx.set(ctx)

        # Patch the approval prompt directly to simulate user rejection
        with patch.object(
            plan_mode_module,
            "_prompt_plan_approval",
            return_value=(False, "needs more detail"),
        ):
            try:
                result = await plan_mode_module.exit_plan_mode(
                    plan="## Steps\n1. do thing\n2. other thing"
                )
            finally:
                _ctx.reset(token)

        assert session.plan_mode is True, "rejection must not clear the flag"
        assert "rejected" in result.lower()
        assert "needs more detail" in result
        assert "STILL in plan mode" in result

    @pytest.mark.asyncio
    async def test_approval_prompt_runs_off_event_loop_thread(self):
        """Regression: the approval prompt must run on a worker thread, never
        the event-loop thread.

        ``exit_plan_mode`` is an async tool awaited directly on the loop. In
        TUI mode ``_prompt_plan_approval`` reaches ``TuiUI.ask_choice`` →
        ``App.call_from_thread``, which raises "must run in a different
        thread from the app" when called on the loop thread. The fix hops to
        a worker via ``asyncio.to_thread``; this test asserts the prompt
        observes a thread id different from the loop's. Before the fix the
        ids matched and TUI users hit the RuntimeError.
        """
        import threading

        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx
        from aru.tools import plan_mode as plan_mode_module

        loop_thread_id = threading.get_ident()
        captured: dict = {}

        def _fake_prompt(plan_steps, n_steps):
            # Runs inside exit_plan_mode's approval call. Record the thread.
            captured["thread_id"] = threading.get_ident()
            # Also prove get_ctx() still resolves across the to_thread hop.
            from aru.runtime import get_ctx
            captured["has_ctx_session"] = get_ctx().session is not None
            return (True, "")

        session = Session()
        session.plan_mode = True
        ctx = RuntimeContext()
        ctx.session = session
        token = _ctx.set(ctx)
        with patch.object(plan_mode_module, "_prompt_plan_approval", _fake_prompt):
            try:
                result = await plan_mode_module.exit_plan_mode(
                    plan="## Steps\n1. do thing\n2. other thing"
                )
            finally:
                _ctx.reset(token)

        assert "thread_id" in captured, "approval prompt was never invoked"
        assert captured["thread_id"] != loop_thread_id, (
            "approval prompt ran on the event-loop thread — "
            "TuiUI.ask_choice's call_from_thread would raise in TUI mode"
        )
        assert captured["has_ctx_session"], (
            "contextvars must propagate across asyncio.to_thread so the "
            "prompt can still read ctx.session"
        )
        assert session.plan_mode is False
        assert "approved" in result.lower()


# ── Tool-wrapper plan-mode gate ───────────────────────────────────────

class TestPlanModeGate:
    """The wrapper in agent_factory must short-circuit mutating tools
    with a BLOCKED message when session.plan_mode is True. Read-only
    tools must pass through untouched."""

    @pytest.mark.asyncio
    async def test_mutating_tool_is_blocked(self):
        from aru.agent_factory import _wrap_tools_with_hooks
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx

        async def edit_file(file_path: str, old_string: str, new_string: str):
            return "EXECUTED — should not reach here"

        [wrapped] = _wrap_tools_with_hooks([edit_file])

        session = Session()
        session.plan_mode = True
        ctx = RuntimeContext()
        ctx.session = session
        token = _ctx.set(ctx)
        try:
            result = await wrapped(
                file_path="x.py", old_string="a", new_string="b",
            )
        finally:
            _ctx.reset(token)

        assert "BLOCKED" in result
        assert "plan mode" in result
        assert "exit_plan_mode" in result
        assert "EXECUTED" not in result

    @pytest.mark.asyncio
    async def test_all_blocked_tools_are_gated(self):
        """Verify every tool in _PLAN_MODE_BLOCKED_TOOLS is actually blocked."""
        from aru.agent_factory import _PLAN_MODE_BLOCKED_TOOLS, _wrap_tools_with_hooks
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx

        async def make_fn(name):
            async def _fn(**kwargs):
                return "EXECUTED"
            _fn.__name__ = name
            return _fn

        session = Session()
        session.plan_mode = True
        ctx = RuntimeContext()
        ctx.session = session

        for tool_name in _PLAN_MODE_BLOCKED_TOOLS:
            fn = await make_fn(tool_name)
            [wrapped] = _wrap_tools_with_hooks([fn])
            token = _ctx.set(ctx)
            try:
                result = await wrapped()
            finally:
                _ctx.reset(token)
            assert "BLOCKED" in result, f"{tool_name} was not blocked"
            assert tool_name in result, f"{tool_name} not named in error"

    @pytest.mark.asyncio
    async def test_read_only_tool_passes_through(self):
        """read_file-like tools must NOT be blocked by plan mode."""
        from aru.agent_factory import _wrap_tools_with_hooks
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx

        async def read_file(file_path: str):
            return f"CONTENT OF {file_path}"

        [wrapped] = _wrap_tools_with_hooks([read_file])

        session = Session()
        session.plan_mode = True  # gate is ON but read_file should still work
        ctx = RuntimeContext()
        ctx.session = session
        token = _ctx.set(ctx)
        try:
            result = await wrapped(file_path="x.py")
        finally:
            _ctx.reset(token)

        assert result == "CONTENT OF x.py"
        assert "BLOCKED" not in result

    @pytest.mark.asyncio
    async def test_plan_mode_off_allows_mutating_tools(self):
        """Sanity check: gate only fires when plan_mode is True."""
        from aru.agent_factory import _wrap_tools_with_hooks
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx

        async def edit_file(**kwargs):
            return "EXECUTED"

        [wrapped] = _wrap_tools_with_hooks([edit_file])

        session = Session()
        session.plan_mode = False
        ctx = RuntimeContext()
        ctx.session = session
        token = _ctx.set(ctx)
        try:
            result = await wrapped(file_path="x", old_string="a", new_string="b")
        finally:
            _ctx.reset(token)

        assert result == "EXECUTED"

    @pytest.mark.asyncio
    async def test_gate_survives_missing_session(self):
        """If ctx has no session, the gate should not raise — fall through."""
        from aru.agent_factory import _wrap_tools_with_hooks
        from aru.runtime import RuntimeContext, _runtime_ctx as _ctx

        async def edit_file(**kwargs):
            return "EXECUTED"

        [wrapped] = _wrap_tools_with_hooks([edit_file])

        ctx = RuntimeContext()
        ctx.session = None
        token = _ctx.set(ctx)
        try:
            result = await wrapped()
        finally:
            _ctx.reset(token)

        assert result == "EXECUTED"


# ── Registry integration ──────────────────────────────────────────────

class TestRegistryExposure:
    """Both tools must be reachable via the registry so agents actually get them."""

    def test_enter_plan_mode_registered(self):
        from aru.tools.registry import TOOL_REGISTRY
        assert "enter_plan_mode" in TOOL_REGISTRY

    def test_exit_plan_mode_registered(self):
        from aru.tools.registry import TOOL_REGISTRY
        assert "exit_plan_mode" in TOOL_REGISTRY

    def test_both_in_general_tools(self):
        from aru.tools.registry import GENERAL_TOOLS
        names = {getattr(t, "__name__", "") for t in GENERAL_TOOLS}
        assert "enter_plan_mode" in names
        assert "exit_plan_mode" in names


# ── Auto plan-approval safety net ────────────────────────────────────

class TestExtractAssistantText:
    """_extract_assistant_text recovers the plan from structured blocks."""

    def test_single_text_block(self):
        from aru.runner import _extract_assistant_text
        blocks = [{"type": "text", "text": "## Goal\nTest plan"}]
        assert _extract_assistant_text(blocks) == "## Goal\nTest plan"

    def test_multiple_text_blocks_joined(self):
        from aru.runner import _extract_assistant_text
        blocks = [
            {"type": "text", "text": "## Goal"},
            {"type": "text", "text": "Do the thing"},
        ]
        assert _extract_assistant_text(blocks) == "## Goal\nDo the thing"

    def test_ignores_tool_use_blocks(self):
        from aru.runner import _extract_assistant_text
        blocks = [
            {"type": "text", "text": "Plan:"},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}},
            {"type": "text", "text": "Step 1"},
        ]
        assert _extract_assistant_text(blocks) == "Plan:\nStep 1"

    def test_empty_blocks(self):
        from aru.runner import _extract_assistant_text
        assert _extract_assistant_text([]) == ""

    def test_skips_empty_text(self):
        from aru.runner import _extract_assistant_text
        blocks = [
            {"type": "text", "text": ""},
            {"type": "text", "text": "real"},
        ]
        assert _extract_assistant_text(blocks) == "real"


class TestPlanRejectionFeedback:
    """_consume_plan_rejection_feedback reads once and clears."""

    def test_consume_returns_feedback_and_clears(self):
        from aru.runner import _consume_plan_rejection_feedback

        session = Session()
        session._plan_rejection_feedback = "add more detail"

        result = _consume_plan_rejection_feedback(session)
        assert result == "add more detail"
        # Consumed — second call returns None
        assert _consume_plan_rejection_feedback(session) is None
        assert session._plan_rejection_feedback is None

    def test_consume_when_none(self):
        from aru.runner import _consume_plan_rejection_feedback
        session = Session()
        assert _consume_plan_rejection_feedback(session) is None


class TestPlanReminderSurfacesFeedback:
    """Plan reminder must inject rejection feedback so the agent sees it."""

    def test_reminder_includes_feedback_plan_mode_no_steps(self):
        from aru.runner import _build_plan_reminder

        session = Session()
        session.plan_mode = True
        session._plan_rejection_feedback = "the goal is wrong"

        reminder = _build_plan_reminder(session)
        assert reminder is not None
        assert "REJECTED" in reminder
        assert "the goal is wrong" in reminder
        # Consumed after rendering
        assert session._plan_rejection_feedback is None

    def test_reminder_includes_feedback_with_steps(self):
        from aru.runner import _build_plan_reminder

        session = Session()
        session.plan_mode = True
        session.set_plan(
            task="test",
            plan_content="## Steps\n1. first\n2. second\n",
        )
        session._plan_rejection_feedback = "step 2 is wrong"

        reminder = _build_plan_reminder(session)
        assert reminder is not None
        assert "step 2 is wrong" in reminder
        assert session._plan_rejection_feedback is None

    def test_no_feedback_no_rejection_text(self):
        from aru.runner import _build_plan_reminder

        session = Session()
        session.plan_mode = True
        reminder = _build_plan_reminder(session)
        assert reminder is not None
        assert "REJECTED" not in reminder
