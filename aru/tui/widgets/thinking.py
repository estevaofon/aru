"""ThinkingIndicator — rotating 'pensando' phrase + spinner while busy."""

from __future__ import annotations

import random

from rich.spinner import Spinner
from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget


# Same pool used by the REPL StatusBar (aru/display.py::THINKING_PHRASES)
# so the mood is consistent across modes. Kept local to avoid importing
# the Rich ``StatusBar`` class into the TUI loop.
THINKING_PHRASES = [
    "Pondering",
    "Reasoning",
    "Thinking",
    "Analyzing",
    "Exploring",
    "Computing",
    "Cogitating",
    "Deliberating",
    "Investigating",
    "Synthesizing",
    "Processing",
    "Reflecting",
    "Working",
    "Planning",
    "Considering",
]


class ThinkingIndicator(Widget):
    """A one-line rotating phrase + spinner, hidden when idle."""

    DEFAULT_CSS = """
    ThinkingIndicator {
        display: none;
        height: 1;
        color: $text-muted;
        padding: 0 1;
        /* Breathing room lives on ChatPane's bottom padding now — when
           the indicator appears it should sit flush with that gap, not
           stack a second one on top of it. */
    }
    ThinkingIndicator.-busy {
        display: block;
    }
    """

    ROTATE_SECONDS: float = 3.0
    TICK_SECONDS: float = 0.1

    busy: reactive[bool] = reactive(False, layout=True)
    # Incremented while a blocking prompt (permission / confirm / text input)
    # owns the screen. The spinner means "a turn is in flight", but while we
    # are parked waiting on the user there is nothing to animate — showing it
    # there reads as "still processing" and is misleading. The indicator is
    # hidden whenever suppress_depth > 0, regardless of ``busy``. A depth
    # counter (not a bool) keeps back-to-back prompts (e.g. "No" → feedback
    # box) suppressed throughout, without a flash between them.
    suppress_depth: reactive[int] = reactive(0, layout=True)

    def __init__(self) -> None:
        super().__init__()
        self._phrases = list(THINKING_PHRASES)
        random.shuffle(self._phrases)
        self._index = 0
        self._spinner = Spinner("dots", text="", style="cyan")
        self._ticks_since_rotate = 0

    def on_mount(self) -> None:
        self.set_interval(self.TICK_SECONDS, self._tick)

    def _visible(self) -> bool:
        return self.busy and self.suppress_depth == 0

    def _apply_visibility(self) -> None:
        if self._visible():
            self.add_class("-busy")
        else:
            self.remove_class("-busy")

    def watch_busy(self, _old: bool, new: bool) -> None:
        if new:
            self._index = 0
            self._ticks_since_rotate = 0
        self._apply_visibility()

    def watch_suppress_depth(self, _old: int, _new: int) -> None:
        self._apply_visibility()

    def suppress(self) -> None:
        """Hide the spinner while a blocking prompt owns the screen."""
        self.suppress_depth += 1

    def release(self) -> None:
        """Undo one ``suppress()``; spinner returns only if the turn is still busy."""
        if self.suppress_depth > 0:
            self.suppress_depth -= 1

    def _tick(self) -> None:
        if not self._visible():
            return
        self._ticks_since_rotate += 1
        if self._ticks_since_rotate * self.TICK_SECONDS >= self.ROTATE_SECONDS:
            self._ticks_since_rotate = 0
            self._index = (self._index + 1) % len(self._phrases)
        self.refresh()

    def render(self) -> Text:
        # Build spinner frame + phrase inline.
        phrase = self._phrases[self._index % len(self._phrases)]
        try:
            frame = self._spinner.render(self.app.ellapsed_time if False else 0.0)
        except Exception:
            frame = Text("·", style="cyan")
        # Simple deterministic frame since Rich's Spinner.render() needs time
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        spinner_frame = frames[self._ticks_since_rotate % len(frames)]
        out = Text()
        out.append(f"{spinner_frame} ", style="bold cyan")
        out.append(f"{phrase}…", style="italic dim")
        return out
