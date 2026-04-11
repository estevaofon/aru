"""Agent creation: general-purpose and custom agent instantiation."""

from __future__ import annotations

import functools
import inspect
import logging

from aru.agents.base import build_instructions as _build_instructions
from aru.config import AgentConfig, CustomAgent
from aru.providers import create_model
from aru.session import Session

logger = logging.getLogger("aru.agent_factory")


def _wrap_tools_with_hooks(tools: list) -> list:
    """Wrap tool functions to fire tool.execute.before/after plugin hooks.

    Before hook can mutate args; after hook can mutate the result.
    If a before hook raises, the tool is not executed and the error is returned.
    """
    from aru.runtime import get_ctx

    async def _fire(event_name: str, data: dict) -> dict:
        try:
            ctx = get_ctx()
            mgr = ctx.plugin_manager
            if mgr is not None and mgr.loaded:
                event = await mgr.fire(event_name, data)
                return event.data
        except (LookupError, AttributeError):
            pass
        return data

    def _wrap_one(fn):
        if not callable(fn) or getattr(fn, "_hook_wrapped", False):
            return fn

        @functools.wraps(fn)
        async def wrapper(**kwargs):
            tool_name = fn.__name__
            # Before hook — plugins can mutate args or raise PermissionError to block
            try:
                before_data = await _fire("tool.execute.before", {
                    "tool_name": tool_name,
                    "args": kwargs,
                })
                kwargs = before_data.get("args", kwargs)
            except PermissionError as e:
                return f"BLOCKED by plugin: {e}. Do NOT retry this operation."

            # Execute the tool
            if inspect.iscoroutinefunction(fn):
                result = await fn(**kwargs)
            else:
                result = fn(**kwargs)

            # After hook — plugins can mutate the result
            after_data = await _fire("tool.execute.after", {
                "tool_name": tool_name,
                "args": kwargs,
                "result": result,
            })
            return after_data.get("result", result)

        wrapper._hook_wrapped = True
        return wrapper

    return [_wrap_one(t) for t in tools]


def create_general_agent(
    session: Session,
    config: AgentConfig | None = None,
    model_override: str | None = None,
    env_context: str = "",
):
    """Create the general-purpose agent.

    Args:
        env_context: Environment context (cwd, tree, git status) to include
            in the system prompt. Placed in instructions so it's cacheable.
    """
    from agno.agent import Agent

    from aru.tools.codebase import GENERAL_TOOLS
    tools = _wrap_tools_with_hooks(GENERAL_TOOLS)

    extra = config.get_extra_instructions() if config else ""
    if env_context:
        extra = f"{extra}\n\n{env_context}" if extra else env_context
    model_ref = model_override or session.model_ref

    return Agent(
        name="Aru",
        model=create_model(model_ref, max_tokens=8192),
        tools=tools,
        instructions=_build_instructions("general", extra),
        markdown=True,
        tool_call_limit=20,
    )


def create_custom_agent_instance(agent_def: CustomAgent, session: Session,
                                  config: AgentConfig | None = None,
                                  env_context: str = ""):
    """Create an Agno Agent from a CustomAgent definition."""
    from agno.agent import Agent
    from aru.agents.base import BASE_INSTRUCTIONS
    from aru.tools.codebase import resolve_tools

    model_ref = agent_def.model or session.model_ref
    tools = _wrap_tools_with_hooks(resolve_tools(agent_def.tools))

    extra = config.get_extra_instructions() if config else ""
    if env_context:
        extra = f"{extra}\n\n{env_context}" if extra else env_context
    parts = [agent_def.system_prompt, BASE_INSTRUCTIONS]
    if extra:
        parts.append(extra)
    instructions = "\n\n".join(parts)

    return Agent(
        name=agent_def.name,
        model=create_model(model_ref, max_tokens=8192),
        tools=tools,
        instructions=instructions,
        markdown=True,
        tool_call_limit=agent_def.max_turns or 20,
    )
