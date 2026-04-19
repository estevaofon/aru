"""delegate_task tool and sub-agent lifecycle.

Spawns sub-agents (explorer / custom / generic) with their own RuntimeContext
fork so permission state and task store are isolated. `_SUBAGENT_TOOLS` lives
here because delegate_task reads it at call time; the registry populates it
once tool wrappers exist.
"""

from __future__ import annotations

import asyncio
import os
import threading

from aru.runtime import get_ctx
from aru.tools._shared import _get_small_model_ref, _truncate_output


_subagent_counter = 0
_subagent_counter_lock = threading.Lock()


def _next_subagent_id() -> int:
    global _subagent_counter
    with _subagent_counter_lock:
        _subagent_counter += 1
        return _subagent_counter


# Populated by the registry once wrappers exist; delegate_task reads this at
# call time so late population is safe. Excludes delegate_task itself to
# prevent infinite recursion.
_SUBAGENT_TOOLS: list = []


def _session_dir(session) -> str | None:
    """Return the subagent-traces directory for a session.

    `.aru/sessions/<session_id>/subagents/` — created lazily. Returns None
    when the session is missing or lacks an id (ephemeral test sessions).
    """
    if session is None:
        return None
    sid = getattr(session, "session_id", None)
    if not sid:
        return None
    return os.path.join(".aru", "sessions", str(sid), "subagents")


