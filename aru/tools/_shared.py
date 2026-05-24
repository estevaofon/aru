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

# How often (seconds) the timeout loop re-checks the permission gate while a
# human decision is in flight. Small enough that the post-decision write is
# noticed promptly; large enough not to busy-spin while the user is thinking.
_PERM_POLL_SLICE = 0.1


def _notify_file_mutation(*, path: str | None = None, mutation_type: str = "unknown"):
    """Notify the session that files changed so caches are invalidated.

    Also publishes ``file.changed`` via the plugin bus so plugins (auto-
    linter, memory extractor, LSP didChange etc.) can react. ``path`` and
    ``mutation_type`` are optional and default to "unknown" for legacy
    callers that haven't been updated yet.
    """
    ctx = get_ctx()
    ctx.read_cache.clear()
    invalidate_walk_cache()
    if ctx.on_file_mutation:
        ctx.on_file_mutation()
    from aru.runtime import _schedule_publish
    _schedule_publish("file.changed", {
        "path": path, "mutation_type": mutation_type,
    })


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

    Permission-wait suspension (safety-critical): if the wrapped tool calls
    ``check_permission`` and blocks on a human decision, the timeout is
    suspended for the duration of that prompt. Without this, the timeout
    could fire mid-prompt, report a timeout to the model, and leave the
    worker thread alive to apply the mutation out-of-band once the user
    finally answered. See ``aru.runtime.PermissionWaitGate``.
    """

    @functools.wraps(sync_fn)
    async def wrapper(*args, **kwargs):
        if timeout is None:
            return await asyncio.to_thread(sync_fn, *args, **kwargs)

        from aru.runtime import (
            install_permission_wait_gate,
            reset_permission_wait_gate,
        )

        # Install the per-call gate BEFORE offloading so the worker thread
        # (which runs in a copy of this context) shares the same gate object
        # and ``check_permission`` can flip it while it blocks on the user.
        gate, token = install_permission_wait_gate()
        task: asyncio.Future | None = None
        try:
            loop = asyncio.get_running_loop()
            task = asyncio.ensure_future(asyncio.to_thread(sync_fn, *args, **kwargs))
            deadline = loop.time() + timeout
            while True:
                if task.done():
                    return task.result()
                now = loop.time()
                if gate.active:
                    # A human is being asked to approve this tool call. Their
                    # decision time is not the tool's execution budget — and
                    # abandoning the worker thread now would let it apply the
                    # mutation the instant the user answers, after we already
                    # reported a timeout. Hold the deadline a full window
                    # ahead so that, once the prompt closes, the actual work
                    # still gets the complete budget (this closes the
                    # answer→write race: when ``gate.active`` flips false the
                    # deadline is at most one poll-slice old).
                    deadline = now + timeout
                    await asyncio.wait({task}, timeout=_PERM_POLL_SLICE)
                    continue
                remaining = deadline - now
                if remaining <= 0:
                    # Genuine timeout — no human in the loop. Request
                    # cancellation (best-effort; the OS thread may run on,
                    # same as the historical behaviour) and surface a string.
                    task.cancel()
                    return (
                        f"[Tool timeout: {sync_fn.__name__} exceeded {timeout:g}s. "
                        f"The worker thread may still be running in the background; "
                        f"narrow the query or raise the timeout explicitly.]"
                    )
                await asyncio.wait({task}, timeout=remaining)
        finally:
            if task is not None and not task.done():
                task.cancel()
            reset_permission_wait_gate(token)

    return wrapper
