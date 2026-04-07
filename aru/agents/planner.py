"""Planning agent - analyzes codebase and creates implementation plans."""

from agno.agent import Agent
from agno.compression.manager import CompressionManager

from aru.agents.base import build_instructions
from aru.providers import create_model
from aru.tools.codebase import (
    glob_search, grep_search, list_directory, read_file, read_file_smart,
)
from aru.runtime import get_ctx

REVIEWER_INSTRUCTIONS = """\
You are a plan scope reviewer. You receive a user request and a generated implementation plan.
Your ONLY job: ensure the plan does not add more deliverables than the user explicitly asked for.

Rules:
- Count EXACTLY how many deliverables the user asked for. "a function" = 1. "two endpoints" = 2. \
  Unquantified plurals = lean minimal.
- If the user said "a" or "one", the plan MUST have exactly 1 deliverable step. \
  Multiple steps that each produce a separate deliverable is scope creep — keep only the best one.
- Multiple steps are OK only when they implement different parts of the SAME deliverable \
  or when the user explicitly asked for multiple things.

CRITICAL — preserve the original plan text:
- You may ONLY delete entire steps that are scope creep. You must NOT rewrite, rephrase, \
  translate, summarize, or simplify any step you keep.
- Copy kept steps EXACTLY as they appear — same language, same wording, same detail level.
- Return the plan in the EXACT same markdown format (## Summary then ## Steps).
- If the plan is already correct, return it UNCHANGED — do not paraphrase it.

Return ONLY the markdown plan. No explanation, no preamble.\
"""

# Planner uses read-only tools only — no write/edit/bash
PLANNER_TOOLS = [
    read_file, read_file_smart,
    glob_search, grep_search, list_directory,
]


async def review_plan(request: str, plan: str) -> str:
    """Review a generated plan against the original request, trimming scope creep.

    Uses the small/fast model — no tools, single call, no iteration.
    Returns the corrected plan (or the original if the reviewer fails).
    """
    reviewer = Agent(
        name="Reviewer",
        model=create_model(get_ctx().small_model_ref, max_tokens=2048),
        instructions=REVIEWER_INSTRUCTIONS,
        markdown=True,
    )
    prompt = f"## User Request\n{request}\n\n## Generated Plan\n{plan}"
    try:
        response = await reviewer.arun(prompt)
        if response and response.content and response.content.strip():
            return response.content.strip()
    except Exception:
        pass
    return plan


def create_planner(model_ref: str = "anthropic/claude-sonnet-4-5", extra_instructions: str = "") -> Agent:
    """Create and return the planner agent.

    Args:
        model_ref: Provider/model reference (e.g., "anthropic/claude-sonnet-4-5", "ollama/llama3.1").
        extra_instructions: Additional instructions to append.
    """
    return Agent(
        name="Planner",
        model=create_model(model_ref, max_tokens=4096),
        tools=PLANNER_TOOLS,
        instructions=build_instructions("planner", extra_instructions),
        markdown=True,
        # Compress tool results after 6 uncompressed tool calls to save tokens
        compress_tool_results=True,
        compression_manager=CompressionManager(
            model=create_model(get_ctx().small_model_ref, max_tokens=1024),
            compress_tool_results=True,
            compress_tool_results_limit=25,
        ),
        tool_call_limit=20,
    )
