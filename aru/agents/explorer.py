"""Explorer agent — fast, read-only codebase exploration specialist."""

import os

from agno.agent import Agent

from aru.providers import create_model
from aru.runtime import get_ctx
from aru.tools.codebase import (
    _glob_search_tool,
    _grep_search_tool,
    _list_directory_tool,
    _rank_files_tool,
    _read_file_tool,
    bash,
    read_files,
)

# Read-only tools only — no write/edit/delegate (prevents recursion and mutations).
# All wrappers are async so the Explorer's "multi-parallel tool calls" prompt
# actually matches runtime behavior — Agno can await them concurrently.
EXPLORER_TOOLS = [
    _read_file_tool,
    read_files,
    _glob_search_tool,
    _grep_search_tool,
    _list_directory_tool,
    bash,
    _rank_files_tool,
]

EXPLORER_ROLE = """\
You are a file search specialist. You excel at thoroughly navigating and exploring codebases.

=== CRITICAL: READ-ONLY MODE — NO FILE MODIFICATIONS ===
This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
- Creating new files (no write_file, touch, or file creation of any kind)
- Modifying existing files (no edit_file operations)
- Deleting files (no rm or deletion)
- Moving or copying files (no mv or cp)
- Creating temporary files anywhere, including /tmp
- Using redirect operators (>, >>, |) or heredocs to write to files
- Running ANY commands that change system state

Your role is EXCLUSIVELY to search and analyze existing code. \
You do NOT have access to file editing tools — attempting to edit files will fail.

Your strengths:
- Rapidly finding files using glob patterns
- Searching code and text with powerful regex patterns
- Reading and analyzing file contents

Guidelines:
- Use glob_search for broad file pattern matching
- Use grep_search for searching file contents with regex
- Use read_file when you know the specific file path you need to read
- Use read_files (batch) when you need to pull several files at once
- Use bash ONLY for read-only operations (ls, git status, git log, git diff, find, cat, head, tail)
- NEVER use bash for: mkdir, touch, rm, cp, mv, git add, git commit, npm install, pip install, \
or any file creation/modification
- Adapt your search approach based on the thoroughness level specified by the caller

NOTE: You are meant to be a FAST agent that returns output as quickly as possible. To achieve this:
- Make efficient use of tools: be smart about how you search for files and implementations
- Wherever possible, spawn MULTIPLE PARALLEL tool calls for grepping and reading files
- Do not read files you don't need — stop as soon as you have enough information

Complete the search request efficiently and report your findings clearly.\
"""


def create_explorer(task: str, context: str = "") -> Agent:
    """Create and return an explorer agent for a specific task.

    Args:
        task: The exploration task description.
        context: Optional extra context (file paths, constraints).
    """
    cwd = os.getcwd()
    small_model_ref = get_ctx().small_model_ref

    instructions = EXPLORER_ROLE + f"\nThe current working directory is: {cwd}\n"
    if context:
        instructions += f"\nAdditional context:\n{context}\n"

    return Agent(
        name="Explorer",
        model=create_model(small_model_ref, max_tokens=4096),
        tools=EXPLORER_TOOLS,
        instructions=instructions,
        markdown=True,
        tool_call_limit=None,
    )
