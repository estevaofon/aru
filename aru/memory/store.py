"""Disk layout + read/write for per-project auto-memory.

Files:
- ``MEMORY.md``           : one-line-per-memory index; always loaded in prompt
- ``<slug>.md``           : individual memory with YAML frontmatter + body

The slug is ``<type>_<descriptive_name>`` mapped from the memory's name via a
filesystem-safe transformation. Collisions are resolved by appending ``_2``
etc. until free.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# ── Constants ────────────────────────────────────────────────────────

# Hard cap on concurrent memories per project. Oldest files (by mtime) are
# evicted when exceeded so long-running projects don't grow unbounded.
MAX_MEMORIES_PER_PROJECT = 50

VALID_MEMORY_TYPES = frozenset({"user", "feedback", "project", "reference"})


@dataclass
class MemoryEntry:
    """Single memory — frontmatter + body + filename slug."""

    name: str
    description: str
    type: str
    body: str
    slug: str = ""  # filled by write_memory; stable filename minus `.md`

    @property
    def filename(self) -> str:
        return f"{self.slug}.md"


# ── Paths ────────────────────────────────────────────────────────────

def _project_hash(project_root: str) -> str:
    return hashlib.sha256(os.path.abspath(project_root).encode("utf-8")).hexdigest()[:12]


def memory_dir_for_project(project_root: str, base: str | None = None) -> Path:
    """Return (and create, if needed) the memory directory for *project_root*.

    Defaults to ``~/.aru/projects/<hash>/memory``. Override ``base`` (test-only).
    """
    base_path = Path(base) if base else Path.home() / ".aru" / "projects"
    d = base_path / _project_hash(project_root) / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def index_path(mem_dir: Path) -> Path:
    return mem_dir / "MEMORY.md"


# ── Slug generation ──────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str, mem_type: str) -> str:
    base = _SLUG_RE.sub("_", name.strip().lower()).strip("_")[:40]
    return f"{mem_type}_{base}" if base else mem_type


def _unique_slug(mem_dir: Path, proposed: str) -> str:
    slug = proposed
    i = 2
    while (mem_dir / f"{slug}.md").exists():
        slug = f"{proposed}_{i}"
        i += 1
    return slug


# ── Frontmatter helpers ──────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, body


def _render_memory_file(entry: MemoryEntry) -> str:
    return (
        "---\n"
        f"name: {entry.name}\n"
        f"description: {entry.description}\n"
        f"type: {entry.type}\n"
        "---\n"
        f"{entry.body.rstrip()}\n"
    )


# ── Index helpers ────────────────────────────────────────────────────

_INDEX_HEADER = "# Memory Index\n\n"


def _read_index(mem_dir: Path) -> list[str]:
    """Return the lines of MEMORY.md below the header, skipping blanks."""
    path = index_path(mem_dir)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    content: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        content.append(line.rstrip())
    return content


def _write_index(mem_dir: Path, lines: Iterable[str]) -> None:
    index_path(mem_dir).write_text(
        _INDEX_HEADER + "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )


def _index_line_for(entry: MemoryEntry) -> str:
    return f"- [{entry.name}]({entry.filename}) — {entry.description}"


# ── Public API ───────────────────────────────────────────────────────

def write_memory(project_root: str, entry: MemoryEntry,
                 base: str | None = None) -> MemoryEntry:
    """Persist *entry* to disk. Populates ``entry.slug`` and returns it."""
    if entry.type not in VALID_MEMORY_TYPES:
        raise ValueError(
            f"Invalid memory type {entry.type!r}; must be one of "
            f"{sorted(VALID_MEMORY_TYPES)}."
        )
    mem_dir = memory_dir_for_project(project_root, base=base)
    slug = _unique_slug(mem_dir, _slugify(entry.name, entry.type))
    entry.slug = slug
    (mem_dir / entry.filename).write_text(_render_memory_file(entry), encoding="utf-8")

    # Append to index (dedupe on exact line)
    lines = _read_index(mem_dir)
    new_line = _index_line_for(entry)
    if new_line not in lines:
        lines.append(new_line)
    _write_index(mem_dir, lines)

    # Evict oldest if over cap
    _evict_if_over_cap(mem_dir)
    return entry


def read_memory(project_root: str, slug: str,
                base: str | None = None) -> MemoryEntry | None:
    """Load a single memory by slug, or return None if missing/corrupt."""
    mem_dir = memory_dir_for_project(project_root, base=base)
    path = mem_dir / f"{slug}.md"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fields, body = _parse_frontmatter(text)
    name = fields.get("name", slug)
    description = fields.get("description", "")
    mtype = fields.get("type", "")
    if mtype not in VALID_MEMORY_TYPES:
        return None
    return MemoryEntry(
        name=name, description=description, type=mtype,
        body=body.strip(), slug=slug,
    )


def list_memories(project_root: str, base: str | None = None) -> list[MemoryEntry]:
    """Return all memories for *project_root*, skipping corrupt files."""
    mem_dir = memory_dir_for_project(project_root, base=base)
    results: list[MemoryEntry] = []
    for path in sorted(mem_dir.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        entry = read_memory(project_root, path.stem, base=base)
        if entry is not None:
            results.append(entry)
    return results


def delete_memory(project_root: str, slug: str, base: str | None = None) -> bool:
    """Delete the memory file + remove its index line. True if something was removed."""
    mem_dir = memory_dir_for_project(project_root, base=base)
    path = mem_dir / f"{slug}.md"
    removed = path.exists()
    if removed:
        path.unlink()
    # Rebuild index from surviving files
    surviving = list_memories(project_root, base=base)
    _write_index(mem_dir, [_index_line_for(e) for e in surviving])
    return removed


def clear_memory(project_root: str, base: str | None = None) -> int:
    """Remove every memory and the index. Returns count removed."""
    mem_dir = memory_dir_for_project(project_root, base=base)
    count = 0
    for path in mem_dir.glob("*.md"):
        try:
            path.unlink()
            count += 1
        except OSError:
            pass
    return count


def _evict_if_over_cap(mem_dir: Path) -> None:
    files = [p for p in mem_dir.glob("*.md") if p.name != "MEMORY.md"]
    if len(files) <= MAX_MEMORIES_PER_PROJECT:
        return
    # Oldest first
    files.sort(key=lambda p: p.stat().st_mtime)
    excess = len(files) - MAX_MEMORIES_PER_PROJECT
    for p in files[:excess]:
        try:
            p.unlink()
        except OSError:
            pass
    # Rebuild index from what remains
    remaining_slugs = {p.stem for p in mem_dir.glob("*.md") if p.name != "MEMORY.md"}
    lines = _read_index(mem_dir)
    surviving = [ln for ln in lines if any(f"]({s}.md)" in ln for s in remaining_slugs)]
    _write_index(mem_dir, surviving)
