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


async def delegate_task(task: str, context: str = "", agent_name: str = "") -> str:
    """Delegate a task to a sub-agent that runs autonomously. Multiple calls run concurrently.
    Use for independent research or subtasks to keep your own context clean.

    Args:
        task: What the sub-agent should do.
        context: Optional extra context (file paths, constraints).
        agent_name: Name of a specialized agent to use instead of the generic sub-agent.
    """

    async def _run() -> str:
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

        if _builtin == "explorer":
            from aru.agent_factory import create_agent_from_spec
            from aru.agents.catalog import AGENTS
            extra = f"The current working directory is: {cwd}\n"
            if context:
                extra += f"\nAdditional context:\n{context}\n"
            sub = await create_agent_from_spec(
                AGENTS["explorer"],
                session=get_ctx().session,
                extra_instructions=extra,
            )
            sub.name = f"Explorer-{agent_id}"
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

        label = f"Explorer-{agent_id}" if _builtin == "explorer" else f"SubAgent-{agent_id}"

        async def _execute_with_streaming(agent_instance) -> str:
            """Run a sub-agent with streaming events for live progress display."""
            import time as _time
            from agno.run.agent import (
                RunContentEvent,
                RunOutput,
                ToolCallCompletedEvent,
                ToolCallStartedEvent,
            )
            from aru.display import subagent_progress

            result_content = ""
            run_output = None
            _tool_starts: dict[str, float] = {}

            async for event in agent_instance.arun(task, stream=True, stream_events=True, yield_run_output=True):
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
                elif isinstance(event, RunContentEvent):
                    if hasattr(event, "content") and event.content:
                        result_content += event.content

            if run_output and hasattr(run_output, "metrics") and run_output.metrics:
                try:
                    session = get_ctx().session
                    if session is not None:
                        m = run_output.metrics
                        session.total_input_tokens += getattr(m, "input_tokens", 0) or 0
                        session.total_output_tokens += getattr(m, "output_tokens", 0) or 0
                        session.total_cache_read_tokens += getattr(m, "cache_read_tokens", 0) or 0
                        session.total_cache_write_tokens += getattr(m, "cache_write_tokens", 0) or 0
                        session.api_calls += 1
                except (LookupError, AttributeError):
                    pass

            final_text = run_output.content if run_output and run_output.content else result_content
            if final_text:
                return _truncate_output(f"[{label}] {final_text}")
            return f"[{label}] Task completed but no output was returned."

        try:
            from aru.permissions import permission_scope
            with permission_scope(agent_perm):
                return await _execute_with_streaming(sub)
        except Exception as e:
            try:
                from aru.permissions import permission_scope as _ps
                if _builtin == "explorer":
                    from aru.agent_factory import create_agent_from_spec
                    from aru.agents.catalog import AGENTS
                    extra = f"The current working directory is: {cwd}\n"
                    if context:
                        extra += f"\nAdditional context:\n{context}\n"
                    sub_retry = await create_agent_from_spec(
                        AGENTS["explorer"],
                        session=get_ctx().session,
                        extra_instructions=extra,
                    )
                    sub_retry.name = f"Explorer-{agent_id}"
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

    return await asyncio.create_task(_run())


def _update_delegate_task_docstring():
    """Dynamically update delegate_task's docstring to list available subagents."""
    base_doc = """Delegate a task to a sub-agent that runs autonomously. Multiple calls run concurrently.
    Use for independent research or subtasks to keep your own context clean.

    Args:
        task: What the sub-agent should do.
        context: Optional extra context (file paths, constraints).
        agent_name: Name of a specialized agent to use. ALWAYS prefer a specialized agent when one matches the task.

    Built-in agents (always available):
    - agent_name="explorer": Fast read-only codebase exploration agent. Use for searching files, \
finding patterns, reading code, and understanding code structure. Optimized for speed with parallel tool calls. \
When calling this agent, specify the desired thoroughness level: "quick" for basic searches, \
"medium" for moderate exploration, or "very thorough" for comprehensive analysis."""

    custom_agent_defs = get_ctx().custom_agent_defs
    if custom_agent_defs:
        lines = [f"\n\n    IMPORTANT: When a specialized agent matches the task, you MUST pass its name in the agent_name parameter."]
        lines.append(f"    Additional specialized agents:")
        for name, agent_def in custom_agent_defs.items():
            lines.append(f'    - agent_name="{name}": {agent_def.description}')
        lines.append(f"\n    If no specialized agent fits, omit the agent_name parameter to use a generic sub-agent.")
        base_doc += "\n".join(lines)

    delegate_task.__doc__ = base_doc


def set_custom_agents(agents: dict):
    """Register custom agent definitions and update delegate_task docstring."""
    ctx = get_ctx()
    ctx.custom_agent_defs = {k: v for k, v in agents.items() if v.mode == "subagent"}
    _update_delegate_task_docstring()
