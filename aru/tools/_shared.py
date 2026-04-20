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


def _thread_tool(sync_fn, *, timeout: float | None = None):
    """Wrap *sync_fn* as an async tool that runs on a worker thread.

    ``functools.wraps`` copies ``__name__``/``__doc__`` so Agno introspects
    the wrapper as if it were the original sync function — tool name and
    signature match what the LLM already knows.

    Args:
        sync_fn: The synchronous implementation to offload to a worker.
        timeout: Optional wall-clock cap (seconds). ``None`` (default) keeps
            the historical behaviour of unbounded wait — callers opt into a
            cap explicitly. Required because ``asyncio.to_thread`` cannot
            actually abort the underlying worker thread (Python limitation):
            on timeout, the REPL regains control but the thread may keep
            running until its sync work finishes. Applying a blanket
            default would break custom plugin tools that legitimately take
            longer than the cap.
    """

    @functools.wraps(sync_fn)
    async def wrapper(*args, **kwargs):
        coro = asyncio.to_thread(sync_fn, *args, **kwargs)
        if timeout is None:
            return await coro
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            return (
                f"[Tool timeout: {sync_fn.__name__} exceeded {timeout:g}s. "
                f"The worker thread may still be running in the background; "
                f"narrow the query or raise the timeout explicitly.]"
            )

    return wrapper
