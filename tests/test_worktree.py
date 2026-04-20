"""Stage 1 Tier 2: git worktree primitive tests.

Covers:
- create_worktree creates sibling dir and new branch
- list_worktrees parses git porcelain output
- enter_worktree / exit_worktree chdir + ctx state round-trip
- removing the currently-active worktree auto-exits before deletion
- worktree_info reports branch + path
- /worktree create is idempotent when the branch already has a worktree
"""

import os
import subprocess
from pathlib import Path

import pytest

from aru.runtime import (
    enter_worktree,
    exit_worktree,
    get_ctx,
)
from aru.session import Session
from aru.tools.worktree import (
    WorktreeError,
    create_worktree,
    list_worktrees,
    remove_worktree,
    worktree_info,
)


@pytest.fixture
def git_project(tmp_path, monkeypatch):
    """Create a bare-bones git repo with one commit and attach it to ctx.session."""
    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    (project / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=project, check=True, capture_output=True,
    )

    # Hand the ctx a session rooted at the project, so _project_root() resolves.
    ctx = get_ctx()
    original_cwd = os.getcwd()
    session = Session(session_id="test")
    session.project_root = str(project)
    session.cwd = str(project)
    ctx.session = session

    monkeypatch.chdir(project)
    yield project

    # Ensure we're not stuck inside a worktree that will be deleted
    if ctx.worktree_path:
        try:
            exit_worktree()
        except Exception:
            pass
    os.chdir(original_cwd)


def test_list_worktrees_includes_main(git_project):
    entries = list_worktrees()
    assert len(entries) == 1
    assert entries[0].is_main is True
    assert entries[0].branch == "main"


def test_create_worktree_creates_dir_and_branch(git_project):
    path = create_worktree("feat-x")
    assert Path(path).exists()
    entries = list_worktrees()
    branches = {e.branch for e in entries}
    assert "feat-x" in branches
    assert "main" in branches


def test_create_worktree_is_idempotent_on_existing_branch(git_project):
    first = create_worktree("feat-dup")
    second = create_worktree("feat-dup")
    assert os.path.abspath(first) == os.path.abspath(second)
    # Still just one worktree for feat-dup
    feat_entries = [e for e in list_worktrees() if e.branch == "feat-dup"]
    assert len(feat_entries) == 1


def test_enter_and_exit_worktree_roundtrip(git_project):
    """Tier 3 #2: enter/exit updates ctx.cwd, NOT process cwd."""
    process_cwd_before = os.getcwd()
    path = create_worktree("feat-enter")
    enter_worktree(path, "feat-enter")
    ctx = get_ctx()
    assert ctx.worktree_path == os.path.abspath(path)
    assert ctx.worktree_branch == "feat-enter"
    # ctx.cwd points into the worktree (Tier 3 #2)
    assert os.path.abspath(ctx.cwd) == os.path.abspath(path)
    # Process cwd is UNCHANGED — that's the whole point of the cwd-aware refactor
    assert os.getcwd() == process_cwd_before
    # session.cwd mirrors ctx.cwd
    assert os.path.abspath(ctx.session.cwd) == os.path.abspath(path)

    left = exit_worktree()
    assert left is True
    assert ctx.worktree_path is None
    assert ctx.worktree_branch is None
    # ctx.cwd back at project root
    assert os.path.abspath(ctx.cwd) == os.path.abspath(ctx.session.project_root)
    # Process cwd still unchanged
    assert os.getcwd() == process_cwd_before


def test_exit_worktree_noop_when_not_inside(git_project):
    assert exit_worktree() is False


def test_worktree_info_reports_active_branch(git_project):
    path = create_worktree("feat-info")
    enter_worktree(path, "feat-info")
    info = worktree_info()
    assert "feat-info" in info
    assert os.path.abspath(path) in info


def test_worktree_info_reports_main_when_not_inside(git_project):
    info = worktree_info()
    assert "main" in info.lower() or "project root" in info.lower()


def test_remove_worktree_auto_exits_when_active(git_project):
    path = create_worktree("feat-rm")
    enter_worktree(path, "feat-rm")

    removed = remove_worktree("feat-rm")
    assert os.path.abspath(removed) == os.path.abspath(path)
    ctx = get_ctx()
    # We were inside; remove_worktree should have exited us first
    assert ctx.worktree_path is None
    # Worktree dir is gone
    assert not Path(path).exists()


def test_remove_worktree_refuses_main(git_project):
    with pytest.raises(WorktreeError, match="main worktree"):
        remove_worktree("main")


def test_remove_unknown_branch_raises(git_project):
    with pytest.raises(WorktreeError, match="No worktree"):
        remove_worktree("definitely-not-a-branch")
