"""Stage 4 regression: fork_ctx() shared-state thread safety.

Covers:
- Concurrent registrations via the helper produce no lost / overwritten
  entries (GIL already guarantees atomicity of individual ops; the test
  documents and pins the contract).
- snapshot_subagent_instances returns a frozen view that does not raise
  ``RuntimeError: dictionary changed size during iteration`` when a
  sibling writer mutates the underlying dict mid-walk.
- snapshot_tracked_processes gives the same guarantee for cleanup.
- fork_ctx still isolates per-fork state (config_stack, read_cache, etc.)
  while sharing subagent_instances / locks.
- custom_agent_defs is shared by reference (read-only in runtime).
"""

import asyncio
import threading
import time

import pytest

from aru.runtime import (
    RuntimeContext,
    append_tracked_process,
    fork_ctx,
    get_ctx,
    get_subagent_instance,
    init_ctx,
    register_subagent_instance,
    set_ctx,
    snapshot_subagent_instances,
    snapshot_tracked_processes,
)


@pytest.mark.asyncio
async def test_concurrent_registrations_no_loss():
    cache: dict = {}

    async def register_one(i: int):
        register_subagent_instance(cache, f"task-{i}", object())

    await asyncio.gather(*(register_one(i) for i in range(100)))
    assert len(cache) == 100


def test_snapshot_survives_concurrent_writer():
    """snapshot_subagent_instances must never raise a 'changed size' error.

    Reproduces the regression: writer thread mutates the dict in a tight
    loop while the main thread iterates the snapshot.
    """
    ctx = get_ctx()
    cache = ctx.subagent_instances
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            register_subagent_instance(cache, f"t-{i}", object())
            i += 1

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(200):
            snap = snapshot_subagent_instances(cache)
            # Walking the snapshot must never raise.
            for _k, _v in snap.items():
                pass
    finally:
        stop.set()
        t.join(timeout=2)


def test_tracked_processes_snapshot_survives_concurrent_append():
    stop = threading.Event()

    class _FakeProc:
        returncode = 0
        def poll(self):
            return 0

    def writer():
        while not stop.is_set():
            append_tracked_process(_FakeProc())

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(200):
            snap = snapshot_tracked_processes()
            for _p in snap:
                pass
    finally:
        stop.set()
        t.join(timeout=2)


def test_register_returns_same_object_on_lookup():
    cache: dict = {}
    agent = object()
    register_subagent_instance(cache, "abc", agent)
    assert get_subagent_instance(cache, "abc") is agent


def test_get_subagent_instance_missing_returns_none():
    cache: dict = {}
    assert get_subagent_instance(cache, "absent") is None
    assert get_subagent_instance(None, "whatever") is None
    assert get_subagent_instance(cache, "") is None


def test_fork_ctx_isolates_per_fork_mutables():
    original = get_ctx()
    # Prime state
    original.config_stack = ["outer"]
    original.read_cache[("k",)] = "v"

    forked = fork_ctx()
    forked.config_stack.append("inner")
    forked.read_cache[("other",)] = "fork"

    # Parent state unchanged
    assert original.config_stack == ["outer"]
    assert original.read_cache == {("k",): "v"}
    # Fork has its own state
    assert forked.config_stack == ["outer", "inner"]
    assert forked.read_cache == {("other",): "fork"}


def test_fork_ctx_shares_subagent_instances_and_lock():
    """Both fork and parent must see the same cache + the same lock instance."""
    original = get_ctx()
    forked = fork_ctx()

    assert forked.subagent_instances is original.subagent_instances
    assert forked.subagent_instances_lock is original.subagent_instances_lock
    assert forked.tracked_processes is original.tracked_processes
    assert forked.tracked_processes_lock is original.tracked_processes_lock
    assert forked.abort_event is original.abort_event


def test_fork_ctx_increments_depth_and_fresh_agent_id():
    original = get_ctx()
    original.subagent_depth = 2
    original.agent_id = "parent"

    forked = fork_ctx()
    assert forked.subagent_depth == 3
    assert forked.agent_id != "parent"
    assert forked.agent_id.startswith("subagent-")


def test_fork_ctx_fresh_read_cache_and_task_store():
    original = get_ctx()
    original.read_cache[("a",)] = "b"
    original.task_store.create(["t1", "t2"])

    forked = fork_ctx()
    assert forked.read_cache == {}
    assert forked.task_store.get_all() == []
    # Parent still has its own
    assert original.task_store.get_all() != []


def test_custom_agent_defs_is_shared_reference():
    """Documented in fork_ctx() docstring — read-only post-setup, shared ref."""
    original = get_ctx()
    original.custom_agent_defs = {"a": object()}

    forked = fork_ctx()
    assert forked.custom_agent_defs is original.custom_agent_defs
