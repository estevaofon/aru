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
import uuid
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

    # -- Agent scope --
    # Stable identifier for the current agent's execution scope. None means
    # "primary agent" (the top-level conversation). Subagents forked via
    # fork_ctx() receive a unique identifier here, used to key per-scope
    # state such as active skills (so a subagent does not inherit the
    # parent's skill-active state).
    agent_id: str | None = None

    # -- Permissions --
    perm_config: Any = field(default_factory=_default_perm_config)
    session_allowed: set[tuple[str, str]] = field(default_factory=set)
    skip_permissions: bool = False
    permission_lock: threading.Lock = field(default_factory=threading.Lock)
    config_stack: list = field(default_factory=list)
    session_stack: list[set[tuple[str, str]]] = field(default_factory=list)
    # "default" = prompt for each edit, "acceptEdits" = auto-allow edit/write
    permission_mode: str = "default"
    last_rejection_feedback: str = ""

    # -- Tasklist --
    task_store: TaskStore = field(default_factory=TaskStore)

    # -- MCP --
    mcp_catalog_text: str = ""
    mcp_loaded_msg: str = ""

    # -- Plugins --
    plugin_manager: Any = None  # aru.plugins.manager.PluginManager (lazy to avoid circular)

    # -- Session --
    session: Any = None  # aru.session.Session (set by CLI, used for sub-agent cost tracking)

    # -- Cancellation --
    # Set by the REPL on Ctrl+C (or by any caller wanting to cancel running
    # sub-agents). `fork_ctx()` shares this by reference so a signal on the
    # primary propagates to every live sub-agent. Using `threading.Event`
    # (not `asyncio.Event`) because it is loop-independent — safe to read
    # from tool threads, worker threads, and the REPL's signal handler.
    # `check_aborted()` helper raises a standardised error when set.
    abort_event: threading.Event = field(default_factory=threading.Event)

    # -- Sub-agent instance cache (resume support) --
    # Keyed by the string task_id returned from a prior delegate_task call.
    # When the LLM passes `task_id="abc123"` on a subsequent invocation, the
    # Agno Agent instance is reused — preserving its full conversation
    # history without a second setup pass. Lives for the duration of the
    # primary session (in-memory only; disk persistence is feature #G).
    # Shared across forks so a sub-agent can spawn a nested sub-agent and
    # resume it later from the same primary — but that usage is rare.
    subagent_instances: dict[str, Any] = field(default_factory=dict)

    # -- Recursion depth --
    # Incremented by `fork_ctx()`. Primary ctx has depth=0; a sub-agent
    # spawned from primary has depth=1; a sub-agent spawned from THAT
    # sub-agent has depth=2; etc. Consulted at the start of `delegate_task`
    # to bound recursion (see `MAX_SUBAGENT_DEPTH`). Prevents a custom
    # agent with `tools: [..., delegate_task]` from triggering a runaway
    # chain through a bug in its own prompt.
    subagent_depth: int = 0

    # -- Checkpoints --
    checkpoint_manager: Any = None  # aru.checkpoints.CheckpointManager (lazy)

    # -- Config (for skill lookup from tools like invoke_skill) --
    config: Any = None  # aru.config.AgentConfig


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
    if ctx.skip_permissions and ctx.permission_mode != "yolo":
        ctx.permission_mode = "yolo"
    _runtime_ctx.set(ctx)
    return ctx


def fork_ctx() -> RuntimeContext:
    """Create an isolated copy of the current RuntimeContext for sub-agent use.

    Permission state is deep-copied to prevent interleaving when multiple
    sub-agents run concurrently via ``asyncio.gather``.  Shared resources
    (console, locks, tracked_processes, abort_event) are kept by reference.

    The fork receives a fresh, unique ``agent_id`` so per-scope state
    (e.g. active skills) keyed by agent_id is isolated from the parent.
    Callers may overwrite ``agent_id`` afterwards if they prefer a more
    descriptive label.

    `abort_event` is intentionally shared so a cancel signal on the primary
    propagates to every live sub-agent (they all observe the same
    `.is_set()` outcome).
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
    # Assign a unique agent_id so skill scope is isolated from the parent.
    # A uuid is used rather than an incrementing counter so nested forks
    # (fork-of-a-fork) still get distinct ids even though the counter on
    # the intermediate ctx was shallow-copied from the root.
    forked.agent_id = f"subagent-{uuid.uuid4().hex[:8]}"
    # Increment recursion depth. Shallow-copy captured the parent's depth
    # value; bumping it here means a fork-of-a-fork sees depth+2. Read by
    # `delegate_task` (MAX_SUBAGENT_DEPTH gate) as a safety net against a
    # custom agent with `tools: [..., delegate_task]` recursing unchecked.
    forked.subagent_depth = getattr(original, "subagent_depth", 0) + 1
    # abort_event is deliberately NOT reassigned — shared reference so the
    # primary can cancel forks it has spawned.
    return forked


def abort_current() -> None:
    """Signal every live sub-agent in the current ctx tree to cancel.

    Called from the REPL's SIGINT handler and from any caller wanting to
    abort in-flight delegation. Idempotent: repeated calls keep the event
    set. Use `reset_abort()` before the next turn to clear.
    """
    try:
        get_ctx().abort_event.set()
    except LookupError:
        pass  # no ctx installed — nothing to cancel


def reset_abort() -> None:
    """Clear the abort signal so the next turn starts fresh.

    Called by the REPL at the top of each iteration so an old Ctrl+C does
    not persist into a new prompt. Idempotent — safe to call even when
    the event was never set.
    """
    try:
        get_ctx().abort_event.clear()
    except LookupError:
        pass


def is_aborted() -> bool:
    """Return True if the current ctx's abort signal is set.

    Thin helper so call sites don't need to import `threading` or poke
    directly at `ctx.abort_event`. Safe to call from any thread.
    """
    try:
        return get_ctx().abort_event.is_set()
    except LookupError:
        return False
