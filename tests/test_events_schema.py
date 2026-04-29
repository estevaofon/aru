"""Tests for the typed event schemas in aru/events.py (E1)."""

from __future__ import annotations

import pytest

from aru.events import (
    EVENT_MODELS,
    CwdChangedEvent,
    FileChangedEvent,
    MessageAssistantEvent,
    MessageUserEvent,
    PermissionDeniedEvent,
    PermissionModeChangedEvent,
    SubagentCompleteEvent,
    SubagentStartEvent,
    SubagentToolCompletedEvent,
    SubagentToolStartedEvent,
    ToolCalledEvent,
    ToolCompletedEvent,
    TurnEndEvent,
    TurnStartEvent,
    coerce_to_dict,
)


def test_all_event_types_mapped():
    """Each model's event_type Literal must be in EVENT_MODELS registry."""
    expected_types = {
        "message.user",
        "message.assistant",
        "turn.start",
        "turn.end",
        "tool.called",
        "tool.completed",
        "subagent.start",
        "subagent.complete",
        "subagent.tool.started",
        "subagent.tool.completed",
        "cwd.changed",
        "file.changed",
        "permission.denied",
        "permission.mode.changed",
        "metrics.updated",
        "tasklist.updated",
        "plan.updated",
    }
    assert set(EVENT_MODELS.keys()) == expected_types


def test_tool_called_roundtrip():
    evt = ToolCalledEvent(
        tool_id="t-42",
        tool_name="read_file",
        args={"path": "README.md"},
    )
    d = evt.model_dump()
    assert d["event_type"] == "tool.called"
    assert d["tool_id"] == "t-42"
    assert d["tool_name"] == "read_file"
    assert d["args"] == {"path": "README.md"}
    assert "timestamp" in d
    # roundtrip
    reconstructed = ToolCalledEvent.model_validate(d)
    assert reconstructed.tool_name == evt.tool_name


def test_event_type_literal_locked():
    """event_type is a Literal — we cannot override it to a wrong value."""
    # Pydantic v2 rejects mismatched literals on validate
    with pytest.raises(Exception):
        ToolCalledEvent.model_validate({"event_type": "bogus.type"})


@pytest.mark.parametrize(
    "event_type,cls,fields",
    [
        ("message.user", MessageUserEvent, {"message": "oi"}),
        ("message.assistant", MessageAssistantEvent, {"content": "oi"}),
        ("turn.start", TurnStartEvent, {"turn_index": 1, "user_message": "hello"}),
        (
            "turn.end",
            TurnEndEvent,
            {"turn_index": 1, "assistant_reply": "done", "input_tokens": 100},
        ),
        (
            "tool.completed",
            ToolCompletedEvent,
            {"tool_id": "x", "tool_name": "read_file", "duration_ms": 42.0},
        ),
        (
            "subagent.start",
            SubagentStartEvent,
            {"task_id": "s1", "agent_kind": "explorer"},
        ),
        (
            "subagent.complete",
            SubagentCompleteEvent,
            {"task_id": "s1", "agent_kind": "explorer", "status": "ok"},
        ),
        (
            "subagent.tool.started",
            SubagentToolStartedEvent,
            {"task_id": "s1", "tool_id": "t-1", "tool_name": "grep_search"},
        ),
        (
            "subagent.tool.completed",
            SubagentToolCompletedEvent,
            {"task_id": "s1", "tool_id": "t-1", "tool_name": "grep_search",
             "duration_ms": 12.3},
        ),
        (
            "cwd.changed",
            CwdChangedEvent,
            {"new_cwd": "/tmp/wt", "reason": "worktree.enter"},
        ),
        ("file.changed", FileChangedEvent, {"path": "foo.py", "operation": "write"}),
        (
            "permission.denied",
            PermissionDeniedEvent,
            {"category": "write", "subject": "foo.env", "reason": "deny rule"},
        ),
        (
            "permission.mode.changed",
            PermissionModeChangedEvent,
            {"old_mode": "default", "new_mode": "acceptEdits"},
        ),
    ],
)
def test_each_event_constructs(event_type, cls, fields):
    evt = cls(**fields)
    assert evt.event_type == event_type
    # Registry resolves to the same class
    assert EVENT_MODELS[event_type] is cls


def test_coerce_to_dict_from_model():
    evt = ToolCalledEvent(tool_id="z", tool_name="bash")
    d = coerce_to_dict(evt)
    assert isinstance(d, dict)
    assert d["tool_id"] == "z"
    assert d["event_type"] == "tool.called"


def test_coerce_to_dict_from_dict():
    raw = {"tool_id": "y", "tool_name": "edit_file"}
    d = coerce_to_dict(raw)
    assert d is raw  # identity preserved for dicts — no unnecessary copy


def test_coerce_to_dict_from_none():
    assert coerce_to_dict(None) == {}


def test_default_timestamp_populated():
    evt = MessageUserEvent(message="hi")
    assert evt.timestamp > 0
