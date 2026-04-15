"""Shared helpers used by multiple tool modules.

Split out of the former monolithic codebase.py. Imported by file_ops, search,
shell, web, and delegate. Intentionally has no dependencies on other tool
submodules so it sits at the bottom of the tool dependency graph.
"""

from __future__ import annotations

import asyncio
import functools

from aru.runtime import get_ctx
from aru.tools.gitignore import invalidate_walk_cache


_MAX_OUTPUT_CHARS = 10_000
_TRUNCATE_KEEP = 3_000  # chars to keep from start and end


def _notify_file_mutation():
    """Notify the session that files changed so caches are invalidated."""
    ctx = get_ctx()
    ctx.read_cache.clear()
    invalidate_walk_cache()
    if ctx.on_file_mutation:
        ctx.on_file_mutation()


def _checkpoint_file(file_path: str):
    """Capture pre-edit state of a file for undo support.

    Must be called BEFORE writing/editing the file.
    """
    ctx = get_ctx()
    if ctx.checkpoint_manager:
        ctx.checkpoint_manager.track_edit(file_path)


def _get_small_model_ref() -> str:
    """Get the small model reference for sub-agents."""
    return get_ctx().small_model_ref


def _truncate_output(text: str, source_file: str = "", source_tool: str = "") -> str:
    """Truncate long tool output to save tokens. Keeps start + end with a marker in the middle."""
    from aru.context import truncate_output
    return truncate_output(text, source_file=source_file, source_tool=source_tool)


def _thread_tool(sync_fn):
    """Wrap *sync_fn* as an async tool that runs on a worker thread.

    ``functools.wraps`` copies ``__name__``/``__doc__`` so Agno introspects
    the wrapper as if it were the original sync function — tool name and
    signature match what the LLM already knows.
    """

    @functools.wraps(sync_fn)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(sync_fn, *args, **kwargs)

    return wrapper
