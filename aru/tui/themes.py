"""Curated TUI theme presets for Aru.

Textual 8 ships its own theme system (``App.theme = name``,
``App.register_theme(Theme)``, ``App.available_themes``). The Ctrl+P
command palette already exposes every registered theme as a switcher.

This module is a thin wrapper around that API:

* ``apply_theme(app, name)`` resolves an Aru-friendly name (``"dark"``,
  ``"solarized"``, ...) to the corresponding registered Textual theme
  and assigns it to ``app.theme``. That single assignment triggers
  Textual's reactive watcher, which recolours every mounted widget.
* ``THEME_NAMES`` lists the curated names ``/theme`` advertises. We
  intentionally keep this short — the long list lives behind Ctrl+P.

Why we don't roll our own ``Theme`` instances anymore: Textual's
``set_variables`` path bypasses the reactive theme cascade, so
``apply_theme`` *appeared* to work (no exception) but no widget
repainted. Going through ``app.theme = ...`` is the only reliable
mechanism in 8.x.
"""

from __future__ import annotations

from typing import Any


# Aru-friendly aliases → Textual's registered theme names. Keys are what
# the user types after ``/theme``; values are what ``app.theme`` accepts.
# The mapping lets us keep short, memorable handles even when Textual's
# canonical name is verbose (e.g. ``solarized-dark``).
_ALIASES: dict[str, str] = {
    "dark": "textual-dark",
    "light": "textual-light",
    "nord": "nord",
    "gruvbox": "gruvbox",
    "dracula": "dracula",
    "solarized": "solarized-dark",
    "solarized-dark": "solarized-dark",
    "solarized-light": "solarized-light",
    "tokyo-night": "tokyo-night",
    "monokai": "monokai",
    "catppuccin": "catppuccin-mocha",
    "catppuccin-mocha": "catppuccin-mocha",
    "catppuccin-latte": "catppuccin-latte",
    "rose-pine": "rose-pine",
}


# Public for /theme listing. Order is the recommended display order.
THEME_NAMES: tuple[str, ...] = (
    "dark",
    "light",
    "nord",
    "gruvbox",
    "dracula",
    "solarized",
    "tokyo-night",
    "monokai",
    "catppuccin",
    "rose-pine",
)


def resolve_theme(name: str) -> str | None:
    """Return the canonical Textual theme name for an Aru alias.

    Accepts either the short alias (``"dark"``) or the canonical
    Textual name (``"textual-dark"``). Returns ``None`` if neither
    matches.
    """
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in _ALIASES:
        return _ALIASES[key]
    # Allow callers to pass a canonical Textual theme name verbatim —
    # useful for /theme power users who know the long form.
    return key


def apply_theme(app: Any, name: str) -> bool:
    """Switch ``app`` to the named theme. Returns True on success.

    Resolves Aru aliases first, then verifies the result is in
    ``app.available_themes`` before assigning. The reactive watcher
    on ``App.theme`` repaints every screen on assignment.
    """
    canonical = resolve_theme(name)
    if not canonical:
        return False
    try:
        available = getattr(app, "available_themes", {}) or {}
        if canonical not in available:
            return False
        app.theme = canonical
        return True
    except Exception:
        return False
