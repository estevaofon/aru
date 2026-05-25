"""Tier 3 #2 R11: session resume path that re-enters a saved worktree.

Previously, only serialization was covered (to_dict/from_dict roundtrip in
``test_cwd_awareness.py::test_session_persists_worktree_state``). The actual
re-entry branch at ``run_tui`` startup had no test.

This module extracts the re-entry logic into the public
``_restore_worktree_from_session`` helper and exercises all four outcomes:
``none`` / ``entered`` / ``stale`` / ``error``.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from aru.cli import _restore_worktree_from_session
from aru.runtime import get_ctx
from aru.session import Session


@pytest.fixture
def git_project(tmp_path, monkeypatch):
    """Bare-bones repo with one commit, wired through the runtime ctx."""
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=proj, check=True)
    (proj / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=proj, check=True, capture_output=True)

    original_cwd = os.getcwd()
    monkeypatch.chdir(proj)
    ctx = get_ctx()
    sess = Session(session_id="t")
    sess.project_root = str(proj)
    ctx.session = sess

    yield proj, sess

    # Cleanup any lingering worktree state
    ctx.worktree_path = None
    ctx.worktree_branch = None
    os.chdir(original_cwd)


def test_restore_returns_none_when_no_worktree_saved(git_project):
    _proj, sess = git_project
    sess.worktree_path = None
    sess.worktree_branch = None
    assert _restore_worktree_from_session(sess) == "none"
    ctx = get_ctx()
    assert ctx.worktree_path is None


def test_restore_entered_branch_when_worktree_exists(git_project, tmp_path):
    proj, sess = git_project
    wt = tmp_path / "wt_resume"
    subprocess.run(
        ["git", "-C", str(proj), "worktree", "add", str(wt), "-b", "feat-resume"],
        check=True, capture_output=True,
    )
    sess.worktree_path = str(wt)
    sess.worktree_branch = "feat-resume"

    result = _restore_worktree_from_session(sess)

    assert result == "entered"
    ctx = get_ctx()
    assert ctx.worktree_path == os.path.abspath(str(wt))
    assert ctx.worktree_branch == "feat-resume"
    assert ctx.cwd == os.path.abspath(str(wt))
    # Process cwd is NOT changed by enter_worktree (Tier 3 #2)
    assert os.getcwd() == str(proj)


def test_restore_stale_when_worktree_deleted(git_project, tmp_path):
    _proj, sess = git_project
    # Session thinks it was saved in a worktree that no longer exists
    sess.worktree_path = str(tmp_path / "deleted_worktree")
    sess.worktree_branch = "feat-deleted"

    result = _restore_worktree_from_session(sess)

    assert result == "stale"
    # Session fields nulled so the next save doesn't repeat the bogus entry
    assert sess.worktree_path is None
    assert sess.worktree_branch is None
    # ctx never entered the stale worktree
    assert get_ctx().worktree_path is None


def test_restore_error_branch_on_enter_worktree_failure(git_project, tmp_path, monkeypatch):
    """If enter_worktree itself raises, surface 'error' and leave ctx intact."""
    proj, sess = git_project
    wt = tmp_path / "wt_err"
    subprocess.run(
        ["git", "-C", str(proj), "worktree", "add", str(wt), "-b", "feat-err"],
        check=True, capture_output=True,
    )
    sess.worktree_path = str(wt)
    sess.worktree_branch = "feat-err"

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated enter failure")

    # Patch the import that the helper resolves inside the function
    import aru.runtime as rt
    monkeypatch.setattr(rt, "enter_worktree", boom)

    result = _restore_worktree_from_session(sess)

    assert result == "error"
    # Session fields preserved (user may want to retry)
    assert sess.worktree_path == str(wt)
    assert sess.worktree_branch == "feat-err"


def test_restore_missing_session_attrs_treated_as_none(git_project):
    """Older sessions without worktree_path/worktree_branch attrs must not crash."""
    _proj, sess = git_project
    # Strip the attributes to simulate a pre-Tier-3 session dict loaded into memory
    del sess.worktree_path
    del sess.worktree_branch
    assert _restore_worktree_from_session(sess) == "none"
