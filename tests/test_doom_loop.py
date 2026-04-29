"""DoomLoopDetector — sliding-window detection of repeated identical tool calls.

Pinning the contract:
* threshold defaults to 3, configurable via ``ARU_DOOM_LOOP_THRESHOLD``
  (values < 2 fall back to the default; non-int falls back too);
* ``record(name, args)`` returns False until the window is full of
  identical signatures, then returns True;
* signature is ``(tool_name, json.dumps(args, sort_keys=True))`` so the
  same args dict in different key order is treated as identical;
* ``reset_for_tool`` drops only entries matching that tool — a multi-loop
  scenario (3× read, then 3× grep) still detects the second loop;
* the detector is purely in-memory and per-instance — it does not read
  or mutate any shared state.

Plus integration tests through ``run_stream`` to verify the streaming
loop calls the prompt helper when a real fan-out triggers the heuristic
and aborts the run when the user says no.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest


# ── Pure unit tests ─────────────────────────────────────────────────


def test_default_threshold_is_3():
    from aru.doom_loop import DEFAULT_THRESHOLD, DoomLoopDetector

    d = DoomLoopDetector()
    assert d.threshold == DEFAULT_THRESHOLD == 3


def test_record_returns_false_until_threshold():
    from aru.doom_loop import DoomLoopDetector

    d = DoomLoopDetector()
    assert d.record("read_file", {"path": "a.py"}) is False
    assert d.record("read_file", {"path": "a.py"}) is False
    # 3rd identical → fires
    assert d.record("read_file", {"path": "a.py"}) is True


def test_different_tool_names_no_loop():
    from aru.doom_loop import DoomLoopDetector

    d = DoomLoopDetector()
    assert d.record("read_file", {"path": "a.py"}) is False
    assert d.record("grep_search", {"path": "a.py"}) is False
    assert d.record("read_file", {"path": "a.py"}) is False  # window not all same


def test_different_args_no_loop():
    from aru.doom_loop import DoomLoopDetector

    d = DoomLoopDetector()
    assert d.record("read_file", {"path": "a.py"}) is False
    assert d.record("read_file", {"path": "b.py"}) is False
    assert d.record("read_file", {"path": "c.py"}) is False


def test_arg_key_order_does_not_matter():
    """{a:1,b:2} and {b:2,a:1} must hash to the same signature."""
    from aru.doom_loop import DoomLoopDetector

    d = DoomLoopDetector()
    assert d.record("edit_file", {"path": "x.py", "new": "1"}) is False
    assert d.record("edit_file", {"new": "1", "path": "x.py"}) is False
    # Same content, different field order — must still trigger.
    assert d.record("edit_file", {"path": "x.py", "new": "1"}) is True


def test_none_args_treated_as_empty():
    from aru.doom_loop import DoomLoopDetector

    d = DoomLoopDetector()
    assert d.record("noop", None) is False
    assert d.record("noop", None) is False
    assert d.record("noop", None) is True


def test_non_dict_args_stable():
    """Non-dict args should not crash and should still hash stably."""
    from aru.doom_loop import DoomLoopDetector

    d = DoomLoopDetector()
    # tuple isn't json-serialisable directly with sort_keys, but the
    # fallback keeps it stable enough for equality comparisons across
    # repeated calls.
    assert d.record("weird", "string-arg") is False
    assert d.record("weird", "string-arg") is False
    assert d.record("weird", "string-arg") is True


def test_reset_clears_window():
    from aru.doom_loop import DoomLoopDetector

    d = DoomLoopDetector()
    d.record("read_file", {"path": "a"})
    d.record("read_file", {"path": "a"})
    d.reset()
    # Must NOT trigger on the very next call after reset — only 1 entry
    # in the window now.
    assert d.record("read_file", {"path": "a"}) is False


def test_reset_for_tool_drops_only_matching():
    """3× read then 3× grep should fire on the 6th, not the 4th."""
    from aru.doom_loop import DoomLoopDetector

    d = DoomLoopDetector(threshold=3)
    # After 3 reads → would fire, simulate user-confirm "continue":
    d.record("read_file", {"p": 1})
    d.record("read_file", {"p": 1})
    fired = d.record("read_file", {"p": 1})
    assert fired is True
    d.reset_for_tool("read_file")

    # Now grep loop builds up
    assert d.record("grep_search", {"q": "x"}) is False
    assert d.record("grep_search", {"q": "x"}) is False
    # 3rd grep — fires (read history was wiped, so window is purely grep)
    assert d.record("grep_search", {"q": "x"}) is True


def test_consecutive_loops_fire_once_per_streak():
    """After firing once, the buffer must still be full — calling
    record again with the same signature should also return True
    (no auto-reset). Caller is responsible for reset_for_tool()."""
    from aru.doom_loop import DoomLoopDetector

    d = DoomLoopDetector(threshold=3)
    for _ in range(3):
        result = d.record("read_file", {"path": "a"})
    assert result is True
    # 4th identical call without reset — still fires (window is still
    # all-identical). Real callers reset_for_tool after handling.
    assert d.record("read_file", {"path": "a"}) is True


def test_custom_threshold_5():
    from aru.doom_loop import DoomLoopDetector

    d = DoomLoopDetector(threshold=5)
    for i in range(4):
        assert d.record("x", {"k": 1}) is False, f"fired too early at i={i}"
    assert d.record("x", {"k": 1}) is True


# ── Env var threshold ────────────────────────────────────────────────


def test_threshold_from_env_default_when_unset(monkeypatch):
    from aru.doom_loop import DEFAULT_THRESHOLD, threshold_from_env

    monkeypatch.delenv("ARU_DOOM_LOOP_THRESHOLD", raising=False)
    assert threshold_from_env() == DEFAULT_THRESHOLD


def test_threshold_from_env_valid_value(monkeypatch):
    from aru.doom_loop import threshold_from_env

    monkeypatch.setenv("ARU_DOOM_LOOP_THRESHOLD", "7")
    assert threshold_from_env() == 7


def test_threshold_from_env_invalid_falls_back(monkeypatch):
    from aru.doom_loop import DEFAULT_THRESHOLD, threshold_from_env

    monkeypatch.setenv("ARU_DOOM_LOOP_THRESHOLD", "not-a-number")
    assert threshold_from_env() == DEFAULT_THRESHOLD


def test_threshold_from_env_below_2_falls_back(monkeypatch):
    """Threshold of 1 would fire on every call — guard against it."""
    from aru.doom_loop import DEFAULT_THRESHOLD, threshold_from_env

    for v in ("0", "1", "-3"):
        monkeypatch.setenv("ARU_DOOM_LOOP_THRESHOLD", v)
        assert threshold_from_env() == DEFAULT_THRESHOLD, (
            f"threshold {v!r} should have fallen back to {DEFAULT_THRESHOLD}"
        )


def test_detector_picks_up_env_threshold(monkeypatch):
    from aru.doom_loop import DoomLoopDetector

    monkeypatch.setenv("ARU_DOOM_LOOP_THRESHOLD", "4")
    d = DoomLoopDetector()
    assert d.threshold == 4
    for _ in range(3):
        assert d.record("x", {}) is False
    assert d.record("x", {}) is True


# ── Integration with run_stream ──────────────────────────────────────


class _RecordingSink:
    """Minimal StreamSink that captures calls."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def enter(self): self.events.append(("enter", {}))
    def exit(self, exc=None): self.events.append(("exit", {}))
    def on_tool_started(self, **kw): self.events.append(("tool_started", kw))
    def on_tool_completed(self, **kw): self.events.append(("tool_completed", kw))
    def on_tool_batch_finished(self, *, session): self.events.append(("batch_done", {}))
    def on_content_delta(self, *, delta, accumulated): self.events.append(("delta", {}))
    def on_stall(self): self.events.append(("stall", {}))
    def on_retry(self, *, attempt, max_attempts): self.events.append(("retry", {}))
    def on_retry_exhausted(self, *, max_attempts): self.events.append(("retry_exhausted", {}))
    def notify(self, message, style=""): self.events.append(("notify", {"msg": message, "style": style}))
    def on_error(self, message): self.events.append(("error", {"msg": message}))
    def on_stream_finished(self, *, final_content): self.events.append(("finished", {}))


