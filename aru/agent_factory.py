"""Agent creation: catalog-driven factory plus custom agent instantiation."""

from __future__ import annotations

import functools
import inspect
import logging

from aru.agents.base import build_instructions as _build_instructions
from aru.agents.catalog import AGENTS, AgentSpec
from aru.config import AgentConfig, CustomAgent
from aru.providers import create_model
from aru.session import Session

logger = logging.getLogger("aru.agent_factory")


async def _fire_hook(event_name: str, data: dict) -> dict:
    """Fire a plugin hook and return the (possibly mutated) event data."""
    try:
        from aru.runtime import get_ctx
        ctx = get_ctx()
        mgr = ctx.plugin_manager
        if mgr is not None and mgr.loaded:
            event = await mgr.fire(event_name, data)
            return event.data
    except (LookupError, AttributeError):
        pass
    return data


def _wrap_tools_with_hooks(tools: list) -> list:
    """Wrap tool functions to fire tool.execute.before/after plugin hooks.

    Before hook can mutate args; after hook can mutate the result.
    If a before hook raises, the tool is not executed and the error is returned.
    """

    def _wrap_one(fn):
        if not callable(fn) or getattr(fn, "_hook_wrapped", False):
            return fn

        @functools.wraps(fn)
        async def wrapper(**kwargs):
            tool_name = fn.__name__
            # Before hook — plugins can mutate args or raise PermissionError to block
            try:
                before_data = await _fire_hook("tool.execute.before", {
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
            after_data = await _fire_hook("tool.execute.after", {
                "tool_name": tool_name,
                "args": kwargs,
                "result": result,
            })
            return after_data.get("result", result)

        wrapper._hook_wrapped = True
        return wrapper

    return [_wrap_one(t) for t in tools]


async def _apply_chat_hooks(instructions: str, model_ref: str, agent_name: str,
                            max_tokens: int = 8192) -> tuple[str, str, int]:
    """Apply chat.system.transform and chat.params hooks to agent creation params.

    Returns (instructions, model_ref, max_tokens) — possibly modified by plugins.
    """
    # chat.system.transform — plugins can modify the system prompt
    data = await _fire_hook("chat.system.transform", {
        "system_prompt": instructions,
        "agent": agent_name,
    })
    instructions = data.get("system_prompt", instructions)

    # chat.params — plugins can modify LLM parameters
    data = await _fire_hook("chat.params", {
        "model": model_ref,
        "max_tokens": max_tokens,
        "temperature": None,  # let plugin set if desired
    })
    model_ref = data.get("model", model_ref)
    max_tokens = data.get("max_tokens", max_tokens)

    return instructions, model_ref, max_tokens


async def create_agent_from_spec(
    spec: AgentSpec,
    session: Session | None = None,
    model_ref: str | None = None,
    extra_instructions: str = "",
):
    """Build an Agno Agent from a catalog spec.

    Single construction path for all native agents (build/plan/executor/explorer).
    Resolves model, wraps tools with plugin hooks, and applies chat.system.transform
    and chat.params hooks. Context reduction is handled by aru's own layers
    (`prune_history` for routine tool cleanup, `should_compact` near window limit),
    so no Agno CompressionManager is attached.

    `session` may be None for subagent specs that always use the small model.
    """
    from agno.agent import Agent
    from aru.runtime import get_ctx

    if spec.small_model:
        resolved_model = model_ref or get_ctx().small_model_ref
    else:
        if session is None:
            raise ValueError(f"AgentSpec {spec.name!r} requires a session to resolve the model")
        resolved_model = model_ref or session.model_ref

    tools = _wrap_tools_with_hooks(spec.tools_factory())
    instructions = _build_instructions(spec.role, extra_instructions)

    instructions, resolved_model, max_tokens = await _apply_chat_hooks(
        instructions, resolved_model, spec.name, max_tokens=spec.max_tokens,
    )

    return Agent(
        name=spec.name,
        model=create_model(resolved_model, max_tokens=max_tokens),
        tools=tools,
        instructions=instructions,
        markdown=True,
        tool_call_limit=None,
    )


async def create_general_agent(
    session: Session,
    config: AgentConfig | None = None,
    model_override: str | None = None,
    env_context: str = "",
):
    """Create the general-purpose agent (thin wrapper around the catalog factory)."""
    extra = config.get_extra_instructions() if config else ""
    if env_context:
        extra = f"{extra}\n\n{env_context}" if extra else env_context
    return await create_agent_from_spec(
        AGENTS["build"],
        session,
        model_ref=model_override or session.model_ref,
        extra_instructions=extra,
    )


async def create_custom_agent_instance(agent_def: CustomAgent, session: Session,
                                        config: AgentConfig | None = None,
                                        env_context: str = ""):
    """Create an Agno Agent from a CustomAgent definition."""
    from agno.agent import Agent
    from aru.agents.base import BASE_INSTRUCTIONS
    from aru.tools.registry import resolve_tools

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
    instructions, model_ref, max_tokens = await _apply_chat_hooks(
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
