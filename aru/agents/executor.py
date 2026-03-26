"""Executor agent - implements changes based on plans or direct instructions."""

from agno.agent import Agent

from aru.agents.base import build_instructions
from aru.providers import create_model
from aru.tools.codebase import ALL_TOOLS


def create_executor(model_ref: str = "anthropic/claude-sonnet-4-5", extra_instructions: str = "") -> Agent:
    """Create and return the executor agent.

    Args:
        model_ref: Provider/model reference (e.g., "anthropic/claude-sonnet-4-5", "ollama/llama3.1").
        extra_instructions: Additional instructions to append.
    """
    return Agent(
        name="Executor",
        model=create_model(model_ref, max_tokens=8192),
        tools=ALL_TOOLS,
        instructions=build_instructions("executor", extra_instructions),
        markdown=True,
    )
