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
import os
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
    # `list.append` is atomic via the GIL, so single writes to `tracked_processes`
    # do NOT require the lock. The lock is acquired around **iteration/snapshot**
    # (see `snapshot_tracked_processes`) to avoid ``list changed size during
    # iteration`` when cleanup runs concurrently with new shell invocations.
    tracked_processes: list = field(default_factory=list)
    tracked_processes_lock: threading.Lock = field(default_factory=threading.Lock)
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

    # -- Worktree (Tier 2 #1) --
    # When the REPL (or a sub-agent) is operating inside a git worktree via
    # ``/worktree enter`` or ``delegate_task(worktree=...)``, these hold the
    # absolute path and branch name. ``None`` means the session is at its
    # original project root.
    worktree_path: str | None = None
    worktree_branch: str | None = None

    # -- Per-scope working directory (Tier 3 #2) --
    # All file-touching tools resolve relative paths against this field
    # instead of ``os.getcwd()``. ``None`` means "follow the process cwd"
    # (``os.getcwd()``) — the default so the REPL tracks ``monkeypatch.chdir``
    # in tests and legacy code that expects ``os.getcwd`` semantics.
    # ``enter_worktree`` / ``delegate_task(worktree=...)`` SET this to an
    # absolute path which detaches ctx.cwd from the process cwd. Inherited
    # (by value — immutable str) through ``fork_ctx`` so concurrent sub-agents
    # can live in different worktrees without touching the process-global cwd.
    cwd: str | None = None

    # -- Concurrent worktree-create serialization (Tier 3 #2) --
    # Keyed by branch name: dict of asyncio.Lock. Acquired by
    # ``delegate_task(worktree=...)`` before calling ``create_worktree`` so
    # two parallel delegates asking for the same branch serialise and the
    # second one reuses the existing worktree rather than racing the
    # non-atomic ``_find_worktree_by_branch + git worktree add`` pair.
    # Populated lazily; lives for the session.
    worktree_create_locks: dict[str, Any] = field(default_factory=dict)
    worktree_create_locks_mutex: threading.Lock = field(default_factory=threading.Lock)

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
    #
    # Individual ``dict[k] = v`` / ``dict.get(k)`` operations are atomic via
    # the GIL; the lock protects **iteration/snapshot** (e.g. cleanup that
    # walks ``.items()``) against concurrent writes from sibling sub-agents.
    # Shared by reference across forks so nested delegations see the same map.
    subagent_instances: dict[str, Any] = field(default_factory=dict)
    subagent_instances_lock: threading.Lock = field(default_factory=threading.Lock)

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

    Sharing model — what is isolated vs shared between parent and fork:

    | Field                         | After fork    | Rationale                              |
    |-------------------------------|---------------|----------------------------------------|
    | ``config_stack``              | **copy**      | permission scopes must not leak sideways |
    | ``session_stack``             | **copy**      | idem                                   |
    | ``session_allowed``           | **copy**      | idem                                   |
    | ``read_cache``                | **fresh**     | sub-agent exploration stays out of parent's cache |
    | ``task_store``                | **fresh**     | sub-agent's subtasks are its own       |
    | ``agent_id``                  | **fresh uuid**| used to key per-scope state (skills etc) |
    | ``subagent_depth``            | **parent+1**  | bounds recursion                       |
    | ``subagent_instances``        | shared ref    | resume lookup across the tree          |
    | ``subagent_instances_lock``   | shared ref    | must match the dict it guards          |
    | ``tracked_processes``         | shared ref    | cleanup() walks the single global list |
    | ``tracked_processes_lock``    | shared ref    | guards iteration of that list          |
    | ``abort_event``               | shared ref    | ``ctx.abort_event.set()`` propagates   |
    | ``custom_agent_defs``         | shared ref    | reassigned once at startup; read-only  |
    | ``console`` / ``display``     | shared ref    | Rich handles its own synchronization   |
    | ``subagent_counter_lock``     | shared ref    | already protected by its own lock      |

    **Atomicity note.** In CPython, a single ``list.append`` or ``dict[k] = v``
    is atomic thanks to the GIL, so individual writes don't need the locks.
    The locks (``subagent_instances_lock``, ``tracked_processes_lock``) exist
    for **iteration/snapshot** — e.g. cleanup that walks ``.items()`` or
    ``for p in tracked_processes`` concurrently with a sibling sub-agent's
    write. Without them we risk
    ``RuntimeError: dictionary changed size during iteration``.

    ``custom_agent_defs`` is reassigned once in delegate.set_custom_agents()
    during startup and read-only afterwards, so no lock is needed.
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


