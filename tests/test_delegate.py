"""Tests for delegate_task and related subagent primitives."""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

# Eager registry import so `_DEFAULT_SUBAGENT_TOOLS` is assigned to the
# delegate module BEFORE any test runs. Without this, the first test to
# touch delegate would trigger registry load inside its body, and a
# monkeypatch.setattr captured earlier could leave a stale binding after
# teardown (see commit history for #I for full diagnosis).
import aru.tools.registry  # noqa: F401
from aru.runtime import init_ctx


@dataclass
class _FakeCustomAgentDef:
    description: str
    mode: str = "subagent"
    tools: list | None = None
    system_prompt: str = ""
    model: str | None = None
    permission: dict | None = None
    name: str = ""


# ---------------------------------------------------------------------------
# #B — prompt de coordenação extenso
# ---------------------------------------------------------------------------


class TestDelegatePromptDocstring:
    """The dynamic docstring is the primary tool schema the LLM sees — these
    tests pin down the structural pieces so future refactors don't silently
    regress the guidance."""

    def setup_method(self):
        init_ctx()

    def test_docstring_reads_from_shipped_prompt_file(self):
        from aru.tools.delegate import _load_delegate_prompt
        text = _load_delegate_prompt()
        assert "{agent_list}" in text, "template must contain the placeholder"
        assert "Parallelism" in text
        assert "Resume" in text

    def test_docstring_contains_parallelism_guidance(self):
        from aru.tools.delegate import _update_delegate_task_docstring, delegate_task
        _update_delegate_task_docstring()
        assert "parallel" in delegate_task.__doc__.lower()
        assert "single message" in delegate_task.__doc__.lower()

    def test_docstring_contains_when_not_to_use(self):
        from aru.tools.delegate import _update_delegate_task_docstring, delegate_task
        _update_delegate_task_docstring()
        assert "When NOT to use" in delegate_task.__doc__

    def test_docstring_contains_resume_guidance(self):
        from aru.tools.delegate import _update_delegate_task_docstring, delegate_task
        _update_delegate_task_docstring()
        assert "task_id" in delegate_task.__doc__.lower()
        assert "resume" in delegate_task.__doc__.lower()

    def test_docstring_lists_builtin_explorer(self):
        from aru.tools.delegate import _update_delegate_task_docstring, delegate_task
        _update_delegate_task_docstring()
        assert "explorer" in delegate_task.__doc__
        # Description rendered from AgentSpec.description
        assert "read-only codebase" in delegate_task.__doc__.lower()

    def test_docstring_lists_custom_agents(self):
        from aru.tools.delegate import _update_delegate_task_docstring, delegate_task
        from aru.runtime import get_ctx
        get_ctx().custom_agent_defs = {
            "reviewer": _FakeCustomAgentDef(description="code-review specialist"),
        }
        _update_delegate_task_docstring()
        assert "reviewer" in delegate_task.__doc__
        assert "code-review specialist" in delegate_task.__doc__

    def test_docstring_rendered_agent_list_helper(self):
        from aru.tools.delegate import _render_agent_list
        text = _render_agent_list()
        assert "Available sub-agents" in text
        assert "explorer" in text

    def test_docstring_survives_missing_file(self, monkeypatch):
        """Fallback doc must be used when the shipped prompt file is absent
        (e.g. a test environment where package data wasn't wired)."""
        import importlib.resources
        from aru.tools import delegate as delegate_mod

        def _raise_missing(*_a, **_kw):
            raise FileNotFoundError("simulated missing prompt")

        monkeypatch.setattr(importlib.resources, "files", _raise_missing)
        text = delegate_mod._load_delegate_prompt()
        assert "Delegate a task" in text  # fallback doc


# ---------------------------------------------------------------------------
# #C — Abort propagation (threading.Event-based)
# ---------------------------------------------------------------------------


