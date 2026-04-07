"""Centralised runtime context for Aru.

Replaces scattered module-level globals with a single RuntimeContext
accessible via ``contextvars.ContextVar``.  This gives each asyncio task
(and each ``asyncio.to_thread`` call) its own isolated snapshot, which
means parallel agent runs and tests never share mutable state.

Usage::

    from aru.runtime import get_ctx, init_ctx

    # At startup (cli.py):
    ctx = init_ctx(console=console)

    # In any tool / helper:
    ctx = get_ctx()
    ctx.live  # Rich Live instance (or None)
"""

from __future__ import annotations

import contextvars
import copy
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from rich.console import Console


# ── TaskStore (moved from tools/tasklist.py) ─────────────────────────

class TaskStore:
    """Thread-safe store for the current step's subtask list."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: list[dict] = []
        self._created = False

    def create(self, tasks: list[str]) -> list[dict]:
        with self._lock:
            self._tasks = [
                {"index": i + 1, "description": desc, "status": "pending"}
                for i, desc in enumerate(tasks)
            ]
            self._created = True
            return list(self._tasks)

    def update(self, index: int, status: str) -> dict | None:
        with self._lock:
            for task in self._tasks:
                if task["index"] == index:
                    task["status"] = status
                    return dict(task)
            return None

    def get_all(self) -> list[dict]:
        with self._lock:
            return list(self._tasks)

    @property
    def is_created(self) -> bool:
        with self._lock:
            return self._created

    def reset(self) -> None:
        with self._lock:
            self._tasks = []
            self._created = False


# ── PermissionConfig (imported lazily to avoid circular deps) ────────

def _default_perm_config():
    from aru.permissions import PermissionConfig
    return PermissionConfig()


# ── RuntimeContext ───────────────────────────────────────────────────

@dataclass
class RuntimeContext:
    """All mutable runtime state, grouped by domain."""

    # -- Display --
    console: Console = field(default_factory=Console)
    live: Any = None
    display: Any = None

    # -- Model --
    model_id: str = "claude-sonnet-4-5-20250929"
    small_model_ref: str = "anthropic/claude-haiku-4-5"

    # -- File operations --
    on_file_mutation: Callable[[], None] | None = None
    read_cache: dict[tuple, str] = field(default_factory=dict)

    # -- Process tracking --
    tracked_processes: list = field(default_factory=list)
    subagent_counter: int = 0
    subagent_counter_lock: threading.Lock = field(default_factory=threading.Lock)

    # -- Custom agents --
    custom_agent_defs: dict = field(default_factory=dict)

    # -- Permissions --
    perm_config: Any = field(default_factory=_default_perm_config)
    session_allowed: set[tuple[str, str]] = field(default_factory=set)
    skip_permissions: bool = False
    permission_lock: threading.Lock = field(default_factory=threading.Lock)
    config_stack: list = field(default_factory=list)
    session_stack: list[set[tuple[str, str]]] = field(default_factory=list)

    # -- Tasklist --
    task_store: TaskStore = field(default_factory=TaskStore)

    # -- MCP --
    mcp_catalog_text: str = ""


# ── ContextVar plumbing ──────────────────────────────────────────────

_runtime_ctx: contextvars.ContextVar[RuntimeContext] = contextvars.ContextVar("aru_runtime")


def get_ctx() -> RuntimeContext:
    """Return the current RuntimeContext.  Raises LookupError if not initialised."""
    return _runtime_ctx.get()


def set_ctx(ctx: RuntimeContext) -> contextvars.Token[RuntimeContext]:
    """Set *ctx* as the current RuntimeContext; return a reset token."""
    return _runtime_ctx.set(ctx)


def init_ctx(console: Console | None = None, **kwargs: Any) -> RuntimeContext:
    """Create a new RuntimeContext, install it, and return it."""
    ctx = RuntimeContext(console=console or Console(), **kwargs)
    _runtime_ctx.set(ctx)
    return ctx


def fork_ctx() -> RuntimeContext:
    """Create an isolated copy of the current RuntimeContext for sub-agent use.

    Permission state is deep-copied to prevent interleaving when multiple
    sub-agents run concurrently via ``asyncio.gather``.  Shared resources
    (console, locks, tracked_processes) are kept by reference.
    """
    original = get_ctx()
    forked = copy.copy(original)
    # Deep-copy mutable permission state for isolation
    forked.config_stack = list(original.config_stack)
    forked.session_stack = [s.copy() for s in original.session_stack]
    forked.session_allowed = original.session_allowed.copy()
    # Fresh read cache per sub-agent
    forked.read_cache = {}
    # Fresh task store per sub-agent
    forked.task_store = TaskStore()
    return forked
