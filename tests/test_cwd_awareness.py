"""Stage 2 Tier 3: cwd-aware tools refactor.

Enter/exit worktree should NOT mutate the process cwd — the agent-facing
``ctx.cwd`` is the source of truth. Tools route relative paths through
``resolve_path`` so sibling sub-agents in different worktrees never fight
over ``os.getcwd()``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from aru.runtime import (
    enter_worktree,
    exit_worktree,
    fork_ctx,
    get_ctx,
    get_cwd,
    get_or_create_worktree_lock,
    resolve_path,
    set_ctx,
)
from aru.session import Session


@pytest.fixture
def git_project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=proj, check=True)
    (proj / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=proj, check=True, capture_output=True)

    original_cwd = os.getcwd()
    monkeypatch.chdir(proj)
    ctx = get_ctx()
    sess = Session(session_id="t")
    sess.project_root = str(proj)
    sess.cwd = str(proj)
    ctx.session = sess
    ctx.cwd = str(proj)  # align with new default

    yield proj

    if ctx.worktree_path:
        try:
            exit_worktree()
        except Exception:
            pass
    os.chdir(original_cwd)


def test_enter_worktree_does_not_chdir_process(git_project, tmp_path):
    """os.getcwd() stays pinned at project root; only ctx.cwd moves."""
    pre_process_cwd = os.getcwd()
    wt_path = tmp_path / "wt_a"
    subprocess.run(
        ["git", "-C", str(git_project), "worktree", "add", str(wt_path), "-b", "feat-a"],
        check=True, capture_output=True,
    )

    enter_worktree(str(wt_path), "feat-a")

    # Process cwd UNCHANGED — that's the whole point
    assert os.getcwd() == pre_process_cwd
    # ctx.cwd points at the worktree
    assert get_ctx().cwd == os.path.abspath(str(wt_path))


def test_exit_worktree_restores_ctx_cwd_to_project_root(git_project, tmp_path):
    wt_path = tmp_path / "wt_b"
    subprocess.run(
        ["git", "-C", str(git_project), "worktree", "add", str(wt_path), "-b", "feat-b"],
        check=True, capture_output=True,
    )
    enter_worktree(str(wt_path), "feat-b")
    exit_worktree()
    assert get_ctx().cwd == os.path.abspath(str(git_project))


def test_resolve_path_uses_ctx_cwd(git_project, tmp_path):
    wt_path = tmp_path / "wt_c"
    subprocess.run(
        ["git", "-C", str(git_project), "worktree", "add", str(wt_path), "-b", "feat-c"],
        check=True, capture_output=True,
    )
    enter_worktree(str(wt_path), "feat-c")

    resolved = resolve_path("sub/file.py")
    assert resolved.startswith(os.path.abspath(str(wt_path)))
    # Absolute paths pass through
    abs_in = "/absolute/else"
    assert resolve_path(abs_in) == abs_in


def test_fork_ctx_inherits_cwd_but_isolates_changes(git_project, tmp_path):
    enter_worktree(str(git_project), None)  # set ctx.cwd to project root
    parent = get_ctx()
    assert parent.cwd == os.path.abspath(str(git_project))

    forked = fork_ctx()
    assert forked.cwd == parent.cwd

    # Mutating fork's cwd doesn't leak to parent
    forked.cwd = str(tmp_path / "somewhere_else")
    assert parent.cwd == os.path.abspath(str(git_project))


def test_two_sub_agents_have_isolated_cwd_via_fork(git_project, tmp_path):
    """Simulated parallel sub-agents with different worktrees — no collision."""
    a_path = tmp_path / "wt_gather_a"
    b_path = tmp_path / "wt_gather_b"
    subprocess.run(
        ["git", "-C", str(git_project), "worktree", "add", str(a_path), "-b", "gather-a"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_project), "worktree", "add", str(b_path), "-b", "gather-b"],
        check=True, capture_output=True,
    )

    fork_a = fork_ctx()
    fork_a.cwd = str(a_path)

    fork_b = fork_ctx()
    fork_b.cwd = str(b_path)

    assert fork_a.cwd != fork_b.cwd
    # Process cwd also unchanged
    assert os.getcwd() == str(git_project)


def test_get_cwd_fallback_without_ctx(monkeypatch):
    """get_cwd must never raise even if no ctx is installed."""
    import aru.runtime as rt
    token = rt._runtime_ctx.set(rt.RuntimeContext(cwd="/tmp/xyz"))
    try:
        assert get_cwd() == "/tmp/xyz"
    finally:
        rt._runtime_ctx.reset(token)


@pytest.mark.asyncio
async def test_worktree_create_lock_serializes_concurrent_requests(git_project):
    """Two delegates racing on the same branch must serialise on the lock."""
    lock_a1 = get_or_create_worktree_lock("concurrent-branch")
    lock_a2 = get_or_create_worktree_lock("concurrent-branch")
    # Same lock instance — that's how we serialise
    assert lock_a1 is lock_a2
    # Different branches get different locks
    lock_b = get_or_create_worktree_lock("other-branch")
    assert lock_b is not lock_a1


def test_session_persists_worktree_state(git_project, tmp_path):
    """to_dict/from_dict roundtrip preserves worktree_path/branch."""
    wt_path = tmp_path / "wt_persist"
    subprocess.run(
        ["git", "-C", str(git_project), "worktree", "add", str(wt_path), "-b", "feat-persist"],
        check=True, capture_output=True,
    )
    enter_worktree(str(wt_path), "feat-persist")

    sess = get_ctx().session
    data = sess.to_dict()
    assert data["worktree_path"] == os.path.abspath(str(wt_path))
    assert data["worktree_branch"] == "feat-persist"
    assert data["project_root"] == str(git_project)

    restored = Session.from_dict(data)
    assert restored.worktree_path == os.path.abspath(str(wt_path))
    assert restored.worktree_branch == "feat-persist"
    assert restored.project_root == str(git_project)


def test_write_file_resolves_via_ctx_cwd(git_project, tmp_path):
    """write_file with a relative path should land in ctx.cwd."""
    wt_path = tmp_path / "wt_write"
    subprocess.run(
        ["git", "-C", str(git_project), "worktree", "add", str(wt_path), "-b", "feat-write"],
        check=True, capture_output=True,
    )
    enter_worktree(str(wt_path), "feat-write")
    get_ctx().skip_permissions = True

    from aru.tools.file_ops import write_file
    result = write_file("generated.py", "x = 1\n")
    assert "Successfully" in result
    # File landed in the worktree, NOT in the process cwd (which is still project root)
    assert (wt_path / "generated.py").exists()
    assert not (Path(git_project) / "generated.py").exists()


def test_process_cwd_never_changes_during_worktree_ops(git_project, tmp_path):
    """Hard invariant: process cwd should be equal to project_root throughout."""
    proc_cwd_before = os.getcwd()
    wt = tmp_path / "wt_invariant"
    subprocess.run(
        ["git", "-C", str(git_project), "worktree", "add", str(wt), "-b", "feat-inv"],
        check=True, capture_output=True,
    )
    enter_worktree(str(wt), "feat-inv")
    assert os.getcwd() == proc_cwd_before
    exit_worktree()
    assert os.getcwd() == proc_cwd_before