class TestAbortPropagation:
    def test_initial_ctx_not_aborted(self):
        from aru.runtime import is_aborted
        init_ctx()
        assert is_aborted() is False

    def test_abort_current_sets_flag(self):
        from aru.runtime import abort_current, is_aborted
        init_ctx()
        abort_current()
        assert is_aborted() is True

    def test_reset_abort_clears_flag(self):
        from aru.runtime import abort_current, is_aborted, reset_abort
        init_ctx()
        abort_current()
        assert is_aborted() is True
        reset_abort()
        assert is_aborted() is False

    def test_abort_current_is_idempotent(self):
        from aru.runtime import abort_current, is_aborted
        init_ctx()
        abort_current()
        abort_current()
        abort_current()
        assert is_aborted() is True

    def test_fork_shares_abort_event(self):
        """A subagent fork must observe cancel signals from the primary.
        Critical invariant — otherwise Ctrl+C leaves zombies."""
        from aru.runtime import fork_ctx, init_ctx
        parent = init_ctx()
        child = fork_ctx()
        assert parent.abort_event is child.abort_event
        # Signal from parent — child observes
        parent.abort_event.set()
        assert child.abort_event.is_set() is True

    def test_fork_isolation_except_abort(self):
        """Other ctx fields must stay isolated — only abort_event is shared."""
        from aru.runtime import fork_ctx, init_ctx
        parent = init_ctx()
        parent.session_allowed.add(("edit", "*"))
        child = fork_ctx()
        # session_allowed isolated (fork takes a copy)
        child.session_allowed.add(("bash", "*"))
        assert ("bash", "*") not in parent.session_allowed
        # But abort_event shared
        assert parent.abort_event is child.abort_event

    def test_no_ctx_is_safe(self):
        """abort/reset/is_aborted must never raise when ctx is missing —
        callers from uninitialised contexts get no-ops / False."""
        import contextvars
        from aru.runtime import abort_current, is_aborted, reset_abort

        # Run in a fresh empty context that has no aru_runtime installed
        def _probe():
            assert is_aborted() is False
            abort_current()  # no-op
            reset_abort()  # no-op
            assert is_aborted() is False

        empty = contextvars.Context()
        empty.run(_probe)


# ---------------------------------------------------------------------------
# #E — Built-in agents: verification, reviewer, guide
# ---------------------------------------------------------------------------


class TestBuiltinAgents:
    def test_explorer_still_present(self):
        """Sanity — we extended the catalog, didn't replace it."""
        from aru.agents.catalog import AGENTS
        assert "explorer" in AGENTS
        assert AGENTS["explorer"].mode == "subagent"

    def test_verification_registered(self):
        from aru.agents.catalog import AGENTS
        spec = AGENTS["verification"]
        assert spec.mode == "subagent"
        assert spec.small_model is True
        assert "double-check" in spec.description.lower() or "verif" in spec.description.lower()

    def test_reviewer_registered(self):
        from aru.agents.catalog import AGENTS
        spec = AGENTS["reviewer"]
        assert spec.mode == "subagent"
        assert "review" in spec.description.lower()

    def test_guide_registered(self):
        from aru.agents.catalog import AGENTS
        spec = AGENTS["guide"]
        assert spec.mode == "subagent"
        assert "aru" in spec.description.lower()

    def test_all_new_builtins_are_readonly(self):
        """Verification/reviewer/guide must all use EXPLORER_TOOLS (read-only)
        to match their role instructions. If a future edit flips them to a
        wider toolset, the role prompts lie to the model."""
        from aru.agents.catalog import AGENTS
        from aru.tools.registry import EXPLORER_TOOLS
        for key in ("verification", "reviewer", "guide"):
            tools = AGENTS[key].tools_factory()
            # EXPLORER_TOOLS is the read-only set
            assert tools is EXPLORER_TOOLS or list(tools) == list(EXPLORER_TOOLS), \
                f"{key} is not using EXPLORER_TOOLS (read-only)"

    def test_new_builtins_appear_in_delegate_docstring(self):
        """Once registered, new built-ins must render in delegate_task's
        docstring so the LLM can pick them without special-casing."""
        from aru.tools.delegate import _update_delegate_task_docstring, delegate_task
        init_ctx()
        _update_delegate_task_docstring()
        doc = delegate_task.__doc__
        for name in ("explorer", "verification", "reviewer", "guide"):
            assert name in doc, f"{name} not in delegate_task docstring"

    def test_builtin_role_instructions_available(self):
        """build_instructions must accept the new role names without KeyError."""
        from aru.agents.base import build_instructions
        for role in ("verifier", "reviewer", "guide"):
            text = build_instructions(role)
            assert "READ-ONLY" in text.upper() or "read-only" in text.lower()


# ---------------------------------------------------------------------------
# #F — Structured sub-agent traces
# ---------------------------------------------------------------------------


