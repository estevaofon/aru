"""UIAdapter protocol + REPL implementation (E6a).

Unifies interactive prompts (choice menu, yes/no, free text, print,
toast) behind a single adapter so call sites don't need to know whether
they're running in REPL (Rich + prompt_toolkit) or TUI (Textual modal
screens). ``ctx.ui`` holds the adapter; E7 migrates the 5 legacy call
sites — ``check_permission``, ``_prompt_plan_approval`` (tool + runner),
``ask_yes_no``, ``/undo`` — to go through it.

Design:

* Synchronous API — matches existing call sites, which are sync-first.
  The TUI implementation (``aru.tui.ui.TuiUI``) uses
  ``app.call_from_thread(app.push_screen_wait(...))`` internally when
  invoked from a tool thread or ``asyncio.to_thread`` context.
* Returns plain Python types (int, str, bool) so adapters are
  interchangeable without touching the caller's control flow.
* ``cancel_value`` / ``default`` semantics mirror ``select.select_option``.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable


@runtime_checkable
class UIAdapter(Protocol):
    """Blocking interactive-prompt adapter. Implementations may dispatch
    across thread / loop boundaries internally.
    """

    def ask_choice(
        self,
        options: Sequence[str],
        *,
        title: str | None = None,
        default: int = 0,
        cancel_value: int | None = None,
        details: Any = None,
    ) -> int | None:
        """Show a numbered option menu. Returns the 0-based index or
        ``cancel_value`` on Esc/Ctrl+C."""
        ...

    def confirm(self, prompt: str, default: bool = False) -> bool:
        """Yes / no question. Returns bool."""
        ...

    def ask_text(
        self,
        prompt: str,
        *,
        default: str = "",
        multiline: bool = False,
        password: bool = False,
    ) -> str:
        """Free-form text prompt. Returns user input or ``default``.

        When ``password`` is True the input is masked (no echo / dots) — used
        for API keys so they don't land in terminal scrollback.
        """
        ...

    def print(self, renderable: Any) -> None:  # noqa: A003 — mirrors Rich API
        """Emit a renderable (string / Rich Renderable) to the active sink."""
        ...

    def notify(self, message: str, severity: str = "info") -> None:
        """Transient toast-style message (warnings, info)."""
        ...


# ── REPL implementation ──────────────────────────────────────────────


class ReplUI:
    """UIAdapter backed by Rich + prompt_toolkit (the legacy stack).

    Delegates:

    * ``ask_choice`` → :func:`aru.select.select_option`
    * ``confirm``    → :func:`aru.commands.ask_yes_no`
    * ``ask_text``   → ``console.input`` (or ``prompt_toolkit`` multiline
      if the caller requests it)
    * ``print``      → ``console.print``
    * ``notify``     → ``console.print`` with severity-tinted style
    """

    def __init__(self, console: Any = None) -> None:
        from aru.display import console as _default_console
        self.console = console or _default_console

    def ask_choice(
        self,
        options: Sequence[str],
        *,
        title: str | None = None,
        default: int = 0,
        cancel_value: int | None = None,
        details: Any = None,
    ) -> int | None:
        # ``details`` is a Rich renderable shown *above* the menu in the
        # TUI modal; in REPL the caller typically printed it already via
        # ``console.print`` before calling ask_choice, so we just render
        # it here for consistency when provided.
        if details is not None:
            try:
                self.console.print(details)
            except Exception:
                pass
        from aru.select import select_option
        return select_option(
            options, title=title, default=default, cancel_value=cancel_value
        )

    def confirm(self, prompt: str, default: bool = False) -> bool:
        from aru.commands import ask_yes_no
        return ask_yes_no(prompt)

    def ask_text(
        self,
        prompt: str,
        *,
        default: str = "",
        multiline: bool = False,
        password: bool = False,
    ) -> str:
        try:
            if password:
                # console.input(password=True) masks the echo with the
                # console's password char (no scrollback leak).
                answer = self.console.input(prompt, password=True)
            else:
                answer = self.console.input(prompt)
        except (EOFError, KeyboardInterrupt):
            return default
        return answer if answer else default

    def print(self, renderable: Any) -> None:  # noqa: A003
        try:
            self.console.print(renderable)
        except Exception:
            # Last resort so a malformed renderable can't crash a turn.
            try:
                print(str(renderable))
            except Exception:
                pass

    def notify(self, message: str, severity: str = "info") -> None:
        color = {
            "info": "cyan",
            "warn": "yellow",
            "warning": "yellow",
            "error": "red",
            "success": "green",
        }.get(severity, "cyan")
        try:
            self.console.print(f"[{color}]{message}[/{color}]")
        except Exception:
            pass


def install_repl_ui_on_ctx(ctx: Any) -> ReplUI:
    """Attach a ``ReplUI`` to the given runtime context. Idempotent."""
    if getattr(ctx, "ui", None) is None or not isinstance(ctx.ui, ReplUI):
        ui = ReplUI(console=getattr(ctx, "console", None))
        ctx.ui = ui
        return ui
    return ctx.ui
