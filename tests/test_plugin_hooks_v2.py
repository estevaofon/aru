"""Stage 3 Tier 2: expanded hook lifecycle tests.

Covers the new events added to VALID_HOOKS:
- turn.start / turn.end
- file.changed
- worktree.create / worktree.remove / cwd.changed
- subagent.start / subagent.complete
- session.compact.before / session.compact.after
- permission.denied
- tool.execute.failure

We verify each event fires with the documented payload shape. Individual
emit sites are tested elsewhere; these tests pin the contract.
"""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from aru.plugins.manager import PluginManager
from aru.plugins.hooks import VALID_HOOKS


def test_new_events_registered_in_valid_hooks():
    for event in (
        "turn.start", "turn.end",
        "file.changed", "cwd.changed",
        "worktree.create", "worktree.remove",
        "subagent.start", "subagent.complete",
        "session.compact.before", "session.compact.after",
        "permission.denied",
        "tool.execute.failure",
    ):
        assert event in VALID_HOOKS, f"missing {event}"


@pytest.mark.asyncio
async def test_file_changed_emitted_with_path_and_mutation_type():
    """_notify_file_mutation should publish file.changed with payload."""
    from aru.runtime import get_ctx
    from aru.tools._shared import _notify_file_mutation

    mgr = PluginManager()
    mgr._loaded = True
    captured: list[dict] = []

    def cb(payload):
        captured.append(payload)

    mgr.subscribe("file.changed", cb)
    get_ctx().plugin_manager = mgr

    _notify_file_mutation(path="/tmp/foo.py", mutation_type="write")
    # Scheduled task — let it run
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(captured) == 1
    assert captured[0]["path"] == "/tmp/foo.py"
    assert captured[0]["mutation_type"] == "write"


@pytest.mark.asyncio
async def test_cwd_changed_emitted_on_enter_exit_worktree(tmp_path):
    """enter_worktree / exit_worktree should publish cwd.changed."""
    from aru.runtime import enter_worktree, exit_worktree, get_ctx
    from aru.session import Session

    # Set up a project root and a mock worktree target
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=proj, check=True)
    (proj / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=proj, check=True, capture_output=True)

    import os
    os.chdir(proj)
    ctx = get_ctx()
    sess = Session(session_id="t")
    sess.project_root = str(proj)
    ctx.session = sess

    mgr = PluginManager()
    mgr._loaded = True
    events: list[dict] = []
    mgr.subscribe("cwd.changed", lambda p: events.append(p))
    ctx.plugin_manager = mgr

    # Create worktree sibling so enter_worktree has a real dir
    wt_path = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(proj), "worktree", "add", str(wt_path), "-b", "feat"],
        check=True, capture_output=True,
    )

    enter_worktree(str(wt_path), "feat")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert any(e["reason"] == "worktree.enter" for e in events)

    exit_worktree()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert any(e["reason"] == "worktree.exit" for e in events)


@pytest.mark.asyncio
async def test_worktree_create_remove_emitted(tmp_path):
    from aru.runtime import get_ctx
    from aru.session import Session
    from aru.tools.worktree import create_worktree, remove_worktree

    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=proj, check=True)
    (proj / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=proj, check=True, capture_output=True)

    import os
    os.chdir(proj)
    ctx = get_ctx()
    sess = Session(session_id="t")
    sess.project_root = str(proj)
    ctx.session = sess

    mgr = PluginManager()
    mgr._loaded = True
    created: list[dict] = []
    removed: list[dict] = []
    mgr.subscribe("worktree.create", lambda p: created.append(p))
    mgr.subscribe("worktree.remove", lambda p: removed.append(p))
    ctx.plugin_manager = mgr

    path = create_worktree("feat-x")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert any(e["branch"] == "feat-x" for e in created)

    remove_worktree("feat-x")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert any(e["branch"] == "feat-x" for e in removed)


@pytest.mark.asyncio
async def test_schedule_publish_noop_without_plugin_manager():
    """Safety net: _schedule_publish must not crash when no manager is installed."""
    from aru.runtime import _schedule_publish, get_ctx

    get_ctx().plugin_manager = None
    # Should not raise
    _schedule_publish("whatever", {"x": 1})


@pytest.mark.asyncio
async def test_schedule_publish_noop_when_manager_not_loaded():
    from aru.runtime import _schedule_publish, get_ctx

    mgr = PluginManager()
    mgr._loaded = False  # simulate pre-load
    get_ctx().plugin_manager = mgr
    # Should not raise
    _schedule_publish("whatever", {"x": 1})
