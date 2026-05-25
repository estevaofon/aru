"""TuiUI — UIAdapter backed by Textual ModalScreen (E6a + E7).

Sync-on-call: the legacy call sites (``check_permission``, plan approval,
``ask_yes_no``, ``/undo``) are synchronous. In TUI mode they are either
invoked from an ``asyncio.to_thread`` tool call or directly from the
runner coroutine wrapped with ``asyncio.to_thread``. Either way the
calling context is NOT the Textual event loop.

``TuiUI`` bridges the thread↔loop boundary with ``App.call_from_thread``
which is Textual's documented cross-thread entry point. ``push_screen_wait``
is an async coroutine that resolves with the modal's dismiss value;
wrapping it with ``call_from_thread`` schedules it on the App's loop and
blocks the caller thread until it resolves.

Contract notes:

* NEVER call these methods from the App's own event loop — it will
  deadlock. Runner-level prompts MUST be wrapped in
  ``await asyncio.to_thread(ctx.ui.ask_choice, ...)``.
* The App passed to ``TuiUI`` must be the currently-running app; if
  the app has been shut down, calls raise ``RuntimeError`` (loop closed).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

from aru.tui.screens import ChoiceModal, ConfirmModal, TextInputModal

_log = logging.getLogger("aru.tui.ui")


class TuiUI:
    """UIAdapter backed by Textual ModalScreens."""

    def __init__(self, app: Any) -> None:
        self.app = app

    @staticmethod
    def _on_app_thread() -> bool:
        """True when the caller is running on an asyncio event-loop thread.

        ``TuiUI``'s blocking prompts bridge to the App loop via
        ``App.call_from_thread``, which *requires* the caller to be on a
        DIFFERENT thread than the loop (otherwise it raises rather than
        deadlock). The correct callers reach us from ``asyncio.to_thread`` /
        ``_thread_tool`` workers — plain threads with no running loop. If a
        loop IS running in this thread, we are on the App's own event-loop
        thread and must not block: that would freeze the loop so the prompt
        could never be drawn or answered. The guards below degrade safely in
        that case instead of surfacing the cryptic ``call_from_thread``
        ``RuntimeError``. The real fix for any such caller is to wrap the
        call in ``await asyncio.to_thread(ctx.ui.<method>, ...)``.
        """
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    # ── choice ────────────────────────────────────────────────────────

    def ask_choice(
        self,
        options: Sequence[str],
        *,
        title: str | None = None,
        default: int = 0,
        cancel_value: int | None = None,
        details: Any = None,
    ) -> int | None:
        # Two paths, chosen by whether we have preview material:
        #
        # With ``details`` (edit/write diff, plan summary) → mount the
        #   preview AND the approval prompt inline in the ChatPane. The
        #   user can scroll the main window freely to review the full
        #   preview above, then Enter on the prompt below. Matches
        #   OpenCode's UX: nothing gets hidden behind a screen-takeover
        #   modal.
        # Without ``details`` (/undo, generic menus) → compact modal is
        #   fine; there's no context above it the user needs to read.
        if details is not None:
            return self._run_inline_choice(
                options,
                title=title,
                default=default,
                cancel_value=cancel_value,
                details=details,
            )
        modal = ChoiceModal(
            options,
            title=title,
            default=default,
            cancel_value=cancel_value,
            details=None,
        )
        return self._run_modal(modal)

    def _run_inline_choice(
        self,
        options: Sequence[str],
        *,
        title: str | None,
        default: int,
        cancel_value: Any,
        details: Any,
        timeout_s: float = 300.0,
    ) -> Any:
        """Mount the preview + ``InlineChoicePrompt`` in the ChatPane.

        Blocks the calling thread on a ``threading.Event`` until the
        user answers, mirroring ``_run_modal`` so the sync call sites
        (``check_permission``, plan approval) keep their contract.
        """
        if self._on_app_thread():
            # Contract violation (see ``_on_app_thread``): a caller dispatched
            # this blocking prompt from the App's own event-loop thread. We
            # cannot show it here, so degrade to ``cancel_value`` rather than
            # crash the turn with ``call_from_thread``'s RuntimeError.
            _log.warning(
                "TuiUI.ask_choice invoked on the event-loop thread; returning "
                "cancel_value=%r without prompting. Wrap the call site in "
                "asyncio.to_thread.",
                cancel_value,
            )
            return cancel_value

        import threading

        from aru.tui.widgets.chat import ChatPane
        from aru.tui.widgets.inline_choice import InlineChoicePrompt

        done = threading.Event()
        result: dict[str, Any] = {}

        def _on_choice(value: Any) -> None:
            result["value"] = value
            done.set()

        def _mount() -> None:
            # Runs on the App loop — safe to touch widgets.
            try:
                chat = self.app.query_one(ChatPane)
            except Exception as exc:
                # No chat pane (edge case: App shutting down) — don't
                # hang the caller; signal cancel.
                result["value"] = cancel_value
                result["error"] = f"ChatPane unavailable: {exc}"
                done.set()
                return
            try:
                chat.add_renderable(details, scrollable=False)
                prompt = InlineChoicePrompt(
                    options,
                    title=title,
                    default=default,
                    cancel_value=cancel_value,
                    on_choice=_on_choice,
                )
                chat.mount(prompt)
                # Land the prompt at the bottom of the viewport — the
                # input bar is hidden while the prompt is active, so the
                # prompt itself becomes the only interactive surface and
                # the user's eye should settle on it immediately.
                # Short previews fit above the prompt in the same view;
                # long previews push off the top and the user scrolls up
                # to read them. Either way, the question is never
                # parked mid-screen or below the fold.
                try:
                    chat.scroll_end(animate=False)
                except Exception:
                    pass
            except Exception as exc:
                result["value"] = cancel_value
                result["error"] = f"mount failed: {exc}"
                done.set()

        # Hide the "thinking…" spinner while the prompt owns the screen — we
        # are parked waiting on the user, not computing, so an animated
        # spinner there is misleading. Restored in the finally regardless of
        # how the wait ends.
        self._suppress_thinking()
        try:
            try:
                self.app.call_from_thread(_mount)
            except Exception as exc:
                raise RuntimeError(
                    f"TuiUI inline-choice dispatch failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            if not done.wait(timeout=timeout_s):
                raise RuntimeError(
                    f"TuiUI inline-choice timed out after {timeout_s:.0f}s"
                )
            return result.get("value")
        finally:
            self._release_thinking()

    # ── confirm ───────────────────────────────────────────────────────

    def confirm(self, prompt: str, default: bool = False) -> bool:
        modal = ConfirmModal(prompt, default=default)
        result = self._run_modal(modal)
        # ModalScreen.dismiss(False) would naturally arrive as False; None
        # only happens if the call site bypassed escape handling. Default
        # to the caller's default in that edge case.
        return default if result is None else bool(result)

    # ── text input ────────────────────────────────────────────────────

    def ask_text(
        self,
        prompt: str,
        *,
        default: str = "",
        multiline: bool = False,  # TODO: multi-line TextArea modal (deferred)
        password: bool = False,
    ) -> str:
        modal = TextInputModal(prompt, default=default, password=password)
        result = self._run_modal(modal)
        return default if result is None else result

    # ── print / notify ───────────────────────────────────────────────

    def print(self, renderable: Any) -> None:  # noqa: A003
        # In TUI the print sink routes to the Chat pane once E3b lands.
        # For now, forward to the App's built-in notify as a fallback so
        # messages are visible.
        try:
            self.app.call_from_thread(self._app_print, renderable)
        except Exception:
            pass

    def _app_print(self, renderable: Any) -> None:
        # Runs on the Textual event loop — safe to touch widgets.
        try:
            chat = self.app.query_one("ChatPane")
            chat.add_system_message(str(renderable))  # type: ignore[attr-defined]
            return
        except Exception:
            pass
        # Fallback: transient toast
        try:
            self.app.notify(str(renderable))
        except Exception:
            pass

    def notify(self, message: str, severity: str = "info") -> None:
        try:
            self.app.call_from_thread(self.app.notify, message, severity=severity)
        except Exception:
            pass

    # ── internal ──────────────────────────────────────────────────────

    def _suppress_thinking(self) -> None:
        """Hide the ThinkingIndicator while a blocking prompt is open.

        Safe to call from a tool worker thread — the widget mutation is
        marshalled onto the App loop. Best-effort: any failure (App shutting
        down, indicator not mounted) is swallowed so a UI hiccup never blocks
        a permission decision. Paired with ``_release_thinking``.
        """
        try:
            from aru.tui.widgets.thinking import ThinkingIndicator

            self.app.call_from_thread(
                lambda: self.app.query_one(ThinkingIndicator).suppress()
            )
        except Exception:
            pass

    def _release_thinking(self) -> None:
        """Undo one ``_suppress_thinking`` once the prompt closes."""
        try:
            from aru.tui.widgets.thinking import ThinkingIndicator

            self.app.call_from_thread(
                lambda: self.app.query_one(ThinkingIndicator).release()
            )
        except Exception:
            pass

    def _run_modal(self, modal: Any, timeout_s: float = 300.0) -> Any:
        """Push a ModalScreen and block until it is dismissed.

        Uses ``push_screen(modal, callback)`` (non-awaiting variant) and
        bridges the App loop → calling thread via ``threading.Event`` so
        we work without an active Textual worker context. Designed to be
        called from ``asyncio.to_thread`` tool threads.
        """
        if self._on_app_thread():
            # See ``_on_app_thread``: blocking on the loop thread would
            # freeze the App. Degrade to ``None`` (callers map this to their
            # default / cancel outcome) instead of raising.
            _log.warning(
                "TuiUI modal (%s) invoked on the event-loop thread; "
                "dismissing without prompting. Wrap the call site in "
                "asyncio.to_thread.",
                type(modal).__name__,
            )
            return None

        import threading

        done = threading.Event()
        result: dict[str, Any] = {}

        def _on_dismiss(value: Any) -> None:
            result["value"] = value
            done.set()

        # Park the spinner while the modal owns the screen (see
        # _run_inline_choice). Restored in the finally.
        self._suppress_thinking()
        try:
            try:
                self.app.call_from_thread(self.app.push_screen, modal, _on_dismiss)
            except Exception as e:
                raise RuntimeError(
                    f"TuiUI modal dispatch failed: {type(e).__name__}: {e}"
                ) from e

            if not done.wait(timeout=timeout_s):
                raise RuntimeError(
                    f"TuiUI modal timed out after {timeout_s:.0f}s"
                )
            return result.get("value")
        finally:
            self._release_thinking()