def _persist_trace_async(session, trace) -> None:
    """Write `trace.json` + `metadata.json` for a completed sub-agent run.

    Fire-and-forget — failures are swallowed because a trace write should
    never block (or break) the sub-agent's return. The dir layout mirrors
    claude-code's `.claude/subagents/<sessionId>/...` (sessionStorage.ts:
    1451-1461, 283-303): one directory per task_id, containing the trace
    blob and a small metadata file callers can scan without loading
    everything.
    """
    base = _session_dir(session)
    if base is None:
        return
    try:
        task_dir = os.path.join(base, trace.task_id)
        os.makedirs(task_dir, exist_ok=True)
        # trace.json — full structured record (consumed by /subagent <id>)
        import json
        with open(os.path.join(task_dir, "trace.json"), "w", encoding="utf-8") as f:
            json.dump(trace.to_dict(), f, indent=2, ensure_ascii=False)
        # metadata.json — lightweight index (consumed by /subagents list)
        meta = {
            "task_id": trace.task_id,
            "parent_id": trace.parent_id,
            "agent_name": trace.agent_name,
            "status": trace.status,
            "started_at": trace.started_at,
            "ended_at": trace.ended_at,
        }
        with open(os.path.join(task_dir, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
    except Exception:
        # Never let a write failure surface to the caller — the trace is
        # already in memory (session.subagent_traces) and disk is a bonus.
        pass


def drain_pending_notifications(session) -> str:
    """Consume and format pending background-task notifications.

    Called by the REPL before each turn. If no notifications are pending,
    returns an empty string. Otherwise returns a text block ready to
    prepend to the user's next message so the model sees the results of
    its background delegations. Mirrors claude-code's `<task-notification>`
    XML-wrapped block (AgentTool.tsx `shouldRunAsync` path).
    """
    pending = list(getattr(session, "pending_notifications", []) or [])
    if not pending:
        return ""
    # Clear atomically — any new notifications arriving during rendering
    # get drained on the next call.
    session.pending_notifications = []
    parts = []
    for n in pending:
        parts.append(
            f"<task-notification task_id=\"{n.get('task_id', '?')}\">\n"
            f"{n.get('result', '(no result)')}\n"
            f"</task-notification>"
        )
    return "\n\n".join(parts)


def load_persisted_traces(session_id: str) -> list:
    """Read all persisted sub-agent traces for a given session.

    Returns a list of `SubagentTrace` objects. Used by `/subagents` to
    surface traces from prior sessions, and by `aru --resume` to restore
    the trace log alongside the conversation history.
    """
    from aru.session import SubagentTrace

    base = os.path.join(".aru", "sessions", str(session_id), "subagents")
    if not os.path.isdir(base):
        return []
    traces: list = []
    try:
        import json
        for task_id in sorted(os.listdir(base)):
            trace_path = os.path.join(base, task_id, "trace.json")
            if not os.path.isfile(trace_path):
                continue
            try:
                with open(trace_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                traces.append(SubagentTrace.from_dict(data))
            except (json.JSONDecodeError, OSError, KeyError):
                continue
    except OSError:
        pass
    return traces


async def delegate_task(
    task: str,
    context: str = "",
    agent_name: str = "",
    task_id: str = "",
    run_in_background: bool = False,
) -> str:
    """Delegate a task to a sub-agent that runs autonomously. Multiple calls run concurrently.
    Use for independent research or subtasks to keep your own context clean.

    Args:
        task: What the sub-agent should do.
        context: Optional extra context (file paths, constraints).
        agent_name: Name of a specialized agent to use instead of the generic sub-agent.
        task_id: If set, resume a prior sub-agent session with its full history
            instead of creating a fresh one. Pass the task_id from a prior
            delegate_task result header. If the id is unknown, a fresh
            sub-agent is created silently.
        run_in_background: When True, return immediately with a background
            task id while the sub-agent runs asynchronously. You will receive
            a <task-notification> message when it completes — do NOT poll.
            Useful for long-running research that can overlap with your
            other work. Foreground mode (default) blocks until the sub-agent
            returns, appropriate when you need the result before continuing.
    """

    async def _run() -> str:
        # Permission check runs in the PARENT ctx so a deny is surfaced to
        # the parent agent without spawning anything (saves a subagent
        # setup cost). After the check, we fork into the subagent ctx.
        from aru.permissions import check_permission
        _agent_name_pre = str(agent_name).strip() if agent_name else "generic"
        if not check_permission(
            "delegate_task",
            _agent_name_pre,
            f"delegate to sub-agent: {_agent_name_pre}",
        ):
            return f"[DELEGATE] Permission denied for sub-agent: {_agent_name_pre}"

        from aru.runtime import fork_ctx, set_ctx
        set_ctx(fork_ctx())

        from agno.agent import Agent
        from aru.providers import create_model
        from aru.tools.registry import resolve_tools

        agent_id = _next_subagent_id()
        cwd = os.getcwd()
        small_model_ref = _get_small_model_ref()

        agent_perm = None
        custom_agent_defs = get_ctx().custom_agent_defs
        _agent_name = str(agent_name).strip() if agent_name else ""
        _builtin = _agent_name.lower()

        # Lookup table: any catalog entry with mode="subagent" is a first-
        # class built-in. Previously only `explorer` was special-cased; now
        # new built-ins (verification, reviewer, guide, …) get dispatched
        # the same way without per-name branches.
        from aru.agents.catalog import AGENTS
        builtin_spec = None
        if _builtin and _builtin in AGENTS and getattr(AGENTS[_builtin], "mode", "") == "subagent":
            builtin_spec = AGENTS[_builtin]

        # Resume path: reuse the prior Agent instance if task_id points to a
        # live sub-agent from this session. The instance dict lives on the
        # PARENT ctx — after fork_ctx() above, we need to look it up on the
        # parent, not the fork (which would have its own empty dict). We
        # traverse back to the originator's ctx via contextvars reset.
        # Simpler approach: use the shared `ctx.session` as a bridge since
        # it survives forks unchanged. Store instances on session instead
        # of ctx.
        _sub_cache: dict | None = None
        if _session_for_cache := getattr(get_ctx(), "session", None):
            # Lazily attach the cache to session so it survives fork_ctx
            # (which gives the fork a fresh subagent_instances dict).
            if not hasattr(_session_for_cache, "_subagent_instances"):
                _session_for_cache._subagent_instances = {}
            _sub_cache = _session_for_cache._subagent_instances
        else:
            _sub_cache = get_ctx().subagent_instances

        resumed = False
        existing_sub = None
        resumed_task_id: str | None = None
        if task_id and _sub_cache is not None:
            existing_sub = _sub_cache.get(task_id)
            if existing_sub is not None:
                resumed = True
                resumed_task_id = task_id

        if resumed and existing_sub is not None:
            sub = existing_sub
        elif builtin_spec is not None:
            from aru.agent_factory import create_agent_from_spec
            extra = f"The current working directory is: {cwd}\n"
            if context:
                extra += f"\nAdditional context:\n{context}\n"
            sub = await create_agent_from_spec(
                builtin_spec,
                session=get_ctx().session,
                extra_instructions=extra,
            )
            sub.name = f"{builtin_spec.name}-{agent_id}"
        elif _agent_name and _agent_name in custom_agent_defs:
            agent_def = custom_agent_defs[_agent_name]
            agent_perm = agent_def.permission
            tools = resolve_tools(agent_def.tools) if agent_def.tools else list(_SUBAGENT_TOOLS)
            tools = [t for t in tools if t is not delegate_task]
            instructions = agent_def.system_prompt + f"\nThe current working directory is: {cwd}\n"
            if context:
                instructions += f"\nAdditional context:\n{context}\n"
            model_ref = agent_def.model or small_model_ref
            sub = Agent(
                name=f"{agent_def.name}-{agent_id}",
                model=create_model(model_ref, max_tokens=4096),
                tools=tools,
                instructions=instructions,
                markdown=True,
            )
        else:
            instructions = f"""\
You are a sub-agent (#{agent_id}) working on a specific task. Be focused and concise.
Complete the task and return a clear summary of what you did or found.
The current working directory is: {cwd}
Do not create documentation files unless explicitly asked.
"""
            if context:
                instructions += f"\nAdditional context:\n{context}\n"

            sub = Agent(
                name=f"SubAgent-{agent_id}",
                model=create_model(small_model_ref, max_tokens=4096),
                tools=_SUBAGENT_TOOLS,
                instructions=instructions,
                markdown=True,
            )

        if resumed and existing_sub is not None:
            label = f"{existing_sub.name}"
            task_id_for_output = resumed_task_id
        elif builtin_spec is not None:
            label = f"{builtin_spec.name}-{agent_id}"
            task_id_for_output = f"sa-{agent_id}"
        else:
            label = f"SubAgent-{agent_id}"
            task_id_for_output = f"sa-{agent_id}"

        # Register the Agent instance in the session-scoped cache so future
        # calls with task_id=<this> resume it. Done BEFORE execution so that
        # nested delegations spawned by this sub-agent can reference its
        # own id (unlikely but possible). The store is idempotent on resume.
        if not resumed and _sub_cache is not None:
            _sub_cache[task_id_for_output] = sub

        async def _execute_with_streaming(agent_instance) -> str:
            """Run a sub-agent with streaming events for live progress display.

            Observes `ctx.abort_event` between events. When set (e.g. by the
            REPL's Ctrl+C handler), returns a cancelled marker immediately
            instead of pumping further events — the Agno loop is left to
            clean itself up via its own abort plumbing (we drop the iterator).

            Populates a `SubagentTrace` on the session so `/subagents` can
            render the invocation tree. Trace status transitions:
                running → completed (normal exit)
                        → cancelled (abort_event fired)
                        → error (exception)
            """
            import time as _time
            from agno.run.agent import (
                RunContentEvent,
                RunOutput,
                ToolCallCompletedEvent,
                ToolCallStartedEvent,
            )
            from aru.display import subagent_progress
            from aru.runtime import is_aborted
            from aru.session import SubagentTrace

            result_content = ""
            run_output = None
            _tool_starts: dict[str, float] = {}

            # Register a trace entry for this invocation. parent_id is the
            # agent_id of the spawning scope (None = primary agent). The trace
            # object is mutated as events flow in — no need to re-append.
            _ctx = get_ctx()
            _session = getattr(_ctx, "session", None)
            _trace = SubagentTrace(
                task_id=str(agent_id),
                parent_id=getattr(_ctx, "agent_id", None),
                agent_name=sub.name,
                task=(task or "")[:200],
                started_at=_time.monotonic(),
            )
            if _session is not None and hasattr(_session, "subagent_traces"):
                _session.subagent_traces.append(_trace)

            try:
                async for event in agent_instance.arun(task, stream=True, stream_events=True, yield_run_output=True):
                    if is_aborted():
                        _trace.status = "cancelled"
                        _trace.ended_at = _time.monotonic()
                        return f"[{label} | task_id={task_id_for_output}] Cancelled by user."
                    if isinstance(event, RunOutput):
                        run_output = event
                        break
                    elif isinstance(event, ToolCallStartedEvent):
                        if hasattr(event, "tool") and event.tool:
                            t_id = getattr(event.tool, "tool_call_id", None) or (event.tool.tool_name or "tool")
                        else:
                            t_id = getattr(event, "tool_call_id", None) or getattr(event, "tool_name", "tool")
                        _tool_starts[t_id] = _time.monotonic()
                    elif isinstance(event, ToolCallCompletedEvent):
                        if hasattr(event, "tool") and event.tool:
                            t_id = getattr(event.tool, "tool_call_id", None) or getattr(event.tool, "tool_name", "tool")
                            t_name = event.tool.tool_name or "tool"
                            t_args = event.tool.tool_args
                        else:
                            t_id = getattr(event, "tool_call_id", None) or getattr(event, "tool_name", "tool")
                            t_name = getattr(event, "tool_name", "tool")
                            t_args = getattr(event, "tool_args", None)
                        dur = _time.monotonic() - _tool_starts.pop(t_id, _time.monotonic())
                        subagent_progress(label, t_name, t_args if isinstance(t_args, dict) else None, duration=dur)
                        _trace.tool_calls.append({
                            "tool": t_name,
                            "args_preview": (str(t_args) if t_args else "")[:150],
                            "duration": round(dur, 3),
                        })
                    elif isinstance(event, RunContentEvent):
                        if hasattr(event, "content") and event.content:
                            result_content += event.content
            except Exception:
                _trace.status = "error"
                _trace.ended_at = _time.monotonic()
                raise

            if run_output and hasattr(run_output, "metrics") and run_output.metrics:
                try:
                    session = get_ctx().session
                    if session is not None:
                        m = run_output.metrics
                        _in = getattr(m, "input_tokens", 0) or 0
                        _out = getattr(m, "output_tokens", 0) or 0
                        session.total_input_tokens += _in
                        session.total_output_tokens += _out
                        session.total_cache_read_tokens += getattr(m, "cache_read_tokens", 0) or 0
                        session.total_cache_write_tokens += getattr(m, "cache_write_tokens", 0) or 0
                        session.api_calls += 1
                        _trace.tokens_in = _in
                        _trace.tokens_out = _out
                except (LookupError, AttributeError):
                    pass

            final_text = run_output.content if run_output and run_output.content else result_content
            _trace.status = "completed"
            _trace.ended_at = _time.monotonic()
            _trace.result = (final_text or "")[:500]
            # Persist trace to disk — fire-and-forget so a slow filesystem
            # never blocks the sub-agent's return. Enables `/subagents list`
            # to surface traces from prior sessions and `/subagent <id>` to
            # replay details after a restart.
            _persist_trace_async(_session, _trace)
            # Header format — `task_id=` lets the LLM resume by passing the
            # same id back. Mirrors opencode/tool/task.ts:138-144.
            header = f"[{label} | task_id={task_id_for_output}]"
            if final_text:
                return _truncate_output(f"{header} {final_text}")
            return f"{header} Task completed but no output was returned."

        try:
            from aru.permissions import permission_scope
            with permission_scope(agent_perm):
                return await _execute_with_streaming(sub)
        except Exception as e:
            try:
                from aru.permissions import permission_scope as _ps
                if builtin_spec is not None:
                    from aru.agent_factory import create_agent_from_spec
                    extra = f"The current working directory is: {cwd}\n"
                    if context:
                        extra += f"\nAdditional context:\n{context}\n"
                    sub_retry = await create_agent_from_spec(
                        builtin_spec,
                        session=get_ctx().session,
                        extra_instructions=extra,
                    )
                    sub_retry.name = f"{builtin_spec.name}-{agent_id}"
                else:
                    sub_retry = Agent(
                        name=sub.name,
                        model=sub.model,
                        tools=sub.tools,
                        instructions=sub.instructions,
                        markdown=True,
                    )
                with _ps(agent_perm):
                    return await _execute_with_streaming(sub_retry)
            except Exception as e2:
                return f"[{label}] Error (after retry): {e2}"

    if run_in_background:
        # Fire-and-forget — dispatch the run, return a handle immediately.
        # When the background task finishes, we append to the primary
        # session's pending_notifications; the REPL drains that list before
        # the next turn so the model sees a <task-notification>.
        import uuid
        bg_id = f"bg-{uuid.uuid4().hex[:8]}"
        parent_session = getattr(get_ctx(), "session", None)

        async def _bg_wrapper() -> None:
            result = None
            try:
                result = await _run()
            except Exception as exc:  # pragma: no cover — defensive
                result = f"[bg-{bg_id}] Background task errored: {exc}"
            if parent_session is not None and hasattr(parent_session, "pending_notifications"):
                parent_session.pending_notifications.append({
                    "task_id": bg_id,
                    "result": result or f"[bg-{bg_id}] No output",
                    "at": asyncio.get_event_loop().time(),
                })

        asyncio.create_task(_bg_wrapper())
        return (
            f"[BackgroundTask | task_id={bg_id}] Dispatched sub-agent "
            f"'{agent_name or 'generic'}' in background. You will receive "
            f"a <task-notification> message when it completes. Do NOT poll — "
            f"continue with other work."
        )

    return await asyncio.create_task(_run())


_FALLBACK_DOC = """Delegate a task to a sub-agent that runs autonomously. Multiple calls run concurrently.
Use for independent research or subtasks to keep your own context clean.

Args:
    task: What the sub-agent should do.
    context: Optional extra context (file paths, constraints).
    agent_name: Name of a specialised agent to use (e.g. "explorer").
    task_id: If set, resume a prior sub-agent session instead of creating a
        fresh one. Pass the task_id returned by a previous delegate_task call.
"""


def _load_delegate_prompt() -> str:
    """Read the shipped delegate_prompt.txt from the package resources.

    Falls back to a minimal inline prompt if the file is not present (e.g.
    an editable install whose package data was not wired). The fallback
    exists so the tool's schema never renders blank for the model.
    """
    try:
        import importlib.resources
        return (importlib.resources.files("aru.tools") / "delegate_prompt.txt").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        return _FALLBACK_DOC


def _render_agent_list() -> str:
    """Render the built-in + custom agents section injected into the prompt.

    Built-in subagents come from the AGENTS catalog (mode="subagent"); custom
    agents are provided by `set_custom_agents` (project-level .agents/). The
    list format mirrors what the model sees — one bullet per agent with its
    description.
    """
    lines: list[str] = ["## Available sub-agents", ""]

    # Built-in subagents from the catalog
    try:
        from aru.agents.catalog import AGENTS
        for key, spec in AGENTS.items():
            if getattr(spec, "mode", "") != "subagent":
                continue
            description = getattr(spec, "description", "") or f"Built-in `{key}` agent."
            lines.append(f'- `agent_name="{key}"`: {description}')
    except Exception:
        # Minimum fallback so callers without a catalog still see explorer
        lines.append(
            '- `agent_name="explorer"`: Fast read-only codebase exploration. '
            'Specify thoroughness: "quick" | "medium" | "very thorough".'
        )

    # Custom agents from .agents/agents/*.md
    try:
        custom_agent_defs = get_ctx().custom_agent_defs or {}
    except LookupError:
        custom_agent_defs = {}
    for name, agent_def in custom_agent_defs.items():
        description = getattr(agent_def, "description", "") or f"Custom `{name}` agent."
        lines.append(f'- `agent_name="{name}"`: {description}')

    if len(lines) > 2:
        lines.append("")
        lines.append(
            "When a specialised agent matches the task, you MUST pass its "
            "name in `agent_name`. Omit `agent_name` only when no specialist fits."
        )
    return "\n".join(lines)


def _update_delegate_task_docstring():
    """Dynamically update delegate_task's docstring.

    The docstring is the primary surface the LLM sees describing the tool.
    We combine a shipped template (`delegate_prompt.txt`, ~80 lines of
    coordination guidance) with a dynamically-rendered agent list so
    custom agents registered at runtime appear as first-class options.
    """
    base = _load_delegate_prompt()
    agent_list = _render_agent_list()
    if "{agent_list}" in base:
        delegate_task.__doc__ = base.replace("{agent_list}", agent_list)
    else:
        # Fallback doc has no placeholder — append agent list
        delegate_task.__doc__ = base + "\n\n" + agent_list


def set_custom_agents(agents: dict):
    """Register custom agent definitions and update delegate_task docstring."""
    ctx = get_ctx()
    ctx.custom_agent_defs = {k: v for k, v in agents.items() if v.mode == "subagent"}
    _update_delegate_task_docstring()
