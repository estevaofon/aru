"""Auto-memory extraction — Tier 2 #4.

Per-project durable facts extracted from user↔assistant turns and loaded
back into the system prompt on subsequent sessions.

Components:
- ``store``     — disk layout, read/write of MEMORY.md + individual files
- ``extractor`` — async extraction triggered by the ``turn.end`` hook
- ``loader``    — renders MEMORY.md into the system prompt at startup

Storage layout:

    ~/.aru/projects/<sha256(project_root)[:12]>/memory/
      ├── MEMORY.md              # one-line-per-memory index
      ├── feedback_*.md          # one file per memory, YAML frontmatter + body
      └── user_*.md

Config (aru.json):

    {
      "memory": {
        "auto_extract": true,                # default false — opt-in
        "model_ref": "anthropic/claude-haiku-4-5",
        "min_turn_tokens": 500
      }
    }
"""

from aru.memory.loader import load_memory_index, memory_section_for_prompt
from aru.memory.store import (
    MemoryEntry,
    delete_memory,
    list_memories,
    memory_dir_for_project,
    read_memory,
    write_memory,
)

__all__ = [
    "MemoryEntry",
    "delete_memory",
    "list_memories",
    "load_memory_index",
    "memory_dir_for_project",
    "memory_section_for_prompt",
    "read_memory",
    "write_memory",
]
