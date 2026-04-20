"""Per-language formatter dispatch, triggered on the ``file.changed`` hook.

Typical config shape (``aru.json``)::

    {
      "format": {
        "enabled": true,
        "python":     { "command": "black",   "args": ["-q", "-"] },
        "typescript": { "command": "prettier", "args": ["--stdin-filepath", "{path}"] },
        "rust":       { "command": "rustfmt",  "args": ["--emit=stdout"] }
      }
    }

``{path}`` in ``args`` is substituted with the actual file path at runtime —
prettier needs it to pick the right parser.

The manager is intentionally simple: one subscriber handler, no threading,
and idempotence derived from byte-matching formatter output against the
file's current content.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from aru.format.runner import run_formatter

logger = logging.getLogger("aru.format")


# Extension -> language id used to look up the config block.
_EXTENSION_LANG = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
}


class FormatManager:
    """Language-aware formatter dispatcher subscribed to ``file.changed``."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config: dict[str, Any] = config or {}
        # Paths currently being formatted — avoids reentrancy when the write
        # after formatting itself emits file.changed. Guarded by try/finally
        # at the call site so a crash never leaves the path pinned.
        self._in_progress: set[str] = set()
        # Languages whose formatter command isn't available — we short-circuit
        # to avoid spamming FileNotFoundError on every subsequent mutation.
        self._failed_langs: set[str] = set()

    def enabled(self) -> bool:
        return bool(self.config.get("enabled"))

    def language_for_file(self, path: str) -> str | None:
        ext = os.path.splitext(path)[1].lower()
        return _EXTENSION_LANG.get(ext)

    def _resolve_command(self, lang: str, path: str) -> list[str] | None:
        cfg = self.config.get(lang)
        if not isinstance(cfg, dict):
            return None
        command = cfg.get("command")
        if not command:
            return None
        args = [
            str(a).replace("{path}", path)
            for a in (cfg.get("args") or [])
        ]
        return [str(command), *args]

    async def handle_file_changed(self, payload: dict[str, Any]) -> None:
        """Subscriber callback for the ``file.changed`` plugin event."""
        if not self.enabled():
            return
        path = payload.get("path")
        mtype = payload.get("mutation_type")
        if not path or mtype in (None, "unknown", "delete"):
            return
        abs_path = os.path.abspath(path)
        if abs_path in self._in_progress:
            return
        lang = self.language_for_file(abs_path)
        if lang is None or lang in self._failed_langs:
            return
        command = self._resolve_command(lang, abs_path)
        if command is None:
            return

        self._in_progress.add(abs_path)
        try:
            await self._format_one(abs_path, command, lang)
        finally:
            self._in_progress.discard(abs_path)

    async def _format_one(self, path: str, command: list[str], lang: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                original = f.read()
        except OSError as exc:
            logger.debug("format skip: cannot read %s (%s)", path, exc)
            return

        formatted = await run_formatter(command, original)
        if formatted is None:
            # Heuristic: if the very first attempt for a language fails to
            # spawn (FileNotFoundError), the runner has already logged it.
            # Mark the language as failed so subsequent edits don't retry.
            self._failed_langs.add(lang)
            return

        # Byte-match idempotence: breaks the file.changed -> format -> write
        # -> file.changed loop on the first pass.
        if formatted == original:
            return

        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(formatted)
        except OSError as exc:
            logger.warning("format write failed for %s: %s", path, exc)


# ── Global singleton ─────────────────────────────────────────────────

_manager: FormatManager | None = None


def get_format_manager() -> FormatManager | None:
    return _manager


def set_format_manager(mgr: FormatManager | None) -> None:
    global _manager
    _manager = mgr


def install_format_from_config(config_format: dict | None) -> FormatManager | None:
    """Instantiate and register the global manager for config-driven subscribe.

    Returns the manager when any language is configured, else ``None``.
    Caller is responsible for subscribing ``handle_file_changed`` to the
    ``file.changed`` event on the plugin manager.
    """
    if not config_format or not isinstance(config_format, dict):
        set_format_manager(None)
        return None
    mgr = FormatManager(config=config_format)
    set_format_manager(mgr)
    return mgr
