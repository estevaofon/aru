"""Planning agent - analyzes codebase and creates implementation plans."""

from agno.agent import Agent
from agno.models.anthropic import Claude

from arc.tools.codebase import glob_search, grep_search, list_directory, read_file, web_search, web_fetch
from arc.tools.indexer import semantic_search
from arc.tools.ast_tools import code_structure, find_dependencies
from arc.tools.ranker import rank_files

PLANNER_INSTRUCTIONS = """\
You are a software architect agent. Your job is to analyze codebases and create concise implementation plans.

## How to research
1. ALWAYS read the project's README.md first if it exists to understand the project context (use read_file)
2. Rank files by relevance (use rank_files) to identify where to focus
3. Explore the codebase structure (use list_directory, glob_search, read_file)
4. Search for relevant code patterns (use grep_search, semantic_search)
5. Analyze code structure and dependencies (use code_structure, find_dependencies)

## Output format — STRICT

Your output MUST follow this exact structure. No other format is accepted:

## Summary
- 1-3 bullet points maximum. What needs to be done and which files are involved.

## Steps
- [ ] Step 1: Description (include file paths and specific details)
- [ ] Step 2: Description (include file paths and specific details)
- [ ] Step 3: Description (include file paths and specific details)

## Rules
- The checklist IS the plan. Do NOT write paragraphs of analysis, context, considerations, or risks outside the Summary and Steps sections.
- Be extremely concise. Every word must earn its place.
- Each step must be self-contained enough that an executor agent can complete it independently.
- Include relevant file paths and specific code references in each step.
- Prefer more small steps over fewer large ones.
- Never include documentation files (*.md) unless the user explicitly asked for them.
- Do not plan creation of README.md, CHANGELOG.md, SETUP.md, CONTRIBUTING.md, or similar files.
- The deliverable is working code, not documentation.
"""


def create_planner(model_id: str = "claude-sonnet-4-5-20250929", extra_instructions: str = "") -> Agent:
    """Create and return the planner agent."""
    import os
    import subprocess
    from arc.tools.codebase import get_project_tree
    
    cwd = os.getcwd()
    env_parts = []
    
    tree_text = get_project_tree(cwd, max_depth=3)
    if tree_text:
        env_parts.append(f"Directory Tree (max depth 3):\n```text\n{tree_text}\n```")
        
    try:
        git_status = subprocess.run(["git", "status", "-s"], capture_output=True, text=True, cwd=cwd, timeout=2).stdout.strip()
        if git_status:
            env_parts.append(f"Git status:\n{git_status}")
    except Exception:
        pass
        
    env_context = "\n\n".join(env_parts)
    
    instructions = f"{PLANNER_INSTRUCTIONS}\n\n## Environment Context\n{env_context}"
    
    if extra_instructions:
        instructions = f"{instructions}\n\n{extra_instructions}"
    return Agent(
        name="Planner",
        model=Claude(id=model_id, max_tokens=4096, cache_system_prompt=True),
        tools=[read_file, glob_search, grep_search, list_directory, web_search, web_fetch,
               semantic_search, code_structure, find_dependencies, rank_files],
        instructions=instructions,
        markdown=True,
    )
