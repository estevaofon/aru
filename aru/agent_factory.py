"""Agent creation: general-purpose and custom agent instantiation."""

from __future__ import annotations

from aru.agents.base import build_instructions as _build_instructions
from aru.config import AgentConfig, CustomAgent
from aru.providers import create_model
from aru.session import Session


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
    tools = GENERAL_TOOLS

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
    tools = resolve_tools(agent_def.tools)

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