class _FakeAgent:
    """Yields a scripted sequence of Agno events."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def arun(self, *args, **kwargs):
        events = self._events

        async def gen():
            for ev in events:
                yield ev

        return gen()


def _make_event_sequence(tool_name: str, tool_args: dict, n: int):
    """Build [(started, completed) × n, RunOutput]."""
    from agno.run.agent import (
        RunOutput,
        ToolCallCompletedEvent,
        ToolCallStartedEvent,
    )

    class _FakeTool:
        def __init__(self, tid, name, args, result=None):
            self.tool_call_id = tid
            self.tool_name = name
            self.tool_args = args
            self.result = result

    out = []
    for i in range(n):
        tid = f"t{i}"
        out.append(ToolCallStartedEvent(tool=_FakeTool(tid, tool_name, tool_args)))
        out.append(ToolCallCompletedEvent(tool=_FakeTool(tid, tool_name, tool_args, result="ok")))
    out.append(RunOutput())
    return out


@pytest.mark.asyncio
async def test_streaming_no_prompt_for_two_identical_calls():
    """Threshold is 3 — two identical calls must NOT prompt."""
    from aru.runtime import init_ctx
    from aru.streaming import run_stream

    init_ctx()

    sink = _RecordingSink()
    fake = _FakeAgent(_make_event_sequence("read_file", {"path": "a.py"}, n=2))

    async def publish(_evt, _data): pass
    def prep_recovery(**_kw): raise AssertionError("should not retry")

    await run_stream(
        fake,
        "u",
        sink=sink,
        assistant_blocks=[],
        tool_result_msgs=[],
        pending_tool_uses={},
        flush_pending_text=lambda _a: None,
        publish_event=publish,
        prepare_recovery_input=prep_recovery,
    )

    notify_msgs = [e for e in sink.events if e[0] == "notify"]
    assert not any("Doom-loop" in n[1].get("msg", "") for n in notify_msgs)


@pytest.mark.asyncio
async def test_streaming_no_prompt_when_args_differ():
    """3× same tool, different args → not a doom-loop."""
    from agno.run.agent import RunOutput, ToolCallCompletedEvent, ToolCallStartedEvent
    from aru.runtime import init_ctx
    from aru.streaming import run_stream

    init_ctx()

    class _FakeTool:
        def __init__(self, tid, name, args, result=None):
            self.tool_call_id = tid
            self.tool_name = name
            self.tool_args = args
            self.result = result

    sink = _RecordingSink()
    events = []
    for i, path in enumerate(["a.py", "b.py", "c.py"]):
        events.append(ToolCallStartedEvent(tool=_FakeTool(f"t{i}", "read_file", {"path": path})))
        events.append(ToolCallCompletedEvent(tool=_FakeTool(f"t{i}", "read_file", {"path": path}, result="ok")))
    events.append(RunOutput())
    fake = _FakeAgent(events)

    async def publish(_e, _d): pass
    def prep(**_kw): raise AssertionError("no retry expected")

    await run_stream(
        fake, "u", sink=sink,
        assistant_blocks=[], tool_result_msgs=[], pending_tool_uses={},
        flush_pending_text=lambda _a: None, publish_event=publish,
        prepare_recovery_input=prep,
    )

    notifies = [e for e in sink.events if e[0] == "notify"]
    assert not any("Doom-loop" in n[1].get("msg", "") for n in notifies)


@pytest.mark.asyncio
async def test_streaming_prompts_user_on_three_identical_and_continues_on_yes():
    """Three identical tool calls fire the prompt; user says yes → run completes."""
    from aru.runtime import get_ctx, init_ctx
    from aru.streaming import run_stream

    init_ctx()

    # Install a UI mock that always says yes
    class _YesUI:
        def __init__(self):
            self.confirm_calls: list[tuple[str, bool]] = []
        def confirm(self, prompt, default=False):
            self.confirm_calls.append((prompt, default))
            return True
        # Other methods unused but defined for protocol-ish duck typing
        def ask_choice(self, *a, **kw): return 0
        def ask_text(self, *a, **kw): return ""
        def print(self, *a, **kw): pass
        def notify(self, *a, **kw): pass

    ui = _YesUI()
    get_ctx().ui = ui

    sink = _RecordingSink()
    fake = _FakeAgent(_make_event_sequence("read_file", {"path": "x.py"}, n=3))

    async def publish(_e, _d): pass
    def prep(**_kw): raise AssertionError("no retry")

    await run_stream(
        fake, "u", sink=sink,
        assistant_blocks=[], tool_result_msgs=[], pending_tool_uses={},
        flush_pending_text=lambda _a: None, publish_event=publish,
        prepare_recovery_input=prep,
    )

    # Prompt was shown
    assert len(ui.confirm_calls) == 1
    assert "loop" in ui.confirm_calls[0][0].lower()
    # Notify message hit the sink before the prompt
    notifies = [e[1].get("msg", "") for e in sink.events if e[0] == "notify"]
    assert any("Doom-loop" in m for m in notifies)
    # No abort — error/stall absent, finished present
    names = [e[0] for e in sink.events]
    assert "finished" in names
    assert "error" not in names


@pytest.mark.asyncio
async def test_streaming_aborts_on_no():
    """Three identical tool calls, user says no → stalled and abort flag set."""
    from aru.runtime import get_ctx, init_ctx, is_aborted
    from aru.streaming import run_stream

    init_ctx()

    class _NoUI:
        def __init__(self):
            self.confirm_calls = 0
        def confirm(self, prompt, default=False):
            self.confirm_calls += 1
            return False
        def ask_choice(self, *a, **kw): return 0
        def ask_text(self, *a, **kw): return ""
        def print(self, *a, **kw): pass
        def notify(self, *a, **kw): pass

    ui = _NoUI()
    get_ctx().ui = ui

    sink = _RecordingSink()
    # Need 4 identical calls so we know the loop breaks after the 3rd —
    # if it doesn't, the 4th tool_completed would fire and we'd see a
    # second tool_completed event.
    fake = _FakeAgent(_make_event_sequence("read_file", {"path": "x.py"}, n=4))

    async def publish(_e, _d): pass
    def prep(**_kw): raise AssertionError("no retry")

    state = await run_stream(
        fake, "u", sink=sink,
        assistant_blocks=[], tool_result_msgs=[], pending_tool_uses={},
        flush_pending_text=lambda _a: None, publish_event=publish,
        prepare_recovery_input=prep,
    )

    assert ui.confirm_calls == 1
    assert state.stalled is True
    assert is_aborted() is True

    # Only 3 tool_completed events should have made it through before abort.
    completed = [e for e in sink.events if e[0] == "tool_completed"]
    assert len(completed) == 3, f"expected 3, got {len(completed)} (loop didn't break)"


@pytest.mark.asyncio
async def test_streaming_continue_clears_buffer_for_that_tool():
    """After 3 reads + user says yes, the next read must NOT immediately re-prompt."""
    from aru.runtime import get_ctx, init_ctx
    from aru.streaming import run_stream

    init_ctx()

    class _YesUI:
        def __init__(self): self.confirm_calls = 0
        def confirm(self, *a, **kw):
            self.confirm_calls += 1
            return True
        def ask_choice(self, *a, **kw): return 0
        def ask_text(self, *a, **kw): return ""
        def print(self, *a, **kw): pass
        def notify(self, *a, **kw): pass

    ui = _YesUI()
    get_ctx().ui = ui

    sink = _RecordingSink()
    # 4 identical reads — user says yes after 3rd, 4th must NOT prompt
    # (buffer was wiped for the read_file tool).
    fake = _FakeAgent(_make_event_sequence("read_file", {"path": "x.py"}, n=4))

    async def publish(_e, _d): pass
    def prep(**_kw): raise AssertionError("no retry")

    await run_stream(
        fake, "u", sink=sink,
        assistant_blocks=[], tool_result_msgs=[], pending_tool_uses={},
        flush_pending_text=lambda _a: None, publish_event=publish,
        prepare_recovery_input=prep,
    )

    # Exactly one prompt — the 4th call did not re-trigger.
    assert ui.confirm_calls == 1


@pytest.mark.asyncio
async def test_streaming_no_ui_installed_aborts_silently():
    """If ctx.ui is None we abort instead of hanging on a missing prompt."""
    from aru.runtime import get_ctx, init_ctx
    from aru.streaming import run_stream

    init_ctx()
    get_ctx().ui = None

    sink = _RecordingSink()
    fake = _FakeAgent(_make_event_sequence("read_file", {"path": "x.py"}, n=4))

    async def publish(_e, _d): pass
    def prep(**_kw): raise AssertionError("no retry")

    state = await run_stream(
        fake, "u", sink=sink,
        assistant_blocks=[], tool_result_msgs=[], pending_tool_uses={},
        flush_pending_text=lambda _a: None, publish_event=publish,
        prepare_recovery_input=prep,
    )
    # Aborted (stalled flag set) — exactly 3 tool_completeds before break.
    assert state.stalled is True
    completed = [e for e in sink.events if e[0] == "tool_completed"]
    assert len(completed) == 3
