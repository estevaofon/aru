"""Loop-saturation tracer for Ctrl+C-during-streaming investigation.

Activate with ``ARU_DEBUG_LOOP=1``. When off, every entry point is a
single ``if not _ENABLED: return`` so production cost is one branch.

Output: ``~/.aru/loop-trace.log`` — append-only CSV with one event per
line, columns ``timestamp_ms,thread,event,detail``. Suitable for
``awk`` / pandas / spreadsheet import without parsing.

Six instrumentation points (see ``docs/aru/2026-04-30-ctrlc-streaming-plan.md``
Fase 1):

  A. ``loop_tick`` / ``loop_blocked`` — heartbeat scheduled on the
     main asyncio loop at 20 Hz. Gap > 200 ms yields a ``loop_blocked``
     entry — direct evidence the loop was unable to drain a callback
     for that long, regardless of why.

  B. ``driver.process_message`` — every message the Textual input
     thread parses, before it's posted to the App pump. Confirms the
     input thread is alive and saw the keystroke. Logged from the
     ``textual-input`` thread.

  C. ``app._post_message`` — every message the App pump dequeues.
     Confirms the ``run_coroutine_threadsafe`` callback that B
     scheduled actually drained on the loop. Logged from the loop
     thread.

  D. ``action_ctrl_c`` — entry of the App's Ctrl+C handler. Confirms
     binding dispatch reached our action.

  E. ``stream.event_burst`` — sampled every N events inside the
     ``async for event in agent.arun(...)`` loop. Detects rajadas of
     events arriving without a yield between them.

  F. ``finalize_render`` — duration of the synchronous full-buffer
     markdown re-parse on the loop thread.

Also exposed: ``trace(event, detail)`` so ad-hoc probes can be added
during diagnosis without recompiling.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

# ── Activation gate ──────────────────────────────────────────────────

_ENABLED: bool = bool(os.environ.get("ARU_DEBUG_LOOP"))


def is_enabled() -> bool:
    return _ENABLED


# ── File handle (lazy, line-buffered) ────────────────────────────────

_TRACE_PATH: str = os.path.expanduser("~/.aru/loop-trace.log")
_LOCK = threading.Lock()
_FILE: Any = None
_T0: float = time.monotonic()


def _now_ms() -> int:
    return int((time.monotonic() - _T0) * 1000)


def trace(event: str, detail: str = "") -> None:
    """Append one event to the trace log. No-op when disabled.

    Format: ``ts_ms,thread,event,detail\\n``. Detail is allowed to
    contain commas — analysis tools should split on the first three
    commas only.
    """
    if not _ENABLED:
        return
    try:
        ts_ms = _now_ms()
        thread = threading.current_thread().name
        line = f"{ts_ms},{thread},{event},{detail}\n"
        with _LOCK:
            global _FILE
            if _FILE is None:
                os.makedirs(os.path.dirname(_TRACE_PATH), exist_ok=True)
                # Line-buffered so a hard kill still leaves a usable
                # trace on disk. ``buffering=1`` is line-buffered for
                # text mode in Python.
                _FILE = open(_TRACE_PATH, "a", encoding="utf-8", buffering=1)
                _FILE.write(
                    f"# === session start === pid={os.getpid()} "
                    f"t0_monotonic={_T0:.3f}\n"
                )
            _FILE.write(line)
    except Exception:
        # Never let the tracer crash the app.
        pass


# ── (B/C) Textual monkey-patches ─────────────────────────────────────

_patches_installed = False


def install_textual_patches() -> None:
    """Patch ``Driver.process_message`` and ``App._post_message``.

    Idempotent — only patches once per process. Patches the *class*,
    so it covers all driver/app instances created later. Safe to call
    multiple times.

    The patched functions log to the tracer then delegate to the
    original. They never raise — a buggy tracer must not break the
    app.
    """
    global _patches_installed
    if not _ENABLED or _patches_installed:
        return
    try:
        from textual.driver import Driver
        from textual.app import App
    except Exception:
        return

    try:
        _orig_pm = Driver.process_message

        def _patched_pm(self, message):
            try:
                trace(
                    "driver.process_message",
                    f"type={type(message).__name__} "
                    f"key={getattr(message, 'key', '-')}",
                )
            except Exception:
                pass
            return _orig_pm(self, message)

        Driver.process_message = _patched_pm
    except Exception:
        pass

    try:
        _orig_post = App._post_message

        async def _patched_post(self, message):
            try:
                trace(
                    "app._post_message",
                    f"type={type(message).__name__} "
                    f"key={getattr(message, 'key', '-')}",
                )
            except Exception:
                pass
            return await _orig_post(self, message)

        App._post_message = _patched_post
    except Exception:
        pass

    # WriterThread.stop instrumentation — investigates the Ctrl+Q
    # "summary appears but terminal does not release" symptom. ``stop()``
    # internally does ``put(None) + join()``; if the queue has thousands
    # of pending writes, ``join()`` blocks until ConPTY drains them all,
    # which is exactly the wedge shape the user reports.
    try:
        from textual.drivers._writer_thread import WriterThread
        _orig_stop = WriterThread.stop

        def _patched_stop(self):
            qsize = self._queue.qsize() if hasattr(self._queue, "qsize") else -1
            trace("writer_thread.stop", f"begin qsize={qsize}")
            try:
                return _orig_stop(self)
            finally:
                trace("writer_thread.stop", "end")

        WriterThread.stop = _patched_stop
    except Exception:
        pass

    # Sniff every WriterThread.write call for escapes that change the
    # terminal's mode (alt-screen, mouse, bracketed paste, etc.). The
    # "TUI invadida pelo terminal" symptom is consistent with one of
    # these escapes leaking from a non-Textual source while mouse
    # tracking remains enabled. Logging the *issuer* lets us pin which
    # site sent ``\x1b[?1049l`` (leave alt-screen) at runtime.
    #
    # The sniff is pattern-based — only a handful of escapes are
    # logged, so noise is bounded even at high write rates.
    try:
        import re as _re
        _MODE_RE = _re.compile(
            r"\x1b\[\?(1049|1000|1003|1006|1015|1004|2004|25)([hl])"
        )
        _orig_write = WriterThread.write

        def _patched_write(self, text):
            try:
                if isinstance(text, str):
                    for m in _MODE_RE.finditer(text):
                        trace(
                            "term_mode_escape",
                            f"mode={m.group(1)} action={m.group(2)} "
                            f"sample={text[max(0, m.start()-8):m.end()+8]!r}",
                        )
            except Exception:
                pass
            return _orig_write(self, text)

        WriterThread.write = _patched_write
    except Exception:
        pass

    _patches_installed = True


# ── (A) Heartbeat ────────────────────────────────────────────────────

_heartbeat_state: dict[str, Any] = {"last": 0.0, "running": False}


def start_heartbeat(loop) -> None:
    """Begin a 20 Hz heartbeat on *loop*.

    Each tick measures the wall-clock gap since the previous tick. A
    healthy loop ticks every ~50 ms (the call_later interval); a
    saturated loop ticks late, and the gap measures exactly how long
    the loop was unable to run a callback.

    Gap > 200 ms emits ``loop_blocked``; otherwise ``loop_tick``. The
    heartbeat keeps itself alive via recursive ``call_later`` and
    stops when ``stop_heartbeat`` flips the running flag.
    """
    if not _ENABLED:
        return
    _heartbeat_state["last"] = time.monotonic()
    _heartbeat_state["running"] = True

    def _tick() -> None:
        if not _heartbeat_state["running"]:
            return
        now = time.monotonic()
        gap_ms = (now - _heartbeat_state["last"]) * 1000
        _heartbeat_state["last"] = now
        if gap_ms > 200:
            trace("loop_blocked", f"gap_ms={gap_ms:.0f}")
        else:
            trace("loop_tick", f"gap_ms={gap_ms:.0f}")
        try:
            loop.call_later(0.05, _tick)
        except Exception:
            _heartbeat_state["running"] = False

    try:
        loop.call_later(0.05, _tick)
    except Exception:
        _heartbeat_state["running"] = False


def stop_heartbeat() -> None:
    _heartbeat_state["running"] = False


# ── (E) Stream hot-loop sampler ──────────────────────────────────────

class StreamSampler:
    """Sampler used inside ``streaming.run_stream``'s ``async for``.

    Counts events and emits ``stream.event_burst`` every ``every`` ticks
    with the wall-clock duration since the previous emission. A burst
    of 16 events in <5 ms means the loop processed 16 events back-to-back
    with no IO yield — direct evidence of hot-loop saturation.

    Used as a context-light counter (one int + one float per call); the
    log emission is gated by ``every`` so the trace stays readable.
    """

    __slots__ = ("_n", "_t0", "_every")

    def __init__(self, every: int = 16) -> None:
        self._n = 0
        self._t0 = time.monotonic()
        self._every = every

    def tick(self, event_kind: str = "") -> None:
        if not _ENABLED:
            return
        self._n += 1
        if self._n % self._every == 0:
            now = time.monotonic()
            dt_ms = (now - self._t0) * 1000
            trace(
                "stream.event_burst",
                f"n={self._n} dt_ms={dt_ms:.0f} kind={event_kind}",
            )
            self._t0 = now


# ── (F) finalize_render timer (helper) ───────────────────────────────

class TimedSection:
    """Context manager that logs the duration of a sync block.

    Used in ``finalize_render`` and any other place we suspect of
    blocking the loop synchronously. Emits ``<event> dt_ms=...`` on
    exit even if the block raised, so the duration is recorded for
    both success and failure paths.
    """

    __slots__ = ("_event", "_detail", "_t0")

    def __init__(self, event: str, detail: str = "") -> None:
        self._event = event
        self._detail = detail
        self._t0 = 0.0

    def __enter__(self) -> "TimedSection":
        if _ENABLED:
            self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not _ENABLED:
            return
        dt_ms = (time.monotonic() - self._t0) * 1000
        suffix = f" exc={exc_type.__name__}" if exc_type is not None else ""
        trace(self._event, f"{self._detail} dt_ms={dt_ms:.0f}{suffix}")
