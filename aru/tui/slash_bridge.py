"""Slash-command bridge: reuse REPL command handlers inside the TUI (E6b).

Background
----------
The REPL's slash dispatch (``/memory``, ``/worktree``, ``/subagents``,
``/plugin``, ``/debug``, etc.) lives in ``aru/commands.py`` as a handful
of ``handle_*`` functions that print via Rich ``console.print``.  The
TUI cannot use ``console.print`` directly — Textual captures stdout and
owns the terminal.

Instead of duplicating each handler (a 440-line refactor the
plan-reviewer flagged as risky), we:

1. Swap ``aru.commands.console`` for a temporary ``Console(record=True)``.
2. Invoke the handler — it prints into the recording console.
3. Export the rendered text and return it so the TUI can push it into
   the ChatPane via ``add_system_message``.

Because every handler reads ``console`` through the module-level binding
that ``commands.py`` established at import time, rebinding that
attribute is enough — no monkeypatching of ``aru.display.console``
required and no other module is affected.

Supported commands
------------------
``/help``, ``/memory``, ``/worktree``, ``/subagents``, ``/subagent``,
``/plugin``, ``/debug`` — the handlers whose contract is "print a
report, don't mutate global state beyond their own domain".
"""

from __future__ import annotations

from typing import Any, Callable

from rich.console import Console


# Handlers that are safe to run via the record→export pipeline.
# key = command name (without leading slash)
# value = (callable or import path, arg_resolver)
#
# arg_resolver(app, body) -> tuple[args, kwargs] — so callers can decide
# whether the command takes positional args (``/memory show foo``) or
# the session / sub-command string.
def _noargs(_app: Any, _body: str) -> tuple[tuple, dict]:
    return ((), {})


def _session_only(app: Any, _body: str) -> tuple[tuple, dict]:
    return ((app.session,), {})


def _config_only(app: Any, _body: str) -> tuple[tuple, dict]:
    return ((app.config,), {})


def _body_plus_session(app: Any, body: str) -> tuple[tuple, dict]:
    return ((body, app.session), {})


def _body_only(_app: Any, body: str) -> tuple[tuple, dict]:
    return ((body,), {})


def _subagent_detail(app: Any, body: str) -> tuple[tuple, dict]:
    task_id = body.split(None, 1)[0] if body.strip() else ""
    return ((app.session, task_id), {})


def _make_handler_ref(path: str):
    """Deferred import — ``aru.commands`` is heavy, only load when asked."""
    def _get():
        mod_name, fn_name = path.rsplit(".", 1)
        import importlib
        return getattr(importlib.import_module(mod_name), fn_name)
    return _get


BRIDGED_COMMANDS: dict[str, tuple[Callable, Callable]] = {
    "help":       (_make_handler_ref("aru.commands._show_help"), _config_only),
    "memory":     (_make_handler_ref("aru.commands.handle_memory_command"),
                   _body_plus_session),
    "worktree":   (_make_handler_ref("aru.commands.handle_worktree_command"),
                   _body_only),
    "subagents":  (_make_handler_ref("aru.commands.handle_subagents_command"),
                   _session_only),
    "subagent":   (_make_handler_ref("aru.commands.handle_subagent_detail_command"),
                   _subagent_detail),
    "plugin":     (_make_handler_ref("aru.commands.handle_plugin_command"),
                   _body_only),
    "debug":      (_make_handler_ref("aru.commands.handle_debug_command"),
                   _body_only),
}


def supported_commands() -> list[str]:
    """List of slash names the bridge can dispatch. Plus always-local aliases."""
    return sorted(BRIDGED_COMMANDS.keys())


def run_bridged(name: str, body: str, app: Any) -> tuple[bool, str]:
    """Run a bridged handler and return (handled, captured_text).

    ``handled`` is False when ``name`` is not in :data:`BRIDGED_COMMANDS`
    (so the caller can fall through). ``handled`` is True even if the
    handler raised — the error text is returned so the user sees it.
    """
    entry = BRIDGED_COMMANDS.get(name.lower())
    if entry is None:
        return False, ""

    resolve_handler, resolve_args = entry
    try:
        handler = resolve_handler()
    except Exception as exc:
        return True, f"Failed to load handler for /{name}: {exc}"

    args, kwargs = resolve_args(app, body)

    import aru.commands as cmds_module
    original_console = cmds_module.console
    # ``file=StringIO()`` keeps the live output off the raw terminal — we
    # only want the recorded copy (``export_text`` below), which we mirror
    # into the ChatPane. Writing to stdout here is pointless under Textual
    # (it owns the screen) and outright unsafe now that the handler runs on
    # a worker thread; ``record=True`` still captures everything regardless
    # of the file target.
    import io
    temp = Console(
        record=True, width=100, force_terminal=True, color_system=None,
        file=io.StringIO(),
    )
    cmds_module.console = temp
    try:
        handler(*args, **kwargs)
    except Exception as exc:
        cmds_module.console = original_console
        return True, f"/{name} failed: {type(exc).__name__}: {exc}"
    finally:
        cmds_module.console = original_console

    text = temp.export_text().rstrip()
    return True, text or f"/{name} produced no output."
