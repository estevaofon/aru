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


# Tools blocked while the session is in plan mode. Read-only tools (read,
# glob, grep, list_directory, web_search, web_fetch, etc.) are NOT in this
# set — the agent needs them to research and write the plan. Mutating or
# execution-capable tools are gated: the agent must call exit_plan_mode and
# get user approval before running any of these.
_PLAN_MODE_BLOCKED_TOOLS: frozenset[str] = frozenset({
    "edit_file",
    "edit_files",
    "write_file",
    "write_files",
    "bash",
    "delegate_task",
})


def _wrap_tools_with_hooks(tools: list) -> list:
    """Wrap tool functions to fire tool.execute.before/after plugin hooks.

    Before hook can mutate args; after hook can mutate the result.
    If a before hook raises, the tool is not executed and the error is returned.

    Also enforces the plan-mode gate: when `session.plan_mode` is True,
    any tool in `_PLAN_MODE_BLOCKED_TOOLS` short-circuits with a structured
    BLOCKED message telling the agent to call `exit_plan_mode` first. The
    gate runs BEFORE plugin hooks so plan mode is the highest-priority
    enforcement; plugins cannot accidentally bypass it.
    """

    def _wrap_one(fn):
        if not callable(fn) or getattr(fn, "_hook_wrapped", False):
            return fn

        @functools.wraps(fn)
        async def wrapper(**kwargs):
            tool_name = fn.__name__
            # Plan-mode gate — fires before any other logic so a mutating
            # tool never reaches the permission layer or the actual executor.
            if tool_name in _PLAN_MODE_BLOCKED_TOOLS:
                try:
                    from aru.runtime import get_ctx
                    session = getattr(get_ctx(), "session", None)
                except (LookupError, AttributeError):
                    session = None
                if session is not None and getattr(session, "plan_mode", False):
                    return (
                        f"BLOCKED: plan mode is active. Mutating tools "
                        f"(edit/write/bash/delegate_task) are blocked until the "
                        f"user approves the plan. Finish writing the plan as "
                        f"your next assistant message, then call "
                        f"exit_plan_mode(plan=<full plan text>) to request "
                        f"approval. Do NOT retry {tool_name}."
                    )
            # Active-skill disallowed-tools gate — honors the `disallowed-tools`
            # frontmatter field of the currently active skill. Mirrors the
            # plan-mode gate pattern above; runs before plugin hooks so a skill
            # can hard-block a tool regardless of permission/plugin state.
            try:
                from aru.runtime import get_ctx
                ctx = get_ctx()
                session = getattr(ctx, "session", None)
                config = getattr(ctx, "config", None)
            except (LookupError, AttributeError):
                session = None
                config = None
            if session is not None and config is not None:
                active = getattr(session, "active_skill", None)
                skills = getattr(config, "skills", None) or {}
                active_skill_obj = skills.get(active) if active else None
                disallowed = getattr(active_skill_obj, "disallowed_tools", None) or []
                if tool_name in disallowed:
                    return (
                        f"BLOCKED: tool `{tool_name}` is disallowed by the "
                        f"currently active skill `{active}`. Read the skill's "
                        f"SKILL.md for the prescribed path. Do NOT retry "
                        f"`{tool_name}`; use the alternative the skill specifies "
                        f"(commonly: write the output to a `.md` file via "
                        f"`write_file` instead of using in-session state)."
                    )
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
                            max_tokens: int | None = None) -> tuple[str, str, int | None]:
    """Apply chat.system.transform and chat.params hooks to agent creation params.

    Returns (instructions, model_ref, max_tokens) — possibly modified by plugins.
    When max_tokens is None, providers.create_model will use the model's full cap.
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

    # Apply chat hooks (system.transform + params). max_tokens=None → provider cap.
    instructions, model_ref, max_tokens = await _apply_chat_hooks(
        instructions, model_ref, agent_def.name, max_tokens=None,
    )

    return Agent(
        name=agent_def.name,
        model=create_model(model_ref, max_tokens=max_tokens),
        tools=tools,
        instructions=instructions,
        markdown=True,
        tool_call_limit=agent_def.max_turns,
    )