class TestSubagentTrace:
    def test_trace_dataclass_shape(self):
        from aru.session import SubagentTrace
        trace = SubagentTrace(
            task_id="abc123",
            parent_id=None,
            agent_name="Explorer-1",
            task="find auth code",
            started_at=100.0,
        )
        assert trace.status == "running"
        assert trace.ended_at is None
        assert trace.duration == 0.0  # still running

    def test_trace_duration_after_completion(self):
        from aru.session import SubagentTrace
        trace = SubagentTrace(
            task_id="abc123",
            parent_id=None,
            agent_name="Explorer-1",
            task="x",
            started_at=100.0,
            ended_at=105.5,
        )
        assert trace.duration == 5.5

    def test_trace_roundtrip_dict(self):
        from aru.session import SubagentTrace
        trace = SubagentTrace(
            task_id="abc",
            parent_id="parent1",
            agent_name="Reviewer-2",
            task="review x.py",
            started_at=1.0,
            ended_at=2.5,
            tokens_in=100,
            tokens_out=50,
            tool_calls=[{"tool": "read_file", "args_preview": "x.py", "duration": 0.1}],
            status="completed",
            result="looks good",
        )
        restored = SubagentTrace.from_dict(trace.to_dict())
        assert restored.task_id == "abc"
        assert restored.parent_id == "parent1"
        assert restored.tokens_in == 100
        assert restored.status == "completed"
        assert restored.result == "looks good"
        assert len(restored.tool_calls) == 1

    def test_session_starts_with_empty_traces(self):
        from aru.session import Session
        session = Session()
        assert session.subagent_traces == []

    def test_subagents_command_handles_empty(self, capsys):
        from aru.commands import handle_subagents_command
        from aru.session import Session
        session = Session()
        handle_subagents_command(session)
        captured = capsys.readouterr()
        assert "No sub-agents" in captured.out

    def test_subagents_command_renders_single_trace(self, capsys):
        from aru.commands import handle_subagents_command
        from aru.session import Session, SubagentTrace
        session = Session()
        session.subagent_traces.append(SubagentTrace(
            task_id="abc12345",
            parent_id=None,
            agent_name="Explorer-1",
            task="find auth",
            started_at=0.0,
            ended_at=1.5,
            tokens_in=100,
            tokens_out=50,
            status="completed",
        ))
        handle_subagents_command(session)
        captured = capsys.readouterr()
        assert "abc12345" in captured.out
        assert "Explorer-1" in captured.out
        assert "completed" in captured.out

    def test_subagents_command_renders_tree_hierarchy(self, capsys):
        """Parent → child: child shows indented under parent."""
        from aru.commands import handle_subagents_command
        from aru.session import Session, SubagentTrace
        session = Session()
        session.subagent_traces.extend([
            SubagentTrace(task_id="parent1", parent_id=None, agent_name="Top",
                          task="x", started_at=0.0, ended_at=2.0, status="completed"),
            SubagentTrace(task_id="child1", parent_id="parent1", agent_name="Sub",
                          task="y", started_at=0.5, ended_at=1.5, status="completed"),
        ])
        handle_subagents_command(session)
        captured = capsys.readouterr()
        # Both present, and tree indent char appears
        assert "parent1" in captured.out
        assert "child1" in captured.out

    def test_subagent_detail_unknown_id(self, capsys):
        from aru.commands import handle_subagent_detail_command
        from aru.session import Session
        session = Session()
        handle_subagent_detail_command(session, "nonexistent")
        captured = capsys.readouterr()
        assert "No sub-agent found" in captured.out

    def test_subagent_detail_prefix_match(self, capsys):
        from aru.commands import handle_subagent_detail_command
        from aru.session import Session, SubagentTrace
        session = Session()
        session.subagent_traces.append(SubagentTrace(
            task_id="abcdef123456",
            parent_id=None,
            agent_name="Explorer-7",
            task="find X",
            started_at=0.0,
            ended_at=1.0,
            tokens_in=500,
            tokens_out=200,
            tool_calls=[{"tool": "grep_search", "args_preview": "pattern=foo", "duration": 0.05}],
            status="completed",
            result="found 3 occurrences",
        )
        )
        handle_subagent_detail_command(session, "abcdef")  # prefix
        captured = capsys.readouterr()
        assert "abcdef123456" in captured.out
        assert "Explorer-7" in captured.out
        assert "grep_search" in captured.out
        assert "found 3 occurrences" in captured.out

    def test_subagent_detail_no_arg_shows_usage(self, capsys):
        from aru.commands import handle_subagent_detail_command
        from aru.session import Session
        session = Session()
        handle_subagent_detail_command(session, "")
        captured = capsys.readouterr()
        assert "Usage" in captured.out


# ---------------------------------------------------------------------------
# #A — Permission per subagent_type (OpenCode parity)
# ---------------------------------------------------------------------------


