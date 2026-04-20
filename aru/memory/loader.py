"""Load per-project memory index into the system prompt."""

from __future__ import annotations

from pathlib import Path

from aru.memory.store import index_path, memory_dir_for_project

# Mirror CC's cap of ~200 lines to keep prompt stable even if the index
# grows across projects with heavy use.
MAX_INDEX_LINES = 200


def load_memory_index(project_root: str, base: str | None = None) -> str:
    """Return the truncated text of MEMORY.md for *project_root*, or empty."""
    mem_dir = memory_dir_for_project(project_root, base=base)
    path = index_path(mem_dir)
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.splitlines()
    if len(lines) > MAX_INDEX_LINES:
        lines = lines[:MAX_INDEX_LINES]
        lines.append(f"... [{len(text.splitlines()) - MAX_INDEX_LINES} more truncated] ...")
    return "\n".join(lines)


def memory_section_for_prompt(project_root: str, base: str | None = None) -> str:
    """Format the memory index as a system-prompt section, or return ''."""
    body = load_memory_index(project_root, base=base).strip()
    if not body:
        return ""
    return (
        "\n\n## Project memory (from past sessions)\n\n"
        "Durable facts extracted from past sessions in this project. "
        "Respect preferences, honour corrections, and do not repeat "
        "feedback the user has already given.\n\n"
        f"{body}\n"
    )
