"""Git worktree primitives exposed to both the REPL (via `/worktree`) and agents.

Worktrees let the user — or a sub-agent — operate on a parallel branch in
its own working directory without touching the primary repo's checkout.
This module handles:

- Locating the git repo root
- Creating / listing / removing worktrees via the `git` CLI
- Entering/exiting worktrees (chdir + context flag updates happen in
  ``aru.runtime.enter_worktree`` / ``exit_worktree``)
- The read-only tool ``worktree_info`` that lets an agent check which
  worktree is active mid-turn

Path convention: ``<project-parent>/.aru-worktrees/<branch>`` by default.
Configurable via ``aru.json`` ``worktree.base_dir`` (absolute path) or
``worktree.dirname`` (override the ``.aru-worktrees`` stem).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from aru.runtime import get_ctx


@dataclass
class WorktreeEntry:
    """Single worktree as reported by ``git worktree list --porcelain``."""

    path: str
    branch: str | None
    head: str
    is_main: bool = False


class WorktreeError(RuntimeError):
    """Raised for any failed worktree operation (git CLI error, bad input)."""


# -- Repo + path utilities -------------------------------------------------


def _project_root() -> str:
    """The session's saved project root (not the current cwd)."""
    ctx = get_ctx()
    if ctx.session is not None and getattr(ctx.session, "project_root", None):
        return ctx.session.project_root
    return os.getcwd()


def _git_repo_root(start: str) -> str:
    """Resolve the containing git repo via ``git rev-parse --show-toplevel``."""
    result = subprocess.run(
        ["git", "-C", start, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise WorktreeError(
            f"Not inside a git repository: {start}\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _default_base_dir(repo_root: str) -> str:
    """Location of ``.aru-worktrees/`` — sibling of the repo root by default."""
    ctx = get_ctx()
    cfg = getattr(ctx, "config", None)
    worktree_cfg = getattr(cfg, "worktree", None) if cfg else None
    if isinstance(worktree_cfg, dict):
        explicit = worktree_cfg.get("base_dir")
        if explicit:
            return os.path.abspath(explicit)
        dirname = worktree_cfg.get("dirname") or ".aru-worktrees"
    else:
        dirname = ".aru-worktrees"
    parent = os.path.dirname(os.path.abspath(repo_root)) or os.path.abspath(repo_root)
    return os.path.join(parent, dirname)


def _worktree_path_for(branch: str) -> str:
    """Default path we'd create for *branch* under the current repo's base_dir."""
    repo = _git_repo_root(_project_root())
    return os.path.join(_default_base_dir(repo), branch)


# -- Git CLI wrappers ------------------------------------------------------


def list_worktrees() -> list[WorktreeEntry]:
    """Parse ``git worktree list --porcelain`` into structured entries."""
    repo = _git_repo_root(_project_root())
    result = subprocess.run(
        ["git", "-C", repo, "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise WorktreeError(f"git worktree list failed: {result.stderr.strip()}")

    entries: list[WorktreeEntry] = []
    cur: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if cur.get("worktree"):
                entries.append(WorktreeEntry(
                    path=cur["worktree"],
                    branch=cur.get("branch", "").replace("refs/heads/", "") or None,
                    head=cur.get("HEAD", ""),
                    is_main=(os.path.abspath(cur["worktree"]) == os.path.abspath(repo)),
                ))
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value
    # Trailing entry without blank-line terminator
    if cur.get("worktree"):
        entries.append(WorktreeEntry(
            path=cur["worktree"],
            branch=cur.get("branch", "").replace("refs/heads/", "") or None,
            head=cur.get("HEAD", ""),
            is_main=(os.path.abspath(cur["worktree"]) == os.path.abspath(repo)),
        ))
    return entries


def _find_worktree_by_branch(branch: str) -> WorktreeEntry | None:
    for e in list_worktrees():
        if e.branch == branch:
            return e
    return None


def create_worktree(branch: str, from_branch: str | None = None, path: str | None = None) -> str:
    """Run ``git worktree add`` and return the absolute worktree path.

    If *path* is provided, uses it as-is (must be absolute or relative to cwd).
    Otherwise defaults to ``<base_dir>/<branch>``.

    When a worktree already exists for *branch*, returns its path instead
    of erroring — lets ``/worktree enter`` be idempotent.
    """
    existing = _find_worktree_by_branch(branch)
    if existing is not None:
        return existing.path

    repo = _git_repo_root(_project_root())
    target = os.path.abspath(path) if path else _worktree_path_for(branch)
    os.makedirs(os.path.dirname(target), exist_ok=True)

    # Create a new branch (-b) when branch doesn't exist yet; otherwise check
    # it out into the worktree directly.
    args = ["git", "-C", repo, "worktree", "add", target]
    if _branch_exists(repo, branch):
        args.append(branch)
    else:
        args.extend(["-b", branch])
        if from_branch:
            args.append(from_branch)

    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise WorktreeError(
            f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    from aru.runtime import _schedule_publish
    _schedule_publish("worktree.create", {"path": target, "branch": branch})
    return target


def _branch_exists(repo: str, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def remove_worktree(branch: str, force: bool = False) -> str:
    """Remove the worktree for *branch* via ``git worktree remove``.

    If the REPL is currently inside the worktree being removed, we ``exit``
    first so the process doesn't leave cwd pointing at a dead directory.
    """
    entry = _find_worktree_by_branch(branch)
    if entry is None:
        raise WorktreeError(f"No worktree for branch {branch!r}")
    if entry.is_main:
        raise WorktreeError(
            f"Refusing to remove the main worktree at {entry.path}"
        )

    ctx = get_ctx()
    if ctx.worktree_path and os.path.abspath(ctx.worktree_path) == os.path.abspath(entry.path):
        from aru.runtime import exit_worktree
        exit_worktree()

    repo = _git_repo_root(_project_root())
    args = ["git", "-C", repo, "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(entry.path)

    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise WorktreeError(
            f"git worktree remove failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    from aru.runtime import _schedule_publish
    _schedule_publish("worktree.remove", {"path": entry.path, "branch": branch})
    return entry.path


# -- Agent-facing tool -----------------------------------------------------


def worktree_info() -> str:
    """Report which git worktree the session is currently operating in.

    Returns a short human-readable string, e.g. ``"worktree: feat/foo at
    /abs/path"`` or ``"worktree: main (project root)"``. Useful for agents
    that need to branch their reasoning based on the active worktree.
    """
    ctx = get_ctx()
    if ctx.worktree_path:
        branch = ctx.worktree_branch or "(detached)"
        return f"worktree: {branch} at {ctx.worktree_path}"
    root = _project_root()
    return f"worktree: main (project root) at {root}"