# ── Shared-state helpers (Stage 4) ───────────────────────────────────
#
# Individual ``dict[k] = v``, ``dict.get(k)``, and ``list.append`` are atomic
# via the GIL, so the lock is acquired only where callers need a consistent
# view (snapshot for iteration, compound pop-and-return). These helpers also
# let delegate.py / shell.py avoid importing ``threading`` directly.


def register_subagent_instance(cache: dict[str, Any] | None, task_id: str, agent: Any) -> None:
    """Register a sub-agent under ``task_id`` for later resume.

    ``cache`` may be ``ctx.subagent_instances`` or a session-scoped dict
    (``session._subagent_instances``) — either path uses the ctx lock.
    """
    if cache is None:
        return
    try:
        lock = get_ctx().subagent_instances_lock
    except LookupError:
        cache[task_id] = agent
        return
    with lock:
        cache[task_id] = agent


def get_subagent_instance(cache: dict[str, Any] | None, task_id: str) -> Any | None:
    """Look up a previously-registered sub-agent. Returns ``None`` if absent."""
    if cache is None or not task_id:
        return None
    try:
        lock = get_ctx().subagent_instances_lock
    except LookupError:
        return cache.get(task_id)
    with lock:
        return cache.get(task_id)


def snapshot_subagent_instances(cache: dict[str, Any] | None) -> dict[str, Any]:
    """Return an immutable copy for iteration without racing concurrent writes."""
    if cache is None:
        return {}
    try:
        lock = get_ctx().subagent_instances_lock
    except LookupError:
        return dict(cache)
    with lock:
        return dict(cache)


def append_tracked_process(process: Any) -> None:
    """Register a subprocess for atexit / Ctrl+C cleanup.

    ``list.append`` is atomic via the GIL, so this does not itself need the
    lock — but we hold it for the brief scope anyway to keep all mutations
    to ``tracked_processes`` serialised against readers.
    """
    try:
        ctx = get_ctx()
    except LookupError:
        return
    with ctx.tracked_processes_lock:
        ctx.tracked_processes.append(process)


def snapshot_tracked_processes() -> list[Any]:
    """Immutable copy of the tracked-processes list for safe iteration."""
    try:
        ctx = get_ctx()
    except LookupError:
        return []
    with ctx.tracked_processes_lock:
        return list(ctx.tracked_processes)


# ── Worktree helpers (Tier 2 Stage 1) ────────────────────────────────
#
# Centralised chdir + state mutation. The tool invocations, slash command
# handler, and tests all funnel through these two functions so the REPL's
# conception of "active worktree" stays in sync with the OS cwd.


def _schedule_publish(event_type: str, data: dict[str, Any]) -> None:
    """Fire-and-forget ``plugin_manager.publish`` without requiring a running loop.

    Helpers like ``enter_worktree`` are called from sync paths (slash
    commands) where we don't want to block. If a loop is running, schedule
    the coroutine; if not, drop silently (tests without an event loop).
    """
    try:
        ctx = get_ctx()
        mgr = ctx.plugin_manager
        if mgr is None or not getattr(mgr, "loaded", False):
            return
    except LookupError:
        return

    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(mgr.publish(event_type, data))


def resolve_path(path: str) -> str:
    """Resolve *path* against the ctx's worktree-aware cwd.

    Absolute paths are returned unchanged. Relative paths are joined onto
    ``get_cwd()`` — which is either ``ctx.cwd`` (set by enter_worktree /
    delegate worktree) or the process cwd as fallback.
    """
    if os.path.isabs(path):
        return path
    return os.path.join(get_cwd(), path)


