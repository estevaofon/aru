"""Layer 12 / 14 — terminal-state recovery: off-then-on shake + keypress trigger.

Background: ``aru/tui/widgets/chat.py`` post-mortem traces the evolution.

Layer 12 (2026-04-25) introduced an off-then-on shake of the four mouse
DEC private modes via ``driver.write`` to defeat ConPTY's enable cache
and bypass the driver's ``_mouse`` gate. That helped some sessions but
the user reported (``fix/scroll-analysis3``) that mouse-only shake did
not recover the wheel after Windows display sleep / wake — the wake
appears to corrupt more than just mouse, and on the input side
``ENABLE_VIRTUAL_TERMINAL_INPUT`` may also drop, so no stdout escape
recovers wheel events.

Layer 14 (2026-04-25, after Ctrl+R proved the heavy shake works)
promoted ``_reenable_mouse_tracking`` from mouse-only to:
* re-assert ``ENABLE_VIRTUAL_TERMINAL_INPUT`` on stdin (Windows), and
* shake the full DEC private-mode set from
  ``WindowsDriver.start_application_mode`` (mouse + focus-events +
  bracketed-paste).

These tests pin the observable contracts of the promoted method:
* the twelve DEC private-mode sequences are emitted in disable→enable
  order with one flush;
* headless/no-driver path is a quiet no-op;
* the keypress trigger calls into ``_reenable_mouse_tracking`` debounced
  by ``_KEYPRESS_REARM_DEBOUNCE`` (the trigger itself is broken in
  practice — ``Input._on_key`` consumes printable keys before
  ``App.on_key`` sees them — but the debounce contract is what this
  test pins).
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


@pytest.mark.asyncio
async def test_reenable_mouse_tracking_emits_off_then_on_shake():
    """Layer 14: full DEC mode set, off→on order, single flush.

    The off→on shake forces ConPTY's enable-cache through a state
    transition, defeating the case where its cache claims a mode is
    already ``h`` and suppresses the propagated write. Order matters —
    if the on sequences came first the cache could no-op them.

    Layer 12 covered four mouse modes only; Layer 14 widens the shake
    to mouse + focus-events (``?1004``) + bracketed-paste (``?2004``) —
    the full set ``WindowsDriver.start_application_mode`` enables at
    boot. User confirmation on 2026-04-25 was that the mouse-only shake
    did not recover after Windows display sleep/wake but the full shake
    via Ctrl+R did, which is the signal that justified the promotion.
    """
    from aru.tui.app import AruApp

    app = AruApp()
    rec = _RecordingDriver()
    # ``_driver`` is a private slot of ``App`` — assigning directly
    # short-circuits the application-mode startup that would normally
    # set it. The recovery method only reads ``self._driver``.
    app._driver = rec

    app._reenable_mouse_tracking()

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
    # One flush at the end — the ``WriterThread`` bufferises everything
    # before that into a single terminal emit.
    assert rec.flushes == 1


@pytest.mark.asyncio
async def test_reenable_mouse_tracking_no_driver_is_noop():
    """If the driver is ``None`` (headless / pre-mount) the call is a quiet no-op."""
    from aru.tui.app import AruApp

    app = AruApp()
    app._driver = None
    # Must not raise.
    app._reenable_mouse_tracking()


@pytest.mark.asyncio
async def test_keypress_rearm_is_debounced(monkeypatch):
    """Layer 12 keypress trigger respects ``_KEYPRESS_REARM_DEBOUNCE``.

    Two keystrokes within the debounce window should produce exactly one
    ``_reenable_mouse_tracking`` invocation; a third keystroke after the
    window elapses should produce a second.

    The trigger itself is broken in production (``Input._on_key``
    consumes printable keys before ``App.on_key`` sees them), but this
    test pins the debounce *primitive* contract — useful if a future
    Layer wires the rearm to ``on_input_changed`` (which bubbles via
    Message dispatch and isn't absorbed by Input).
    """
    from aru.tui import app as app_mod
    from aru.tui.app import AruApp

    app = AruApp()
    rec = _RecordingDriver()
    app._driver = rec

    fake_now = [100.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr(app_mod.time, "monotonic", fake_monotonic)

    # 1st keystroke at t=100 — fires.
    app._maybe_rearm_mouse_on_keypress()
    # 2nd keystroke 100 ms later — within 500 ms debounce → suppressed.
    fake_now[0] += 0.1
    app._maybe_rearm_mouse_on_keypress()
    # 3rd keystroke 600 ms after 1st (i.e. 500 ms after debounce window
    # opened) — fires.
    fake_now[0] += 0.5
    app._maybe_rearm_mouse_on_keypress()

    # Each fired call emits 12 sequences (Layer 14 full mode set: 6 off
    # + 6 on). Two fires = 24.
    assert len(rec.writes) == 24
    assert rec.flushes == 2
