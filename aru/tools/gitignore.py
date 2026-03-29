"""Gitignore-aware file filtering for codebase operations."""

from __future__ import annotations

import os
from typing import Iterator

import pathspec


def normalize_path(path: str) -> str:
    """Convert backslashes to forward slashes and remove trailing slashes."""
    return path.replace("\\", "/").rstrip("/")

# Hardcoded fallback patterns (always excluded even without .gitignore)
_FALLBACK_PATTERNS = [
    ".git",
    "node_modules",
    "__pycache__",
    "venv",
    ".venv",
    ".aru",
    "*.pyc",
    "*.pyo",
]

# Cache: {(root_dir, gitignore_mtime): PathSpec}
_cache: dict[tuple[str, float], pathspec.PathSpec] = {}


def _find_git_root(start: str) -> str | None:
    """Walk up from start directory to find the git root (directory containing .git)."""
    current = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def load_gitignore(root_dir: str) -> pathspec.PathSpec:
    """Parse .gitignore from root_dir combined with hardcoded fallback patterns.

    Results are cached by root_dir and .gitignore mtime.
    """
    root_dir = os.path.abspath(root_dir)
    gitignore_path = os.path.join(root_dir, ".gitignore")

    mtime = 0.0
    if os.path.isfile(gitignore_path):
        mtime = os.path.getmtime(gitignore_path)

    cache_key = (root_dir, mtime)
    if cache_key in _cache:
        return _cache[cache_key]

    # Clear old entries for this root_dir
    _cache.pop(next((k for k in _cache if k[0] == root_dir), (None, None)), None)

    patterns = list(_FALLBACK_PATTERNS)
    if os.path.isfile(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)

    spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    _cache[cache_key] = spec
    return spec


def is_ignored(path: str, root_dir: str) -> bool:
    """Check if a relative path should be ignored based on .gitignore rules.

    Args:
        path: Relative path to check (forward slashes preferred).
        root_dir: Project root directory containing .gitignore.
    """
    spec = load_gitignore(root_dir)
    # Normalize to forward slashes for pathspec
    normalized = path.replace("\\", "/")
    return spec.match_file(normalized)


def walk_filtered(directory: str) -> Iterator[tuple[str, list[str], list[str]]]:
    """Walk directory tree, filtering out gitignored files and directories.

    Drop-in replacement for os.walk() that respects .gitignore rules.
    Finds the git root (or uses the directory itself) to load ignore patterns.
    """
    directory = os.path.abspath(directory)
    root_dir = _find_git_root(directory) or directory
    spec = load_gitignore(root_dir)

    for dirpath, dirs, files in os.walk(directory):
        # Filter directories in-place to prevent descending into ignored dirs
        dirs[:] = [
            d for d in dirs
            if not spec.match_file(os.path.relpath(os.path.join(dirpath, d), root_dir).replace("\\", "/") + "/")
        ]

        # Filter files
        filtered_files = [
            f for f in files
            if not spec.match_file(os.path.relpath(os.path.join(dirpath, f), root_dir).replace("\\", "/"))
        ]

        yield dirpath, dirs, filtered_files
