"""Persistent user preferences for aru — currently the recent-models list.

Mirrors OpenCode's ``~/.local/state/opencode/model.json`` recent-list idea
so that, after running ``/connect`` (or ``/model``) once, the next launch
of aru picks up where you left off without having to hand-edit
``aru.json``. Storage lives in ``~/.aru/state.json`` (separate from
``auth.json`` so credentials and preferences are not co-mingled).

Schema::

    {
      "recent_models": ["openai/gpt-5.4", "anthropic/claude-opus-4-7"]
      // MRU first, capped at MAX_RECENT entries
    }

Consumption:

* ``get_last_model()`` returns the most-recent entry whose provider is
  still in the registry — TUI/one-shot bootstrap calls this before
  falling back to ``config.default_model`` (which itself falls back to
  ``aru.session.DEFAULT_MODEL``).
* ``record_model(ref)`` moves ``ref`` to the front of the list, dedups,
  caps at ``MAX_RECENT``, and persists. Called every time the user
  switches model — either through ``/model`` or after a ``/connect``
  picks a model.

All disk operations are best-effort: a missing or malformed file just
gives an empty list back so startup is never blocked by a corrupted
state file.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("aru.state")

MAX_RECENT = 10


def state_path() -> Path:
    """Absolute path to the state file (``~/.aru/state.json``)."""
    return Path.home() / ".aru" / "state.json"


def load_state() -> dict[str, Any]:
    """Return the raw state dict, or ``{}`` when missing/garbled."""
    path = state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_state(data: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def get_recent_models() -> list[str]:
    """Return the MRU model list (most recent first)."""
    data = load_state()
    recent = data.get("recent_models")
    if not isinstance(recent, list):
        return []
    return [r for r in recent if isinstance(r, str) and r]


def get_last_model() -> str | None:
    """Return the most-recent model ref whose provider is still registered.

    Walks the MRU list and returns the first ``provider/model`` whose
    ``provider`` is present in the in-memory provider registry. Returns
    ``None`` when the list is empty or every entry refers to a vanished
    provider — callers should then fall back to their built-in default.
    """
    try:
        from aru.providers import get_provider
    except Exception:  # pragma: no cover — providers import should always work
        return None

    for ref in get_recent_models():
        provider_key = ref.split("/", 1)[0] if "/" in ref else ref
        if get_provider(provider_key) is not None:
            return ref
    return None


def record_model(ref: str) -> None:
    """Move ``ref`` to the front of the recent list and persist.

    Dedupes by exact string equality (provider+model), caps the list at
    :data:`MAX_RECENT`. Empty / non-string inputs are silently ignored
    so callers don't need to guard.
    """
    if not isinstance(ref, str) or not ref.strip():
        return
    ref = ref.strip()
    data = load_state()
    existing = data.get("recent_models")
    if not isinstance(existing, list):
        existing = []
    # Filter out any prior occurrence (case-sensitive — provider/model is
    # canonical and we don't want "OpenAI/..." to be considered a dup of
    # "openai/...").
    deduped = [r for r in existing if isinstance(r, str) and r and r != ref]
    new_list = [ref, *deduped][:MAX_RECENT]
    data["recent_models"] = new_list
    try:
        _write_state(data)
    except OSError as exc:
        logger.warning("Failed to write %s: %s", state_path(), exc)


def forget_model(ref: str) -> None:
    """Drop ``ref`` from the recent list (used by ``/connect logout``)."""
    data = load_state()
    existing = data.get("recent_models")
    if not isinstance(existing, list):
        return
    pruned = [r for r in existing if isinstance(r, str) and r and r != ref]
    if pruned == existing:
        return
    data["recent_models"] = pruned
    try:
        _write_state(data)
    except OSError as exc:
        logger.warning("Failed to write %s: %s", state_path(), exc)


def forget_provider(provider_key: str) -> None:
    """Drop every recent entry for ``provider_key`` (``/connect logout`` use).

    When the user signs out of a provider, any cached MRU pointing at it
    becomes stale — leaving them in would resurrect the now-broken
    credential on the next launch. Match prefix-style on ``provider/``.
    """
    if not provider_key:
        return
    data = load_state()
    existing = data.get("recent_models")
    if not isinstance(existing, list):
        return
    prefix = f"{provider_key}/"
    pruned = [
        r
        for r in existing
        if isinstance(r, str) and r and not r.startswith(prefix) and r != provider_key
    ]
    if pruned == existing:
        return
    data["recent_models"] = pruned
    try:
        _write_state(data)
    except OSError as exc:
        logger.warning("Failed to write %s: %s", state_path(), exc)


__all__ = [
    "MAX_RECENT",
    "forget_model",
    "forget_provider",
    "get_last_model",
    "get_recent_models",
    "load_state",
    "record_model",
    "state_path",
]
