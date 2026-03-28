"""Executor agent - implements changes based on plans or direct instructions."""

from agno.agent import Agent
from agno.compression.manager import CompressionManager

from aru.agents.base import build_instructions
from aru.providers import create_model
from aru.tools.codebase import EXECUTOR_TOOLS, _get_small_model_ref


def create_executor(model_ref: str = "anthropic/claude-sonnet-4-5", extra_instructions: str = "") -> Agent:
    """Create and return the executor agent.

    Args:
        model_ref: Provider/model reference (e.g., "anthropic/claude-sonnet-4-5", "ollama/llama3.1").
        extra_instructions: Additional instructions to append.
    """
    return Agent(
        name="Executor",
        model=create_model(model_ref, max_tokens=8192),
        tools=EXECUTOR_TOOLS,
        instructions=build_instructions("executor", extra_instructions),
        markdown=True,
        # Compress tool results after 5 uncompressed tool calls to save tokens
        compress_tool_results=True,
        compression_manager=CompressionManager(
            model=create_model(_get_small_model_ref(), max_tokens=1024),
            compress_tool_results=True,
            compress_tool_results_limit=15,
        ),
        tool_call_limit=15,
    )
