"""Logging → ChatPane bridge for TUI mode.

Background
----------
Agno (and other libraries) report API errors via Python's ``logging``
module — for example, an OpenRouter rate-limit looks like::

    ERROR    Rate limit error from OpenAI API: Error code: 429 ...
    ERROR    Error in Agent run: Provider returned error

In REPL mode these records reach the user because Python's default log
handler writes to ``sys.stderr`` and the terminal renders it directly.
In TUI mode Textual takes over the alternate screen and captures stdout
/ stderr — those ERROR lines vanish, leaving the user staring at a
spinner that eventually stops without any message.

This module installs a ``logging.Handler`` that forwards qualifying
records into the running ``AruApp``'s ``ChatPane`` as system messages
via ``app.call_from_thread``. The handler is idempotent (a marker
attribute on each target logger prevents double-attachment when
``run_tui`` is invoked twice in the same process, e.g. tests).
"""

from __future__ import annotations

import logging
from typing import Any

# Loggers we forward to chat. Keep this tight — capturing the root logger
# would also pick up debug noise from libraries that log at WARNING for
# routine state. We only want clearly-actionable messages.
_BRIDGE_LOGGERS: tuple[str, ...] = ("agno", "aru")

# Records below this level are dropped. ERROR is the right floor:
# WARNING from Agno is often non-actionable (e.g. "tool call schema
# coerced"). The user explicitly asked for transparency about *errors*.
_BRIDGE_LEVEL = logging.ERROR

# Sentinel attribute name set on a logger after we've attached our
# handler, so re-running the install is a no-op.
_INSTALLED_FLAG = "_aru_chat_bridge_installed"


class _ChatPaneLogHandler(logging.Handler):
    """Forward logging records into the TUI ChatPane.

    Holds a weak reference semantically (we let the App outlive the
    handler — the handler is detached in ``uninstall_chat_log_bridge``).
    Failures inside ``emit`` are swallowed: a logging handler must never
    raise, and the user can still see the error via Textual's own dev
    log if they really need it.
    """

    def __init__(self, app: Any) -> None:
        super().__init__(level=_BRIDGE_LEVEL)
        self._app = app
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            try:
                text = record.getMessage()
            except Exception:
                return
        try:
            from aru.tui.widgets.chat import ChatPane
            chat = self._app.query_one(ChatPane)
        except Exception:
            return
        try:
            self._app.call_from_thread(
                chat.add_system_message, f"Error: {text}"
            )
        except Exception:
            # Last-resort direct call — safe when already on the loop
            # and the App is still running. If even this raises, drop
            # the record silently rather than crashing the producer.
            try:
                chat.add_system_message(f"Error: {text}")
            except Exception:
                return


def install_chat_log_bridge(app: Any) -> list[logging.Handler]:
    """Attach a ChatPane bridge to each target logger.

    Returns the list of installed handlers so ``uninstall_chat_log_bridge``
    can remove them on teardown. Idempotent per logger — if a previous
    bridge from the same process is still attached, that logger is
    skipped and only loggers without a bridge get a new handler.
    """
    installed: list[logging.Handler] = []
    for name in _BRIDGE_LOGGERS:
        logger = logging.getLogger(name)
        if getattr(logger, _INSTALLED_FLAG, False):
            continue
        handler = _ChatPaneLogHandler(app)
        logger.addHandler(handler)
        # Make sure ERROR records actually fire — Agno ships at WARNING
        # by default in cli.py, but a downstream user could lower it.
        if logger.level == logging.NOTSET or logger.level > _BRIDGE_LEVEL:
            logger.setLevel(_BRIDGE_LEVEL)
        setattr(logger, _INSTALLED_FLAG, True)
        installed.append(handler)
    return installed


def uninstall_chat_log_bridge(handlers: list[logging.Handler]) -> None:
    """Detach the bridge handlers and clear the per-logger marker."""
    for name in _BRIDGE_LOGGERS:
        logger = logging.getLogger(name)
        for h in list(logger.handlers):
            if isinstance(h, _ChatPaneLogHandler):
                logger.removeHandler(h)
        if getattr(logger, _INSTALLED_FLAG, False):
            try:
                delattr(logger, _INSTALLED_FLAG)
            except AttributeError:
                pass
    # Best-effort close so file descriptors etc. don't leak (we don't
    # use any, but a custom subclass might).
    for h in handlers:
        try:
            h.close()
        except Exception:
            pass
