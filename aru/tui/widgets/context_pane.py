"""ContextPane — live context-window breakdown (top of sidebar).

Inspired by the OpenCode sidebar: model, usage progress (used / max),
and a per-call breakdown (input / output / cache_read / cache_write).
Updates by re-reading the ``session`` each time ``refresh_from_session``
is called (typically after ``turn.end`` and after /cost / /compact).
"""

from __future__ import annotations

from typing import Any

from rich.progress_bar import ProgressBar
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, Static


# Approximate per-provider context window sizes (tokens). Used purely
# for the progress bar — if the model isn't recognised the bar scales
# against a conservative default.
_CONTEXT_WINDOWS: dict[str, int] = {
    "anthropic/claude-sonnet-4-5": 200_000,
    "anthropic/claude-haiku-4-5": 200_000,
    "anthropic/claude-opus-4-7": 200_000,
    "openai/gpt-4o": 128_000,
    "openai/gpt-4o-mini": 128_000,
    "openai/gpt-5": 272_000,
    "ollama/llama3.1": 128_000,
    "alibabacloud/qwen3.6-plus": 131_072,
    "groq/llama-3.1-8b-instant": 131_072,
    "deepseek/deepseek-chat": 64_000,
}
_DEFAULT_WINDOW = 128_000


def _guess_window(model_ref: str) -> int:
    """Best-effort context-window lookup by prefix match."""
    if not model_ref:
        return _DEFAULT_WINDOW
    mr = model_ref.lower()
    for key, size in _CONTEXT_WINDOWS.items():
        if mr.startswith(key.lower()):
            return size
    return _DEFAULT_WINDOW


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:,}"


class ContextPane(Vertical):
    """Top half of the sidebar: model, window usage, per-call breakdown."""

    DEFAULT_CSS = """
    ContextPane {
        background: $surface;
        border-left: solid $primary;
        border-bottom: solid $primary;
        padding: 1 1 0 1;
        height: auto;
    }
    #ctx-title {
        color: $accent;
        text-style: bold;
        padding-bottom: 1;
    }
    #ctx-body {
        height: auto;
        color: $text;
    }
    #ctx-bar {
        height: 1;
        color: $secondary;
        padding: 0 0 1 0;
    }
    """

    def __init__(self, session: Any = None) -> None:
        super().__init__()
        self._session = session

    def compose(self) -> ComposeResult:
        yield Label("Context Window", id="ctx-title")
        yield Static("", id="ctx-bar")
        yield Static("", id="ctx-body")

    def on_mount(self) -> None:
        self.refresh_from_session()

    def refresh_from_session(self) -> None:
        session = self._session
        if session is None:
            return
        model_ref = (
            getattr(session, "model_ref", None)
            or getattr(session, "model_id", None)
            or ""
        )
        window = _guess_window(model_ref)
        last_in = int(getattr(session, "last_input_tokens", 0) or 0)
        last_out = int(getattr(session, "last_output_tokens", 0) or 0)
        last_cache_read = int(getattr(session, "last_cache_read", 0) or 0)
        last_cache_write = int(getattr(session, "last_cache_write", 0) or 0)
        last_total = last_in + last_out + last_cache_read + last_cache_write
        pct = min(100, int(last_total / window * 100)) if window else 0
        try:
            cost = float(getattr(session, "estimated_cost", 0.0) or 0.0)
        except Exception:
            cost = 0.0

        # Header line (progress bar row).
        bar_text = Text()
        bar_text.append(f"{_fmt(last_total)} ", style="bold yellow")
        bar_text.append(f"/ {_fmt(window)} ", style="dim")
        bar_text.append(f"({pct}%)", style="bold cyan" if pct < 80 else "bold red")
        try:
            self.query_one("#ctx-bar", Static).update(bar_text)
        except Exception:
            pass

        # Body — per-call breakdown.
        lines = Text()
        lines.append("Model: ", style="dim")
        lines.append(f"{model_ref}\n", style="white")
        lines.append("Cost:  ", style="dim")
        lines.append(f"${cost:.4f}\n", style="green")
        if last_total > 0:
            lines.append("\n")
            lines.append("Last context window: ", style="bold dim")
            lines.append(f"{last_total:,}\n", style="bold yellow")
            lines.append(f"  input:       {last_in:,}\n", style="cyan")
            lines.append(f"  output:      {last_out:,}\n", style="cyan")
            if last_cache_read:
                lines.append(
                    f"  cache_read:  {last_cache_read:,}\n", style="magenta"
                )
            if last_cache_write:
                lines.append(
                    f"  cache_write: {last_cache_write:,}\n", style="magenta"
                )
        else:
            lines.append("\n", style="dim")
            lines.append("Waiting for first turn…", style="italic dim")
        try:
            self.query_one("#ctx-body", Static).update(lines)
        except Exception:
            pass

    # ── Bus callbacks ────────────────────────────────────────────────

    def update_from_turn(self, _payload: dict) -> None:
        self.refresh_from_session()

    def update_from_metrics(self, _payload: dict) -> None:
        """Intra-turn refresh — fires after each internal LLM API call.

        Keeps the "Last context window" breakdown and the progress bar
        honest during long implementation phases, instead of waiting for
        the whole turn to finish.
        """
        self.refresh_from_session()

    def update_from_model_change(self, new_ref: str) -> None:
        # Session already has the new ref; just re-render.
        self.refresh_from_session()
