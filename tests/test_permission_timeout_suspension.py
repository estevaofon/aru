"""Regression: a tool blocked on a permission decision must NOT time out.

The bug (branch ``fix/skipping-permission``): mutating file tools run their
``check_permission`` prompt *inside* a worker thread wrapped by
``_thread_tool(timeout=60)``. ``asyncio.to_thread`` cannot abort that thread,
so when the user took longer than the timeout to answer, the tool reported a
timeout to the model — but the orphaned worker thread stayed parked on the
prompt and applied the edit the instant the user finally clicked "yes". The
mutation landed out-of-band, after the tool already claimed it timed out. For
a delete-capable tool that is catastrophic.

The fix: ``check_permission`` marks a per-call gate while it blocks on the
user (``aru.runtime.PermissionWaitGate``) and ``_thread_tool`` suspends its
timeout for as long as the gate is active.

These tests use a tiny timeout (0.2s) and a human "decision" several times
longer, so the OLD code would always time out here and the NEW code must not.
"""

from __future__ import annotations

import time

import pytest

import aru.tools.file_ops as file_ops
from aru.runtime import begin_permission_wait, end_permission_wait
from aru.tools._shared import _thread_tool


# ── Unit level: the gate suspends the _thread_tool timeout ───────────


@pytest.mark.asyncio
async def test_timeout_suspended_while_permission_pending():
    """A long human decision must not trip the timeout, and the work that
    follows the approval must complete and be returned (not discarded)."""

    def slow_permission_then_work() -> str:
        # Stand in for check_permission blocking on a human for 3x the
        # timeout budget, then the post-approval write completing.
        begin_permission_wait()
        try:
            time.sleep(0.6)
        finally:
            end_permission_wait()
        return "wrote file"

    wrapped = _thread_tool(slow_permission_then_work, timeout=0.2)
    result = await wrapped()

    assert result == "wrote file"
    assert "Tool timeout" not in result


@pytest.mark.asyncio
async def test_timeout_resumes_after_permission_decision():
    """The clock is suspended, not disabled: once the prompt closes, a tool
    that then genuinely hangs past the budget still reports a timeout."""

    def permission_then_hang() -> str:
        begin_permission_wait()
        try:
            time.sleep(0.2)
        finally:
            end_permission_wait()
        time.sleep(1.0)  # genuine hang AFTER approval — must still time out
        return "should never be returned"

    wrapped = _thread_tool(permission_then_hang, timeout=0.2)
    result = await wrapped()

    assert "Tool timeout" in result


@pytest.mark.asyncio
async def test_gate_is_per_call_not_global():
    """A prompt open for one call must not exempt an unrelated concurrent
    call from its timeout. Each _thread_tool wrapper owns its own gate."""

    def waits_on_human() -> str:
        begin_permission_wait()
        try:
            time.sleep(0.5)
        finally:
            end_permission_wait()
        return "approved"

    def just_hangs() -> str:
        time.sleep(0.5)  # never touches the gate → must time out
        return "should never be returned"

    import asyncio

    waiting = _thread_tool(waits_on_human, timeout=0.2)
    hanging = _thread_tool(just_hangs, timeout=0.2)
    approved, timed_out = await asyncio.gather(waiting(), hanging())

    assert approved == "approved"
    assert "Tool timeout" in timed_out


# ── Integration: the real edit_file + check_permission flow ──────────


class _SlowChoiceUI:
    """Fake UIAdapter whose ask_choice blocks (simulating a slow human)."""

    def __init__(self, *, delay: float, choice: int) -> None:
        self.delay = delay
        self._choice = choice
        self.calls = 0

    def ask_choice(self, options, **kwargs):
        self.calls += 1
        time.sleep(self.delay)
        if self._choice == "cancel":
            return kwargs.get("cancel_value", len(options) - 1)
        return self._choice

    def ask_text(self, *args, **kwargs):
        return ""

    def print(self, *args, **kwargs):
        pass

    def notify(self, *args, **kwargs):
        pass

    def confirm(self, *args, **kwargs):
        return False


@pytest.mark.asyncio
async def test_edit_waits_for_slow_human_yes(tmp_path, fresh_runtime_context):
    """The exact reported scenario: an edit whose prompt takes longer than the
    tool timeout must wait for the user and apply on 'yes' — never time out and
    leave the write to land out-of-band."""
    ctx = fresh_runtime_context
    ctx.skip_permissions = False  # actually run the permission gate
    ctx.ui = _SlowChoiceUI(delay=0.5, choice=0)  # "Yes" after 0.5s

    f = tmp_path / "target.py"
    f.write_text("old\n")

    wrapped = _thread_tool(file_ops.edit_file, timeout=0.2)
    result = await wrapped(str(f), "old", "new")

    assert "Tool timeout" not in result
    assert "Edited" in result
    assert f.read_text() == "new\n"
    assert ctx.ui.calls == 1


@pytest.mark.asyncio
async def test_edit_slow_human_no_blocks_write(tmp_path, fresh_runtime_context):
    """A slow 'no' must also be honoured: no timeout, and the file is left
    untouched (never the orphaned-write bug in reverse)."""
    ctx = fresh_runtime_context
    ctx.skip_permissions = False
    ctx.ui = _SlowChoiceUI(delay=0.4, choice="cancel")  # "No" after 0.4s

    f = tmp_path / "target.py"
    f.write_text("old\n")

    wrapped = _thread_tool(file_ops.edit_file, timeout=0.2)
    result = await wrapped(str(f), "old", "new")

    assert "Tool timeout" not in result
    assert "PERMISSION DENIED" in result
    assert f.read_text() == "old\n"
