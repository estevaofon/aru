"""Doom-loop detection: catch agents stuck repeating identical tool calls.

A *doom-loop* is the agent invoking the same tool with the same arguments
N times in a row, with no intervening different call. The model has lost
the plot — typically because a tool kept failing and the model forgot it
already tried that exact thing — and without intervention the run will
just burn budget until the context window fills.

This module gives the runner a cheap, deterministic detector. The
heuristic mirrors OpenCode's ``session/processor.ts:188-211``:

* keep a sliding window of the last N (tool_name, sorted-args) pairs;
* when the window is full and every entry equals the latest one, fire.

When detection fires the runner pauses, asks the user "continue or
abort?", and either resets the buffer for that tool (allowing the model
a fresh chance) or aborts the run.

Threshold is **3** by default — same as OpenCode. Override per-process
via the ``ARU_DOOM_LOOP_THRESHOLD`` env var (must be ≥ 2; values below
fall back to the default to avoid pathologically eager prompts).

Args equality uses ``json.dumps(..., sort_keys=True)`` so two calls with
the same logical args but differing key order — ``{"a": 1, "b": 2}`` vs
``{"b": 2, "a": 1}`` — are correctly treated as identical. ``default=str``
keeps non-JSON values (e.g. Path) from raising; the resulting string is
still a stable signature for equality.
"""

from __future__ import annotations

import json
import os
from collections import deque
from typing import Any


DEFAULT_THRESHOLD = 3
_ENV_VAR = "ARU_DOOM_LOOP_THRESHOLD"


def _stable_signature(tool_name: str, tool_args: Any) -> tuple[str, str]:
    """Return a hashable equality signature for a tool invocation.

    The args portion is a JSON dump with ``sort_keys=True`` so two calls
    that differ only by key order in the args dict are treated as equal.
    Non-JSON values (Paths, datetimes, etc.) are stringified so the dump
    never raises — the goal is a stable signature, not a round-trippable
    payload.
    """
    if isinstance(tool_args, dict):
        try:
            args_repr = json.dumps(tool_args, sort_keys=True, default=str)
        except Exception:
            # json.dumps can still fail on truly exotic values (e.g.
            # circular refs). Fallback to repr — less stable but never
            # raises, and the detector tolerates occasional mismatches.
            args_repr = repr(sorted(tool_args.items()))
    elif tool_args is None:
        args_repr = "null"
    else:
        # Non-dict args (rare — Agno usually wraps in a dict) — just str.
        args_repr = str(tool_args)
    return (tool_name or "", args_repr)


def threshold_from_env() -> int:
    """Read ``ARU_DOOM_LOOP_THRESHOLD`` or return the default.

    Values < 2 fall back to the default — a threshold of 1 would fire on
    the very first call which is meaningless, and a threshold of 0 makes
    the deque() unbounded. Invalid values (non-int) also fall back.
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is None:
        return DEFAULT_THRESHOLD
    try:
        v = int(raw)
    except ValueError:
        return DEFAULT_THRESHOLD
    if v < 2:
        return DEFAULT_THRESHOLD
    return v


class DoomLoopDetector:
    """Sliding-window detector for repeated identical tool calls.

    Each ``record(tool_name, tool_args)`` call appends a signature to the
    window and returns ``True`` iff the window is now full **and** every
    entry in it is identical (i.e. the last N calls were the exact same
    tool with the exact same args).

    The detector is stateless beyond its window — there is no notion of
    sessions or scopes. The runner instantiates one detector per turn of
    the primary agent loop; sub-agents that run their own arun loop
    (delegate.py) get their own detector via the same wiring.
    """

    def __init__(self, threshold: int | None = None) -> None:
        self.threshold: int = threshold if threshold is not None else threshold_from_env()
        self._recent: deque[tuple[str, str]] = deque(maxlen=self.threshold)

    def record(self, tool_name: str, tool_args: Any) -> bool:
        """Append a call's signature; return True if a doom-loop is now detected."""
        sig = _stable_signature(tool_name, tool_args)
        self._recent.append(sig)
        if len(self._recent) < self.threshold:
            return False
        first = self._recent[0]
        return all(s == first for s in self._recent)

    def reset(self) -> None:
        """Forget all recorded calls. Used after manual intervention."""
        self._recent.clear()

    def reset_for_tool(self, tool_name: str) -> None:
        """Drop every entry whose tool_name equals *tool_name*.

        Called after the user chooses *continue* on a doom-loop prompt:
        the buffer is wiped for that specific tool so the very next call
        doesn't immediately re-trigger the same prompt. Other tools'
        history (which were not part of the loop) is preserved so a
        secondary loop on a different tool still detects.
        """
        kept = [s for s in self._recent if s[0] != tool_name]
        self._recent = deque(kept, maxlen=self.threshold)

    def __len__(self) -> int:  # convenience for tests
        return len(self._recent)


__all__ = [
    "DEFAULT_THRESHOLD",
    "DoomLoopDetector",
    "threshold_from_env",
]