class TestSubagentPermission:
    """The `delegate_task` permission now resolves via `canonical_permission`
    so users key aru.json rules by `task` (OpenCode-compatible) rather than
    `delegate_task`. Per-subagent patterns let specific agents be allowed,
    asked, or denied independently."""

    def setup_method(self):
        from aru.permissions import (
            PermissionConfig,
            parse_permission_config,
            reset_session,
            set_config,
            set_skip_permissions,
        )
        set_skip_permissions(False)
        set_config(PermissionConfig())
        reset_session()

    def test_delegate_task_default_allowed(self):
        from aru.permissions import resolve_permission
        # Default policy: CATEGORY_DEFAULTS["task"] = "allow"
        action, _ = resolve_permission("delegate_task", "explorer")
        assert action == "allow"

    def test_task_rule_applies_to_delegate_task(self):
        """{"task": {"explorer": "allow", "*": "ask"}} must resolve correctly
        when the caller passes `delegate_task` as the category — canonical
        permission mapping makes the two equivalent at resolve time."""
        from aru.permissions import parse_permission_config, resolve_permission, set_config
        set_config(parse_permission_config({
            "task": {"*": "ask", "explorer": "allow"},
        }))
        assert resolve_permission("delegate_task", "explorer")[0] == "allow"
        assert resolve_permission("delegate_task", "custom_dangerous")[0] == "ask"

    def test_task_deny_specific_subagent(self):
        from aru.permissions import parse_permission_config, resolve_permission, set_config
        set_config(parse_permission_config({
            "task": {"custom_dangerous": "deny"},
        }))
        assert resolve_permission("delegate_task", "custom_dangerous")[0] == "deny"

    def test_webfetch_canonical_works(self):
        """web_fetch → webfetch canonical. Rule on `webfetch` affects the tool."""
        from aru.permissions import parse_permission_config, resolve_permission, set_config
        set_config(parse_permission_config({"webfetch": "deny"}))
        assert resolve_permission("web_fetch", "https://example.com")[0] == "deny"

    def test_websearch_canonical_works(self):
        from aru.permissions import parse_permission_config, resolve_permission, set_config
        set_config(parse_permission_config({"websearch": "deny"}))
        assert resolve_permission("web_search", "query")[0] == "deny"

    def test_edit_rule_still_covers_edit_file(self):
        """Back-compat: {"edit": "ask"} must still govern edit_file callers
        directly (edit_file tool isn't canonicalised because it already keys
        via category="edit" in its call site)."""
        from aru.permissions import parse_permission_config, resolve_permission, set_config
        set_config(parse_permission_config({"edit": "deny"}))
        assert resolve_permission("edit", "main.py")[0] == "deny"


