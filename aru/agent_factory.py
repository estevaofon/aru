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

    async def _fire_tool_definition(tool_name: str, description: str, parameters: dict) -> dict:
        """Fire tool.definition hook — plugins can modify tool desc/params."""
        try:
            ctx = get_ctx()
            mgr = ctx.plugin_manager
            if mgr is not None and mgr.loaded:
                event = await mgr.fire("tool.definition", {
                    "tool_name": tool_name,
                    "description": description,
                    "parameters": parameters,
                })
                return event.data
        except (LookupError, AttributeError):
            pass
        return {"tool_name": tool_name, "description": description, "parameters": parameters}

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


def _fire_sync_hook(event_name: str, data: dict) -> dict:
    """Fire a plugin hook synchronously (for agent creation context).

    Agent creation happens in sync code, so we need a sync path.
    """
    try:
        from aru.runtime import get_ctx
        ctx = get_ctx()
        mgr = ctx.plugin_manager
        if mgr is not None and mgr.loaded:
            import asyncio
            from aru.plugins.hooks import HookEvent
            event = HookEvent(hook=event_name, data=data or {})
            for hooks in mgr._hooks:
                for handler in hooks.get_handlers(event_name):
                    try:
                        if asyncio.iscoroutinefunction(handler):
                            # Best-effort: try to run async handler
                            try:
                                loop = asyncio.get_running_loop()
                            except RuntimeError:
                                loop = None
                            if loop and loop.is_running():
                                # Can't await in sync context with running loop — skip
                                continue
                            else:
                                asyncio.run(handler(event))
                        else:
                            handler(event)
                    except Exception as e:
                        logger.warning("Hook handler error (%s): %s", event_name, e)
            return event.data
    except (LookupError, AttributeError):
        pass
    return data


def _apply_chat_hooks(instructions: str, model_ref: str, agent_name: str,
                      max_tokens: int = 8192) -> tuple[str, str, int]:
    """Apply chat.system.transform and chat.params hooks to agent creation params.

    Returns (instructions, model_ref, max_tokens) — possibly modified by plugins.
    """
    # chat.system.transform — plugins can modify the system prompt
    data = _fire_sync_hook("chat.system.transform", {
        "system_prompt": instructions,
        "agent": agent_name,
    })
    instructions = data.get("system_prompt", instructions)

    # chat.params — plugins can modify LLM parameters
    data = _fire_sync_hook("chat.params", {
        "model": model_ref,
        "max_tokens": max_tokens,
        "temperature": None,  # let plugin set if desired
    })
    model_ref = data.get("model", model_ref)
    max_tokens = data.get("max_tokens", max_tokens)

    return instructions, model_ref, max_tokens


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
    instructions = _build_instructions("general", extra)

    # Apply chat hooks (system.transform + params)
    instructions, model_ref, max_tokens = _apply_chat_hooks(
        instructions, model_ref, "Aru", max_tokens=8192,
    )

    return Agent(
        name="Aru",
        model=create_model(model_ref, max_tokens=max_tokens),
        tools=tools,
        instructions=instructions,
        markdown=True,
        tool_call_limit=None,
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

    # Apply chat hooks (system.transform + params)
    instructions, model_ref, max_tokens = _apply_chat_hooks(
        instructions, model_ref, agent_def.name, max_tokens=8192,
    )

    return Agent(
        name=agent_def.name,
        model=create_model(model_ref, max_tokens=max_tokens),
        tools=tools,
        instructions=instructions,
        markdown=True,
        tool_call_limit=agent_def.max_turns,
    )
