"""Automatic formatter integration — Tier 3 Stage 1.

Subscribes to the ``file.changed`` hook and pipes every mutated file through
the formatter configured for its language (``black`` for .py, ``prettier``
for .ts/.tsx, ``rustfmt`` for .rs, etc.). Runs fire-and-forget so the agent
turn never waits on formatting.

Idempotence by byte-match: if the formatter output equals the current file
content, the manager skips the write. That breaks the
``write -> file.changed -> format -> write`` loop at the root — no timers, no
state beyond a per-path ``_in_progress`` set managed with ``try/finally``.
"""

from aru.format.manager import (
    FormatManager,
    get_format_manager,
    install_format_from_config,
)

__all__ = [
    "FormatManager",
    "get_format_manager",
    "install_format_from_config",
]
