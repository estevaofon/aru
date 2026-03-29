"""Executor agent - implements changes based on plans or direct instructions."""

from agno.agent import Agent
from agno.compression.manager import CompressionManager
from agno.utils.log import log_warning

from aru.agents.base import build_instructions
from aru.providers import create_model
from aru.tools.codebase import EXECUTOR_TOOLS, _get_small_model_ref

# Max chars for truncation fallback when compression fails
_TRUNCATE_FALLBACK = 3000


class _SafeCompressionManager(CompressionManager):
    """CompressionManager that truncates on failure instead of leaving messages uncompressed.

    Agno's default behavior: if compression returns None, the message stays with
    compressed_content=None → should_compress() fires again → infinite retry loop.
    This subclass marks failed messages with a truncated version so the loop moves on.
    """

    async def acompress(self, messages, run_metrics=None):
        # Track which messages are currently uncompressed
        before = {id(m) for m in messages if m.role == "tool" and m.compressed_content is None}
        await super().acompress(messages, run_metrics=run_metrics)
        # Any message still uncompressed after super() = compression failed
        for msg in messages:
            if id(msg) in before and msg.compressed_content is None:
                content_str = str(msg.content or "")
                msg.compressed_content = content_str[:_TRUNCATE_FALLBACK] + (
                    "... [truncated, compression failed]" if len(content_str) > _TRUNCATE_FALLBACK else ""
                )
                log_warning(f"Compression fallback (truncate) for {msg.tool_name}")


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
        compression_manager=_SafeCompressionManager(
            model=create_model(_get_small_model_ref(), max_tokens=2048),
            compress_tool_results=True,
            compress_tool_results_limit=15,
        ),
        tool_call_limit=None,
    )
