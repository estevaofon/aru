"""Analyze ``~/.aru/loop-trace.log`` and answer the decision tree from
``docs/aru/2026-04-30-ctrlc-streaming-plan.md`` Fase 3.

Usage::

    python -m aru._debug.analyze_trace [path]

Default path: ``~/.aru/loop-trace.log``. Pass an explicit path to
analyse a different file (e.g. one shipped from another machine).

The analyser is intentionally simple — `awk`-style line parsing,
bucketed counters, and a fixed set of questions. Any pattern more
complex than what this script captures should be added as a new
section here, not a separate ad-hoc script.

Output sections:

  STATISTICS  — summary of every event kind that appeared, with
                count and (where relevant) max duration.
  HOTSPOTS    — top-10 ``loop_blocked`` entries sorted by gap.
  CTRL_C      — for each Ctrl+C key press detected at
                ``driver.process_message``, the latency to
                ``app._post_message`` and ``action_ctrl_c``.
  VERDICT     — direct readout of the decision tree (P1/P2/P3 or
                continue-investigating).

Designed to be idempotent and zero-side-effect — never writes back to
the log.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Event:
    ts_ms: int
    thread: str
    name: str
    detail: str

    @classmethod
    def parse(cls, line: str) -> "Event | None":
        if line.startswith("#") or not line.strip():
            return None
        parts = line.rstrip("\n").split(",", 3)
        if len(parts) < 3:
            return None
        try:
            ts = int(parts[0])
        except ValueError:
            return None
        thread = parts[1]
        name = parts[2]
        detail = parts[3] if len(parts) > 3 else ""
        return cls(ts_ms=ts, thread=thread, name=name, detail=detail)


def _parse_kv(detail: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in detail.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            out[k] = v
    return out


def _detail_int(detail: str, key: str) -> int | None:
    kv = _parse_kv(detail)
    if key not in kv:
        return None
    try:
        return int(float(kv[key]))
    except ValueError:
        return None


@dataclass
class Stats:
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    max_gap_ms_loop: int = 0
    loop_blocked_top: list[Event] = field(default_factory=list)
    finalize_render_max_ms: int = 0
    finalize_render_calls: int = 0
    finalize_render_total_ms: int = 0
    stream_bursts: list[Event] = field(default_factory=list)
    ctrl_c_press: list[Event] = field(default_factory=list)
    post_messages_ctrl_c: list[Event] = field(default_factory=list)
    action_ctrl_c: list[Event] = field(default_factory=list)


def _is_ctrl_c(detail: str) -> bool:
    kv = _parse_kv(detail)
    return kv.get("key") == "ctrl+c"


def collect(events: list[Event]) -> Stats:
    s = Stats()
    for e in events:
        s.counts[e.name] += 1
        if e.name == "loop_blocked":
            gap = _detail_int(e.detail, "gap_ms") or 0
            s.max_gap_ms_loop = max(s.max_gap_ms_loop, gap)
            s.loop_blocked_top.append(e)
        elif e.name == "loop_tick":
            gap = _detail_int(e.detail, "gap_ms") or 0
            s.max_gap_ms_loop = max(s.max_gap_ms_loop, gap)
        elif e.name == "finalize_render":
            dt = _detail_int(e.detail, "dt_ms") or 0
            s.finalize_render_calls += 1
            s.finalize_render_total_ms += dt
            s.finalize_render_max_ms = max(s.finalize_render_max_ms, dt)
        elif e.name == "stream.event_burst":
            s.stream_bursts.append(e)
        elif e.name == "driver.process_message":
            if _is_ctrl_c(e.detail):
                s.ctrl_c_press.append(e)
        elif e.name == "app._post_message":
            if _is_ctrl_c(e.detail):
                s.post_messages_ctrl_c.append(e)
        elif e.name == "action_ctrl_c":
            s.action_ctrl_c.append(e)

    s.loop_blocked_top.sort(
        key=lambda e: _detail_int(e.detail, "gap_ms") or 0, reverse=True
    )
    s.loop_blocked_top = s.loop_blocked_top[:10]
    return s


def _correlate_ctrl_c(s: Stats) -> list[dict]:
    """Match each ``driver.process_message ctrl+c`` to the next
    ``app._post_message ctrl+c`` and ``action_ctrl_c`` after it.

    Returns one dict per press with latencies in ms.
    """
    out: list[dict] = []
    for press in s.ctrl_c_press:
        next_post = next(
            (p for p in s.post_messages_ctrl_c if p.ts_ms >= press.ts_ms),
            None,
        )
        next_action = next(
            (a for a in s.action_ctrl_c if a.ts_ms >= press.ts_ms),
            None,
        )
        out.append(
            {
                "press_ms": press.ts_ms,
                "thread_seen": press.thread,
                "post_lag_ms": (
                    next_post.ts_ms - press.ts_ms if next_post else None
                ),
                "action_lag_ms": (
                    next_action.ts_ms - press.ts_ms if next_action else None
                ),
            }
        )
    return out


def _verdict(s: Stats, presses: list[dict]) -> list[str]:
    out: list[str] = []
    if not presses:
        out.append("No Ctrl+C key events recorded.")
        out.append("-> Either the user did not press Ctrl+C during this trace,")
        out.append("  or the Textual input thread (P1) is wedged before")
        out.append("  ``Driver.process_message`` is reached. If the user did")
        out.append("  press Ctrl+C: P1 — investigate EventMonitor / ConIn read.")
        return out

    for i, p in enumerate(presses, 1):
        out.append(f"Press #{i} at ts={p['press_ms']}ms (thread={p['thread_seen']}):")
        post = p["post_lag_ms"]
        action = p["action_lag_ms"]

        if post is None:
            out.append(
                "  P2/P3 — driver saw the key, but ``app._post_message`` "
                "never fired."
            )
            out.append(
                "  -> loop saturated for the rest of the trace. Look at "
                "loop_blocked HOTSPOTS at this timestamp."
            )
            continue

        out.append(f"  press -> app._post_message: {post}ms")
        if post > 500:
            out.append(
                "  P2 — loop took >500ms to drain the posted callback. "
                "Saturation is the dominant cause."
            )
        elif post > 50:
            out.append(
                "  Borderline — pump latency >50ms; check loop_blocked "
                "near this timestamp."
            )
        else:
            out.append("  pump latency healthy.")

        if action is None:
            out.append(
                "  P3 — pump received but action_ctrl_c never dispatched. "
                "Check Screen.dispatch."
            )
            continue
        out.append(f"  press -> action_ctrl_c:    {action}ms")
        if action > 500:
            out.append(
                "  P3 — pump dispatch is the bottleneck (likely behind a "
                "queue of expensive events)."
            )
        elif action - (post or 0) > 100:
            out.append(
                "  Pump->action handoff is slow; suspect heavy event "
                "ahead of Key in the queue."
            )

    return out


def _suggest_fix(s: Stats) -> list[str]:
    out: list[str] = []
    if s.finalize_render_max_ms > 200:
        out.append(
            f"finalize_render max {s.finalize_render_max_ms}ms across "
            f"{s.finalize_render_calls} calls "
            f"(total {s.finalize_render_total_ms}ms)."
        )
        out.append(
            "-> C3 candidate: move finalize_render off-thread "
            "(asyncio.to_thread). One-file change in chat.py."
        )

    fast_bursts = [
        b
        for b in s.stream_bursts
        if (_detail_int(b.detail, "dt_ms") or 999) < 5
    ]
    if fast_bursts:
        out.append(
            f"{len(fast_bursts)} stream bursts of 16 events in <5ms — "
            f"hot-loop without yield."
        )
        out.append(
            "-> C1 candidate: ``await asyncio.sleep(0)`` every N events "
            "in streaming.py. One-line change."
        )

    if not out:
        out.append(
            "No obvious culprit in C1/C3. New round of instrumentation "
            "needed (Compositor render hooks, paint cost)."
        )
    return out


def main(argv: list[str]) -> int:
    # Windows cp1252 stdout chokes on em-dashes / arrows in our prose
    # — switch to UTF-8 so the analyser can run anywhere without
    # truncating the report.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    path = argv[1] if len(argv) > 1 else os.path.expanduser("~/.aru/loop-trace.log")
    if not os.path.exists(path):
        print(f"trace file not found: {path}", file=sys.stderr)
        return 2
    with open(path, encoding="utf-8") as fh:
        events = []
        for line in fh:
            ev = Event.parse(line)
            if ev is not None:
                events.append(ev)

    if not events:
        print("trace file has no events", file=sys.stderr)
        return 2

    s = collect(events)
    presses = _correlate_ctrl_c(s)

    print("=" * 72)
    print(f"Trace: {path}")
    print(f"Events: {len(events)}  ({events[0].ts_ms}ms -> {events[-1].ts_ms}ms)")
    print("=" * 72)

    print("\n--- STATISTICS ---")
    for name in sorted(s.counts):
        print(f"  {name:<32} {s.counts[name]}")
    print(f"  max loop gap:                  {s.max_gap_ms_loop}ms")
    print(
        f"  finalize_render: max={s.finalize_render_max_ms}ms  "
        f"total={s.finalize_render_total_ms}ms  "
        f"calls={s.finalize_render_calls}"
    )

    print("\n--- HOTSPOTS (top 10 loop_blocked) ---")
    for e in s.loop_blocked_top:
        print(f"  ts={e.ts_ms:>8}ms  thread={e.thread:<20}  {e.detail}")

    print("\n--- CTRL_C ---")
    for p in presses:
        print(
            f"  ts={p['press_ms']:>8}ms  "
            f"post_lag={p['post_lag_ms']}ms  "
            f"action_lag={p['action_lag_ms']}ms"
        )

    print("\n--- VERDICT ---")
    for line in _verdict(s, presses):
        print(f"  {line}")

    print("\n--- SUGGESTED FIX ---")
    for line in _suggest_fix(s):
        print(f"  {line}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