def get_cwd() -> str:
    """Return the active ctx's cwd, or the process cwd as fallback.

    When ``ctx.cwd`` is ``None`` (the default) we track ``os.getcwd()`` so
    tests that rely on ``monkeypatch.chdir`` and legacy code that assumes
    process-cwd semantics keep working. Only ``enter_worktree`` and
    ``delegate_task(worktree=...)`` SET a concrete ``ctx.cwd``, which
    detaches the agent scope from the process cwd.
    """
    try:
        value = get_ctx().cwd
    except LookupError:
        return os.getcwd()
    return value if value is not None else os.getcwd()


def enter_worktree(path: str, branch: str | None = None) -> None:
    """Make *path* the active worktree for the current session.

    Sets ``ctx.cwd``, ``ctx.worktree_path``, and ``ctx.worktree_branch``.
    Does NOT call ``os.chdir`` — process cwd stays pinned at the session
    project_root so concurrent sub-agents can each have their own ctx.cwd
    without fighting the process-global state.

    Invalidates read_cache and the gitignore walk cache so cwd-keyed
    entries from the previous directory don't leak. Emits ``cwd.changed``.
    """
    from aru.tools.gitignore import invalidate_walk_cache

    ctx = get_ctx()
    abs_path = os.path.abspath(path)
    old_cwd = ctx.cwd
    ctx.worktree_path = abs_path
    ctx.worktree_branch = branch
    ctx.cwd = abs_path
    ctx.read_cache.clear()
    invalidate_walk_cache()
    if ctx.session is not None:
        ctx.session.cwd = abs_path
        # Tier 3 #2 R11: persist worktree state so `aru --resume` can
        # restore the same worktree on startup.
        if hasattr(ctx.session, "worktree_path"):
            ctx.session.worktree_path = abs_path
            ctx.session.worktree_branch = branch
    _schedule_publish("cwd.changed", {
        "old_cwd": old_cwd, "new_cwd": abs_path,
        "reason": "worktree.enter", "branch": branch,
    })


def exit_worktree() -> bool:
    """Return the REPL to the session's project_root.

    Returns True if a worktree was active (and we exited), False if the
    session was already at its project root. Does NOT call ``os.chdir`` —
    process cwd never changed when we entered, so there's nothing to undo
    at the OS level. Emits ``cwd.changed``.
    """
    from aru.tools.gitignore import invalidate_walk_cache

    ctx = get_ctx()
    if ctx.worktree_path is None:
        return False
    target = getattr(ctx.session, "project_root", None) or os.getcwd()
    old_cwd = ctx.cwd
    old_branch = ctx.worktree_branch
    ctx.worktree_path = None
    ctx.worktree_branch = None
    ctx.cwd = target
    ctx.read_cache.clear()
    invalidate_walk_cache()
    if ctx.session is not None:
        ctx.session.cwd = target
        if hasattr(ctx.session, "worktree_path"):
            ctx.session.worktree_path = None
            ctx.session.worktree_branch = None
    _schedule_publish("cwd.changed", {
        "old_cwd": old_cwd, "new_cwd": target,
        "reason": "worktree.exit", "branch": old_branch,
    })
    return True


def get_or_create_worktree_lock(branch: str):
    """Return the async lock that serialises ``create_worktree`` for *branch*.

    Populated lazily and shared across forks so two ``delegate_task`` calls
    racing on the same branch end up reusing the worktree rather than each
    one triggering ``git worktree add`` (which is NOT atomic with
    ``_find_worktree_by_branch``).
    """
    import asyncio as _asyncio
    ctx = get_ctx()
    with ctx.worktree_create_locks_mutex:
        lock = ctx.worktree_create_locks.get(branch)
        if lock is None:
            lock = _asyncio.Lock()
            ctx.worktree_create_locks[branch] = lock
        return lock
