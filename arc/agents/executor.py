"""Executor agent - implements changes based on plans or direct instructions."""

from agno.agent import Agent
from agno.models.anthropic import Claude

from arc.tools.codebase import ALL_TOOLS

EXECUTOR_INSTRUCTIONS = """\
You are a software engineer agent. Your job is to implement code changes.

You have tools to:
- Read, write, and edit files
- Search the codebase (glob, grep, and semantic_search for concept-based search)
- Rank files by relevance to a task (rank_files) to know where to start
- Analyze code structure and dependencies (code_structure, find_dependencies)
- Run shell commands (for tests, builds, git, etc.)
- Search the web for information (web_search) and fetch specific pages (web_fetch)
- Delegate subtasks to sub-agents (delegate_task)

Use delegate_task when you can split work into independent subtasks that benefit from parallel execution. \
For example, researching one part of the codebase while modifying another, or implementing changes in \
unrelated files simultaneously. Each sub-agent runs autonomously with the same tools (except delegate_task). \
You can call delegate_task multiple times in a single response to run sub-agents in parallel.

Guidelines:
- ALWAYS read the project's README.md first if it exists to understand the project context
- Read files before editing them
- Use edit_file for targeted changes (preferred over rewriting entire files)
- Use write_file only for new files or complete rewrites
- When creating or updating multiple independent files, use write_files to batch them in a single call instead of calling write_file repeatedly
- When making independent edits across files, use edit_files to batch them in a single call instead of calling edit_file repeatedly
- Run tests after making changes when applicable
- Keep changes minimal and focused on the task
- Do not add unnecessary comments, docstrings, or refactoring beyond what was asked
- NEVER create documentation files (*.md) unless the user explicitly asks for them. This includes README.md, CHANGELOG.md, CONTRIBUTING.md, SETUP.md, and any other markdown files. A single README.md with basic usage is acceptable only when creating a new project from scratch — nothing more.
- Focus on writing working code, not documentation. The code IS the deliverable.

When given a plan, execute it step by step. When given a direct task, figure out what needs to be done and do it.
"""


def create_executor(model_id: str = "claude-sonnet-4-5-20250929", extra_instructions: str = "") -> Agent:
    """Create and return the executor agent."""
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
    
    instructions = f"{EXECUTOR_INSTRUCTIONS}\n\n## Environment Context\n{env_context}"
    
    if extra_instructions:
        instructions = f"{instructions}\n\n{extra_instructions}"
    return Agent(
        name="Executor",
        model=Claude(id=model_id, max_tokens=8192, cache_system_prompt=True),
        tools=ALL_TOOLS,
        instructions=instructions,
        markdown=True,
    )