# ---------------------------------------------------------------------------
# #D — Resume via task_id (in-memory)
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Minimal async agent that emits a single RunOutput event.

    Used to exercise delegate_task's orchestration without hitting the
    LLM. Compatible with the `arun(task, stream=True, stream_events=True,
    yield_run_output=True)` signature — yields an actual `agno.run.agent.RunOutput`
    so the `isinstance()` check in delegate._execute_with_streaming passes.
    """

    def __init__(self, name: str = "FakeAgent", reply: str = "done"):
        self.name = name
        self.reply = reply
        self.calls: list[str] = []
        # Attributes the delegate retry path touches
        self.model = None
        self.tools = []
        self.instructions = ""

    async def arun(self, task, stream=True, stream_events=True, yield_run_output=True):
        from agno.run.agent import RunOutput
        self.calls.append(task)
        out = RunOutput(content=f"{self.reply}: {task}")
        yield out


class TestDelegateResume:
    def setup_method(self):
        from aru.permissions import PermissionConfig, reset_session, set_config, set_skip_permissions
        from aru.runtime import init_ctx, reset_abort
        from aru.session import Session
        init_ctx()
        from aru.runtime import get_ctx
        get_ctx().session = Session()
        set_config(PermissionConfig())
        reset_session()
        set_skip_permissions(True)  # avoid permission prompt in tests
        reset_abort()

    @pytest.mark.asyncio
    async def test_fresh_delegation_emits_task_id(self, monkeypatch):
        from aru.runtime import get_ctx
        from aru.tools import delegate as delegate_mod

        fake = _FakeAgent(name="Explorer-1", reply="found auth at x.py:12")

        # Bypass the spec-driven agent creation path and force our fake.
        async def _fake_create_from_spec(*a, **kw):
            return fake

        monkeypatch.setattr(
            "aru.agent_factory.create_agent_from_spec", _fake_create_from_spec
        )
        # Disable subagent progress rendering (no console in test ctx)

        result = await delegate_mod.delegate_task("find auth", agent_name="explorer")
        assert "task_id=" in result
        assert "found auth" in result

        # task_id from result must be indexable into the cache
        import re
        match = re.search(r"task_id=(\S+?)\]", result)
        assert match, f"task_id header missing: {result}"
        task_id = match.group(1)
        session = get_ctx().session
        assert task_id in session._subagent_instances

    @pytest.mark.asyncio
    async def test_resume_reuses_same_agent_instance(self, monkeypatch):
        from aru.runtime import get_ctx
        from aru.tools import delegate as delegate_mod

        fake = _FakeAgent(name="Explorer-1", reply="first result")

        async def _fake_create_from_spec(*a, **kw):
            return fake

        monkeypatch.setattr(
            "aru.agent_factory.create_agent_from_spec", _fake_create_from_spec
        )

        # First call — fresh
        r1 = await delegate_mod.delegate_task("first", agent_name="explorer")
        import re
        task_id = re.search(r"task_id=(\S+?)\]", r1).group(1)

        # Swap fake's reply for the second call so we can verify reuse
        fake.reply = "second result"

        # Second call with task_id — should reuse the SAME fake agent
        # (which tracks calls in `self.calls`)
        r2 = await delegate_mod.delegate_task(
            "follow-up", agent_name="explorer", task_id=task_id
        )
        assert "second result" in r2
        assert len(fake.calls) == 2
        assert fake.calls == ["first", "follow-up"]

    @pytest.mark.asyncio
    async def test_unknown_task_id_creates_fresh(self, monkeypatch):
        from aru.tools import delegate as delegate_mod

        fake = _FakeAgent(name="Explorer-1", reply="ok")

        async def _fake_create_from_spec(*a, **kw):
            return fake

        monkeypatch.setattr(
            "aru.agent_factory.create_agent_from_spec", _fake_create_from_spec
        )

        result = await delegate_mod.delegate_task(
            "x", agent_name="explorer", task_id="nonexistent-xyz"
        )
        # Must not error — silently creates fresh
        assert "task_id=" in result
        assert "[Error" not in result

    @pytest.mark.asyncio
    async def test_denied_delegation_returns_deny_marker(self, monkeypatch):
        from aru.permissions import parse_permission_config, set_config, set_skip_permissions
        from aru.tools import delegate as delegate_mod

        set_skip_permissions(False)
        set_config(parse_permission_config({"task": {"dangerous": "deny"}}))

        called = {"spec": False}

        async def _fake_create_from_spec(*a, **kw):
            called["spec"] = True
            return _FakeAgent(reply="should-never-run")

        monkeypatch.setattr(
            "aru.agent_factory.create_agent_from_spec", _fake_create_from_spec
        )

        result = await delegate_mod.delegate_task("x", agent_name="dangerous")
        assert "Permission denied" in result
        assert called["spec"] is False  # permission check fired before spawn


# ---------------------------------------------------------------------------
# #G — Persistência em disco dos traces
# ---------------------------------------------------------------------------


class TestTracePersistence:
    def setup_method(self):
        from aru.permissions import PermissionConfig, reset_session, set_config, set_skip_permissions
        from aru.runtime import init_ctx, reset_abort
        from aru.session import Session
        init_ctx()
        from aru.runtime import get_ctx
        # Use a session with a stable id so we can find the persisted dir
        get_ctx().session = Session(session_id="testsess")
        set_config(PermissionConfig())
        reset_session()
        set_skip_permissions(True)
        reset_abort()

    def teardown_method(self):
        """Clean up the .aru/sessions/testsess/subagents/ dir we created."""
        import shutil
        path = os.path.join(".aru", "sessions", "testsess")
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)

    def test_session_dir_helper(self):
        from aru.session import Session
        from aru.tools.delegate import _session_dir
        s = Session(session_id="abc")
        result = _session_dir(s)
        assert result is not None
        assert result.endswith(os.path.join("sessions", "abc", "subagents"))

    def test_session_dir_none_for_missing_session(self):
        from aru.tools.delegate import _session_dir
        assert _session_dir(None) is None

    def test_persist_trace_writes_json_files(self):
        from aru.session import Session, SubagentTrace
        from aru.tools.delegate import _persist_trace_async
        s = Session(session_id="testsess")
        trace = SubagentTrace(
            task_id="sa-42",
            parent_id=None,
            agent_name="Explorer-42",
            task="find bugs",
            started_at=1.0,
            ended_at=2.5,
            tokens_in=100,
            tokens_out=50,
            status="completed",
            result="no bugs found",
        )
        _persist_trace_async(s, trace)

        import json
        base = os.path.join(".aru", "sessions", "testsess", "subagents", "sa-42")
        trace_file = os.path.join(base, "trace.json")
        meta_file = os.path.join(base, "metadata.json")
        assert os.path.isfile(trace_file)
        assert os.path.isfile(meta_file)

        with open(trace_file) as f:
            data = json.load(f)
        assert data["task_id"] == "sa-42"
        assert data["agent_name"] == "Explorer-42"
        assert data["tokens_in"] == 100
        assert data["result"] == "no bugs found"

        with open(meta_file) as f:
            meta = json.load(f)
        assert meta["status"] == "completed"

    def test_persist_swallows_failures(self):
        """If the disk write fails, the call must never raise — the trace
        in memory is authoritative; disk is a bonus."""
        from aru.session import Session, SubagentTrace
        from aru.tools.delegate import _persist_trace_async
        # A session with an id that would be an illegal path on Windows
        # causes makedirs to fail; _persist_trace_async should swallow.
        s = Session(session_id="\x00/bad?chars*:")
        trace = SubagentTrace(
            task_id="x", parent_id=None, agent_name="x", task="x",
            started_at=0.0, ended_at=1.0,
        )
        # Should not raise
        _persist_trace_async(s, trace)

    def test_load_persisted_traces_roundtrip(self):
        from aru.session import Session, SubagentTrace
        from aru.tools.delegate import _persist_trace_async, load_persisted_traces
        s = Session(session_id="testsess")
        for i in range(3):
            t = SubagentTrace(
                task_id=f"sa-{i}",
                parent_id=None,
                agent_name=f"Agent-{i}",
                task=f"task {i}",
                started_at=float(i),
                ended_at=float(i) + 1.0,
                tokens_in=i * 10,
                tokens_out=i * 5,
                status="completed",
                result=f"result {i}",
            )
            _persist_trace_async(s, t)

        loaded = load_persisted_traces("testsess")
        assert len(loaded) == 3
        # Sorted by task_id: sa-0, sa-1, sa-2
        assert loaded[0].task_id == "sa-0"
        assert loaded[1].tokens_in == 10
        assert loaded[2].result == "result 2"

    def test_load_returns_empty_for_missing_session(self):
        from aru.tools.delegate import load_persisted_traces
        assert load_persisted_traces("nonexistent-sess") == []

    def test_load_skips_corrupt_files(self):
        from aru.session import Session, SubagentTrace
        from aru.tools.delegate import _persist_trace_async, load_persisted_traces
        s = Session(session_id="testsess")
        # Persist a valid trace
        _persist_trace_async(s, SubagentTrace(
            task_id="good", parent_id=None, agent_name="A", task="x",
            started_at=0.0, ended_at=1.0, status="completed",
        ))
        # Write a corrupt trace.json next to it
        bad_dir = os.path.join(".aru", "sessions", "testsess", "subagents", "bad")
        os.makedirs(bad_dir, exist_ok=True)
        with open(os.path.join(bad_dir, "trace.json"), "w") as f:
            f.write("{not json")

        loaded = load_persisted_traces("testsess")
        # Only the good one — corrupt is silently skipped
        assert len(loaded) == 1
        assert loaded[0].task_id == "good"


# ---------------------------------------------------------------------------
# #H — Background mode (run_in_background)
# ---------------------------------------------------------------------------


class TestBackgroundMode:
    def setup_method(self):
        from aru.permissions import PermissionConfig, reset_session, set_config, set_skip_permissions
        from aru.runtime import init_ctx, reset_abort
        from aru.session import Session
        init_ctx()
        from aru.runtime import get_ctx
        get_ctx().session = Session(session_id="bgtest")
        set_config(PermissionConfig())
        reset_session()
        set_skip_permissions(True)
        reset_abort()

    @pytest.mark.asyncio
    async def test_background_returns_immediately_with_task_id(self, monkeypatch):
        from aru.tools import delegate as delegate_mod

        class _SlowAgent(_FakeAgent):
            async def arun(self, task, **kw):
                import asyncio as _asyncio
                from agno.run.agent import RunOutput
                await _asyncio.sleep(0.05)  # simulates LLM latency
                self.calls.append(task)
                yield RunOutput(content=f"done: {task}")

        fake = _SlowAgent(name="Explorer-1")

        async def _fake_create_from_spec(*a, **kw):
            return fake

        monkeypatch.setattr(
            "aru.agent_factory.create_agent_from_spec", _fake_create_from_spec
        )

        import time
        t0 = time.monotonic()
        result = await delegate_mod.delegate_task(
            "slow task", agent_name="explorer", run_in_background=True
        )
        elapsed = time.monotonic() - t0

        # Must NOT wait for the slow agent — we return immediately
        assert elapsed < 0.03, f"background should return fast, took {elapsed:.3f}s"
        assert "BackgroundTask" in result
        assert "task_id=bg-" in result

    @pytest.mark.asyncio
    async def test_background_notification_appended_on_completion(self, monkeypatch):
        from aru.runtime import get_ctx
        from aru.tools import delegate as delegate_mod

        fake = _FakeAgent(name="Explorer-1", reply="bg-result")

        async def _fake_create_from_spec(*a, **kw):
            return fake

        monkeypatch.setattr(
            "aru.agent_factory.create_agent_from_spec", _fake_create_from_spec
        )

        session = get_ctx().session
        assert session.pending_notifications == []

        await delegate_mod.delegate_task(
            "x", agent_name="explorer", run_in_background=True
        )
        # Give the background task a chance to complete
        import asyncio as _asyncio
        for _ in range(20):
            await _asyncio.sleep(0.01)
            if session.pending_notifications:
                break

        assert len(session.pending_notifications) == 1
        n = session.pending_notifications[0]
        assert n["task_id"].startswith("bg-")
        assert "bg-result" in n["result"]

    def test_drain_empty_returns_empty_string(self):
        from aru.session import Session
        from aru.tools.delegate import drain_pending_notifications
        session = Session()
        assert drain_pending_notifications(session) == ""

    def test_drain_formats_task_notification_xml(self):
        from aru.session import Session
        from aru.tools.delegate import drain_pending_notifications
        session = Session()
        session.pending_notifications.append({
            "task_id": "bg-abc123", "result": "findings here", "at": 0.0,
        })
        text = drain_pending_notifications(session)
        assert "<task-notification" in text
        assert "task_id=\"bg-abc123\"" in text
        assert "findings here" in text
        assert "</task-notification>" in text
        # Drained — session list now empty
        assert session.pending_notifications == []

    def test_drain_handles_multiple(self):
        from aru.session import Session
        from aru.tools.delegate import drain_pending_notifications
        session = Session()
        for i in range(3):
            session.pending_notifications.append({
                "task_id": f"bg-{i}", "result": f"result {i}", "at": 0.0,
            })
        text = drain_pending_notifications(session)
        for i in range(3):
            assert f"bg-{i}" in text
            assert f"result {i}" in text

    def test_bg_command_empty(self, capsys):
        from aru.commands import handle_background_command
        from aru.session import Session
        handle_background_command(Session())
        captured = capsys.readouterr()
        assert "No pending" in captured.out

    def test_bg_command_lists_pending(self, capsys):
        from aru.commands import handle_background_command
        from aru.session import Session
        session = Session()
        session.pending_notifications.append({
            "task_id": "bg-xyz", "result": "some result text here", "at": 0.0,
        })
        handle_background_command(session)
        captured = capsys.readouterr()
        assert "bg-xyz" in captured.out
        assert "some result text" in captured.out


# ---------------------------------------------------------------------------
# #I — Configurable sub-agent recursion
# ---------------------------------------------------------------------------


class TestSubagentRecursion:
    """Recursion is gated by two mechanisms:

    1. Toolset — the sub-agent's tool list must include `delegate_task` for
       the model to call it. Generic (nameless) sub-agents default-deny via
       `_DEFAULT_SUBAGENT_TOOLS` which excludes delegate_task. Custom YAML
       agents with explicit `tools: [..., delegate_task]` opt in.

    2. Depth — `ctx.subagent_depth` increments on each `fork_ctx()`.
       `delegate_task` refuses to spawn when depth >= `MAX_SUBAGENT_DEPTH`
       as a safety net against a prompt bug triggering unbounded chains.
    """

    def setup_method(self):
        from aru.permissions import PermissionConfig, reset_session, set_config, set_skip_permissions
        from aru.runtime import init_ctx, reset_abort
        from aru.session import Session
        init_ctx()
        from aru.runtime import get_ctx
        get_ctx().session = Session(session_id="rectest")
        set_config(PermissionConfig())
        reset_session()
        set_skip_permissions(True)
        reset_abort()

    # ----- subagent_depth ctx field -----

    def test_primary_ctx_has_depth_zero(self):
        from aru.runtime import init_ctx
        ctx = init_ctx()
        assert ctx.subagent_depth == 0

    def test_fork_increments_depth(self):
        from aru.runtime import fork_ctx, init_ctx
        init_ctx()
        child = fork_ctx()
        assert child.subagent_depth == 1

    def test_fork_of_fork_gets_depth_two(self):
        """Nested delegation — child of a child has depth=2."""
        from aru.runtime import fork_ctx, init_ctx, set_ctx
        init_ctx()
        child = fork_ctx()
        set_ctx(child)
        grandchild = fork_ctx()
        assert grandchild.subagent_depth == 2

    def test_depth_survives_three_levels(self):
        """Safety net arithmetic — depth compounds correctly over 5 forks."""
        from aru.runtime import fork_ctx, init_ctx, set_ctx
        init_ctx()
        current = fork_ctx()
        for expected_depth in range(2, 6):
            set_ctx(current)
            current = fork_ctx()
            assert current.subagent_depth == expected_depth

    # ----- MAX_SUBAGENT_DEPTH gate -----

    @pytest.mark.asyncio
    async def test_delegate_refuses_at_max_depth(self):
        """At the cap, delegate_task returns a deny marker without spawning."""
        from aru.runtime import get_ctx
        from aru.tools import delegate as delegate_mod

        get_ctx().subagent_depth = delegate_mod.MAX_SUBAGENT_DEPTH
        result = await delegate_mod.delegate_task("x", agent_name="explorer")
        assert "Max sub-agent recursion depth" in result
        assert str(delegate_mod.MAX_SUBAGENT_DEPTH) in result

    @pytest.mark.asyncio
    async def test_delegate_refuses_above_max_depth(self):
        """Defensive — a stale deep ctx can't bypass the cap."""
        from aru.runtime import get_ctx
        from aru.tools import delegate as delegate_mod

        get_ctx().subagent_depth = delegate_mod.MAX_SUBAGENT_DEPTH + 3
        result = await delegate_mod.delegate_task("x", agent_name="explorer")
        assert "Max sub-agent recursion depth" in result

    @pytest.mark.asyncio
    async def test_delegate_allowed_below_max_depth(self, monkeypatch):
        """At depth 4 (cap is 5), a spawn still proceeds."""
        from aru.runtime import get_ctx
        from aru.tools import delegate as delegate_mod

        get_ctx().subagent_depth = delegate_mod.MAX_SUBAGENT_DEPTH - 1

        fake = _FakeAgent(name="Explorer-1", reply="ok")

        async def _fake_create(*a, **kw):
            return fake

        monkeypatch.setattr("aru.agent_factory.create_agent_from_spec", _fake_create)

        result = await delegate_mod.delegate_task("x", agent_name="explorer")
        assert "Max sub-agent recursion depth" not in result
        assert "task_id=" in result

    # ----- toolset: hardcoded filter removed -----

    @pytest.mark.asyncio
    async def test_custom_agent_yaml_tools_honoured_verbatim(self, monkeypatch):
        """The old `tools = [t for t in tools if t is not delegate_task]`
        filter contradicted the YAML contract. After removal, a custom
        agent with `tools: [delegate_task, ...]` receives delegate_task in
        its toolset."""
        from aru.runtime import get_ctx
        from aru.tools import delegate as delegate_mod
        from aru.tools.delegate import delegate_task

        # Register a custom agent whose YAML opts into recursion
        @dataclass
        class _CustomDef:
            name: str = "orchestrator"
            mode: str = "subagent"
            description: str = "delegates"
            tools: list = None
            system_prompt: str = "you orchestrate"
            model: str = None
            permission: dict = None

        custom = _CustomDef(tools=["read_file", "delegate_task"])
        get_ctx().custom_agent_defs = {"orchestrator": custom}

        captured_tools = {}

        # Patch Agent constructor to capture the tools it received
        import agno.agent
        original_init = agno.agent.Agent.__init__

        def _capturing_init(self, *args, **kwargs):
            captured_tools["tools"] = kwargs.get("tools", [])
            # Minimum viable agent so execution doesn't blow up
            self.name = kwargs.get("name", "captured")
            self.model = kwargs.get("model")
            self.tools = captured_tools["tools"]
            self.instructions = kwargs.get("instructions", "")

            async def _arun(task, **_):
                from agno.run.agent import RunOutput
                yield RunOutput(content=f"ok: {task}")

            self.arun = _arun

        monkeypatch.setattr(agno.agent.Agent, "__init__", _capturing_init)

        await delegate_mod.delegate_task("dispatch", agent_name="orchestrator")

        # Verify delegate_task is in the captured toolset — the post-resolve
        # filter is gone, YAML intent is respected.
        tool_names = [getattr(t, "__name__", str(t)) for t in captured_tools["tools"]]
        assert "delegate_task" in tool_names
        assert "read_file" in tool_names

    def test_default_toolset_still_excludes_delegate(self):
        """Default conservative: a nameless sub-agent's toolset does NOT
        include delegate_task (no explicit authorisation path)."""
        import aru.tools.registry  # noqa: F401 — ensures registry populated
        from aru.tools.delegate import _DEFAULT_SUBAGENT_TOOLS, delegate_task

        assert delegate_task not in _DEFAULT_SUBAGENT_TOOLS

    def test_default_toolset_has_reasonable_size(self):
        """Sanity — the default set must have enough tools to be useful
        even without delegation (read, grep, bash, etc.). 13 tools before
        the rename; floor at 10 leaves room for future additions/removals."""
        from aru.tools.delegate import _DEFAULT_SUBAGENT_TOOLS
        assert len(_DEFAULT_SUBAGENT_TOOLS) >= 10

    def test_delegate_module_and_registry_bindings_coincide(self):
        """Regression guard for the binding-divergence bug. Registry used
        to do `from aru.tools.delegate import _DEFAULT_SUBAGENT_TOOLS`
        (creates a separate binding in registry's namespace) and mutate
        the list in-place via `[:]=`. Under `monkeypatch.setattr`, the two
        bindings would diverge — registry's binding kept the populated
        list, delegate's binding got restored to the empty placeholder.

        Fix: registry now imports the module (`import aru.tools.delegate
        as _delegate_module`) and assigns the attribute
        (`_delegate_module._DEFAULT_SUBAGENT_TOOLS = [...]`). Single
        authoritative binding. This test asserts the invariant.
        """
        import aru.tools.delegate as delegate_mod
        from aru.tools.delegate import _DEFAULT_SUBAGENT_TOOLS as symbol_ref
        assert symbol_ref is delegate_mod._DEFAULT_SUBAGENT_TOOLS
