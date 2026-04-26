"""Layer 13 — user-invoked terminal recovery binding (Ctrl+R).

Background: ``aru/tui/widgets/chat.py`` post-mortem under "no self-heal
of Layers 9/10/12 actually recovers" (2026-04-25). User report on
``fix/scroll-analysis3``: after a Windows display sleep/wake the wheel
dies and *no* autonomous recovery brings it back. Layer 13 adds a
user-invoked binding (Ctrl+R) that does a full DEC mode shake +
``ENABLE_VIRTUAL_TERMINAL_INPUT`` re-assert. User confirmed the same
day that Ctrl+R does recover the wheel where Layers 9/10/12 don't.

Layer 14 (same day, after Ctrl+R proved the heavy shake works)
promoted the recovery sequence into ``_reenable_mouse_tracking`` so
every existing caller (Layer 9 turn boundary, Layer 10 periodic tick,
Layer 12 keypress) gets the proven recovery. ``action_recover_terminal``
now delegates the byte-level shake to ``_reenable_mouse_tracking`` and
adds two extras unique to the manual path: ``self.refresh()`` and a
visible chat message.

These tests pin the Layer 13 contract:
* the binding is registered to the action with ``priority=True``;
* invoking the action triggers the same byte sequence
  ``_reenable_mouse_tracking`` produces (Layer 14 contract);
* the action is a quiet no-op when the driver is unavailable.

The Windows-only ``set_console_mode`` step is exercised at runtime
only — wrapped in try/except and gated on ``sys.platform``, so
unit-testing requires ctypes mocking that adds little value over the
integration test (manual TUI run on Windows).
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")


class _RecordingDriver:
    """Driver stub that records every ``write`` call."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.flushes: int = 0

    def write(self, data: str) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        self.flushes += 1


def test_recover_terminal_binding_present():
    """Layer 13: ``Ctrl+R`` is bound and dispatches ``recover_terminal``.

    The binding must use ``priority=True`` so the action fires even
    when a focused widget would otherwise consume the key — that is
    the whole point vs Layer 12's broken ``on_key`` path.
    """
    from aru.tui.app import AruApp

    matches = [b for b in AruApp.BINDINGS if getattr(b, "key", None) == "ctrl+r"]
    assert matches, "ctrl+r must be in AruApp.BINDINGS"
    binding = matches[0]
    assert binding.action == "recover_terminal"
    assert binding.priority is True


@pytest.mark.asyncio
async def test_action_recover_terminal_emits_full_mode_shake():
    """Layer 13/14: action emits the full DEC mode shake via delegation.

    Post-Layer-14, ``action_recover_terminal`` delegates the byte-level
    shake to ``_reenable_mouse_tracking`` so the manual (Ctrl+R) path
    and the autonomous paths (Layer 9 turn boundary, Layer 10 periodic
    tick) all run identical recovery sequences. This test verifies
    that the action does in fact produce the 12-escape full-set shake
    end-to-end, regardless of how the implementation is factored.
    """
    from aru.tui.app import AruApp

    app = AruApp()
    rec = _RecordingDriver()
    app._driver = rec

    app.action_recover_terminal()

    expected_off = [
        "\x1b[?1000l",
        "\x1b[?1003l",
        "\x1b[?1015l",
        "\x1b[?1006l",
        "\x1b[?1004l",
        "\x1b[?2004l",
    ]
    expected_on = [
        "\x1b[?1000h",
        "\x1b[?1003h",
        "\x1b[?1015h",
        "\x1b[?1006h",
        "\x1b[?1004h",
        "\x1b[?2004h",
    ]
    assert rec.writes == expected_off + expected_on
    assert rec.flushes == 1


@pytest.mark.asyncio
async def test_action_recover_terminal_no_driver_is_noop():
    """Headless / pre-mount: action must not raise when driver is None."""
    from aru.tui.app import AruApp

    app = AruApp()
    app._driver = None
    # Must not raise — ChatPane query, refresh(), and console-mode
    # branch all wrapped in try/except for exactly this case.
    app.action_recover_terminal()
