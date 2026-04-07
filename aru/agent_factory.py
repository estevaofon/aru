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
):
    """Create the general-purpose agent."""
    from agno.agent import Agent
    from agno.compression.manager import CompressionManager

    from aru.tools.codebase import GENERAL_TOOLS
    from aru.runtime import get_ctx

    extra = config.get_extra_instructions() if config else ""
    model_ref = model_override or session.model_ref

    return Agent(
        name="Aru",
        model=create_model(model_ref, max_tokens=8192),
        tools=GENERAL_TOOLS,
        instructions=_build_instructions("general", extra),
        markdown=True,
        compress_tool_results=True,
        compression_manager=CompressionManager(
            model=create_model(get_ctx().small_model_ref, max_tokens=1024),
            compress_tool_results=True,
            compress_tool_results_limit=25,
        ),
        tool_call_limit=20,
    )


def create_custom_agent_instance(agent_def: CustomAgent, session: Session,
                                  config: AgentConfig | None = None):
    """Create an Agno Agent from a CustomAgent definition."""
    from agno.agent import Agent
    from agno.compression.manager import CompressionManager
    from aru.agents.base import BASE_INSTRUCTIONS
    from aru.tools.codebase import resolve_tools
    from aru.runtime import get_ctx

    model_ref = agent_def.model or session.model_ref
    tools = resolve_tools(agent_def.tools)

    extra = config.get_extra_instructions() if config else ""
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
        compress_tool_results=True,
        compression_manager=CompressionManager(
            model=create_model(get_ctx().small_model_ref, max_tokens=1024),
            compress_tool_results=True,
            compress_tool_results_limit=25,
        ),
        tool_call_limit=agent_def.max_turns or 20,
    )
