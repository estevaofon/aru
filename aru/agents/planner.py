"""Planning agent - analyzes codebase and creates implementation plans."""

from agno.agent import Agent

from aru.agents.base import build_instructions
from aru.providers import create_model
from aru.tools.codebase import glob_search, grep_search, list_directory, read_file
from aru.tools.indexer import semantic_search
from aru.tools.ast_tools import code_structure, find_dependencies
from aru.tools.ranker import rank_files

# Planner uses read-only tools only — no write/edit/bash
PLANNER_TOOLS = [
    read_file, glob_search, grep_search, list_directory,
    semantic_search, code_structure, find_dependencies, rank_files,
]


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
    )
