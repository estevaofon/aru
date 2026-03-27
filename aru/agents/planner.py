"""Planning agent - analyzes codebase and creates implementation plans."""

from agno.agent import Agent
from agno.compression.manager import CompressionManager

from aru.agents.base import build_instructions
from aru.providers import create_model
from aru.tools.codebase import (
    _get_small_model_ref,
    glob_search, grep_search, list_directory, read_file, read_file_smart,
    delegate_research,
)
from aru.tools.indexer import semantic_search
from aru.tools.ast_tools import code_structure, find_dependencies
from aru.tools.ranker import rank_files

REVIEWER_INSTRUCTIONS = """\
You are a plan scope reviewer. You receive a user request and a generated implementation plan.
Your ONLY job: ensure the plan does not add more deliverables than the user explicitly asked for.

Rules:
- Count the concrete deliverables named in the user request (functions, classes, files, bug fixes)
- If the plan adds MORE than what was requested, remove the excess steps
- Be conservative: only remove steps that clearly add things the user did NOT name
- Never add steps, never rewrite steps, never change wording
- Return the plan in the EXACT same markdown format (## Summary then ## Steps)
- If the plan is already correct, return it unchanged

Return ONLY the markdown plan. No explanation, no preamble.\
"""

# Planner uses read-only tools only — no write/edit/bash
PLANNER_TOOLS = [
    read_file, read_file_smart, delegate_research,
    glob_search, grep_search, list_directory,
    semantic_search, code_structure, find_dependencies, rank_files,
]


async def review_plan(request: str, plan: str) -> str:
    """Review a generated plan against the original request, trimming scope creep.

    Uses the small/fast model — no tools, single call, no iteration.
    Returns the corrected plan (or the original if the reviewer fails).
    """
    reviewer = Agent(
        name="Reviewer",
        model=create_model(_get_small_model_ref(), max_tokens=2048),
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
        # Compress tool results after 2 uncompressed tool calls to save tokens
        compress_tool_results=True,
        compression_manager=CompressionManager(
            model=create_model(_get_small_model_ref(), max_tokens=1024),
            compress_tool_results=True,
            compress_tool_results_limit=2,
        ),
        tool_call_limit=10,
    )
