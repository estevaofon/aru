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


# Backward-compat re-export. The canonical list now lives in
# aru.tool_policy.PLAN_MODE_BLOCKED_TOOLS; external callers (tests,
# docs) that import it from here keep working.
from aru.tool_policy import PLAN_MODE_BLOCKED_TOOLS as _PLAN_MODE_BLOCKED_TOOLS


def _wrap_tools_with_hooks(tools: list) -> list:
    """Wrap tool functions with a single tool-policy gate and plugin hooks.

    The policy gate (plan mode + active-skill disallowed_tools) is
    evaluated by `aru.tool_policy.evaluate_tool_policy` — a single
    decision function shared with `aru.permissions.resolve_permission`,
    so both the wrapper and per-tool permission checks see the same
    answer. When a tool is denied by multiple rules at once, the policy
    layer returns one combined BLOCKED message rather than two
    sequential contradictory ones (this is the scenario-1 fix of the
    combinatorial gate audit).

    Plugin hooks run AFTER the policy gate so a plugin's
    tool.execute.before hook cannot bypass plan-mode / skill rules.
    """

    def _wrap_one(fn):
        if not callable(fn) or getattr(fn, "_hook_wrapped", False):
            return fn

        @functools.wraps(fn)
        async def wrapper(**kwargs):
            tool_name = fn.__name__
            # Unified policy gate — one function, one decision, one
            # message on denial (combines plan-mode + skill rules when
            # both apply).
            from aru.tool_policy import evaluate_tool_policy
            decision = evaluate_tool_policy(tool_name)
            if not decision.allowed:
                return decision.message
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

    # chat.params — plugins can modify LLM parameters. max_tokens is
    # deliberately NOT exposed: it is coupled with the recovery loop in
    # runner.py and mutating it from a plugin can break mid-thought
    # recovery. Plugins that need to bound output should do so via model
    # selection or temperature, not raw token limits.
    data = await _fire_hook("chat.params", {
        "model": model_ref,
        "temperature": None,  # let plugin set if desired
    })
    model_ref = data.get("model", model_ref)

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
    # Merge spec-level extra instructions (static, agent-specific policy like
    # "you are read-only, never call write tools") with caller-provided extras
    # (dynamic, session-specific context like cwd or AGENTS.md). Spec text
    # comes first so the agent's baseline policy is established before any
    # session-specific text that might try to override it.
    combined_extra = "\n\n".join(
        part for part in (spec.extra_instructions, extra_instructions) if part
    )
    instructions = _build_instructions(spec.role, combined_extra)

    instructions, resolved_model, max_tokens = await _apply_chat_hooks(
        instructions, resolved_model, spec.name, max_tokens=spec.max_tokens,
    )

    reasoning_override = session.reasoning_override if session is not None else None

    return Agent(
        name=spec.name,
        model=create_model(
            resolved_model,
            max_tokens=max_tokens,
            use_reasoning=spec.use_reasoning,
            reasoning_override=reasoning_override,
        ),
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
        model=create_model(
            model_ref,
            max_tokens=max_tokens,
            reasoning_override=session.reasoning_override,
        ),
        tools=tools,
        instructions=instructions,
        markdown=True,
        tool_call_limit=agent_def.max_turns,
    )
