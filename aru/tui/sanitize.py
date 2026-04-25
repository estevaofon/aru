"""Terminal-output hygiene helpers used across the TUI.

Layer 7 / Layer 9 / Layer 10 of the scroll-freeze post-mortem (see
``aru/tui/widgets/chat.py`` for the full history) all hinge on the same
invariant: **no string content originating from the agent, a tool, or
arbitrary file content may reach the terminal with raw C0 control bytes
intact.** A single stray ``\\x1b[?1000l`` switches X10 mouse reporting off
on Windows ConPTY (and most modern emulators), at which point the wheel
stops working in every scroll surface in the app simultaneously.

The chat pane already protects everything that flows through
``ChatMessageWidget.buffer`` and the ``add_renderable`` path, but modal
screens (``ChoiceModal`` / ``ConfirmModal`` / ``TextInputModal``)
historically built their ``Label`` / ``Static`` content from raw
agent-provided strings — including the diff preview shown when an edit
needs approval. Files containing escape bytes (colored scripts, captured
terminal output, accidentally-saved binaries) flowed straight through to
the terminal. Hence Layer 10: lift these helpers out of ``chat.py`` and
apply them at every modal composition point too.
"""

from __future__ import annotations

from typing import Any


# C0 controls (0x00-0x1F) and DEL (0x7F) are dropped on the way to the
# terminal, EXCEPT ``\n`` and ``\t`` which carry semantic meaning to
# markdown-it, Rich, and Textual layout. Implementation note:
# ``str.translate`` is implemented in C and runs in microseconds for
# multi-KB inputs — applying it at every boundary is essentially free.
_CTRL_CHAR_TRANSLATION: dict[int, None] = {
    c: None for c in range(32) if chr(c) not in ("\n", "\t")
}
_CTRL_CHAR_TRANSLATION[0x7F] = None


def sanitize_for_terminal(raw: str) -> str:
    """Remove non-printable C0 controls so rogue ANSI escapes can't reach the tty.

    Keeps ``\\n`` and ``\\t``; drops everything else in the C0 range plus DEL.
    Apply this at every boundary where externally-sourced text becomes a
    Rich renderable / Textual widget content.
    """
    return raw.translate(_CTRL_CHAR_TRANSLATION)


class SanitizedRenderable:
    """Wraps a Rich renderable so its output segments are stripped of C0 bytes.

    ``sanitize_for_terminal`` covers every plain string we render. Arbitrary
    Rich renderables (panels, diff previews, plan summaries, the startup
    logo) skip that path and would mount as ``Static(renderable)`` whose
    segments hit the compositor unmodified — including any rogue escapes
    embedded in the inner text.

    This wrapper closes that gap at the segment level: ``console.render``
    yields segments from the inner renderable, we strip C0 bytes from any
    segment whose ``.text`` contains them, and re-emit the cleaned stream.
    Rich's ``Segment`` is a ``NamedTuple`` so ``seg._replace(text=...)`` is
    a cheap immutable swap. Unchanged segments are re-emitted unmodified —
    the hot path is a single ``str.translate`` per segment which typically
    no-ops.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __rich_console__(self, console: Any, options: Any) -> Any:
        for seg in console.render(self._inner, options):
            if seg.text:
                clean = sanitize_for_terminal(seg.text)
                if clean != seg.text:
                    yield seg._replace(text=clean)
                    continue
            yield seg
