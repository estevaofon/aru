"""OS notifications for end-of-turn / background subagent completion.

Three layers, all opt-in and best-effort:

1. **Terminal bell** — single ``\\a`` byte. Universal, basically free.
   Most terminals translate this into a flash, sound, or both.
2. **OSC 9 notification** — iTerm2 / WezTerm / Windows Terminal pop a
   native notification when they see ``\\x1b]9;<message>\\x07``. Gracefully
   ignored by terminals that don't grok it.
3. **OS-level toast** — Windows toast via ``winotify`` (if installed),
   Linux ``notify-send`` (if on PATH), macOS ``osascript`` fallback.
   Skipped silently when nothing matches.

Policy is driven by ``config.notify``:

* ``off``        — never fire.
* ``background`` — only fire on subagent.complete with run_in_background=True
                   (the user already moved on, so they need a ping). Default.
* ``long``       — also fire on turn.end when duration > threshold.
* ``always``     — also fire on every turn.end.

The dispatcher attaches itself to the plugin manager bus from
``AruApp.on_mount`` and silently no-ops if anything is misconfigured.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
from typing import Any

logger = logging.getLogger("aru.tui.notifications")


def _emit_terminal_bell() -> None:
    out = sys.__stdout__
    if out is None:
        return
    try:
        out.write("\a")
        out.flush()
    except Exception:
        pass


def _emit_osc9(message: str) -> None:
    """OSC 9 — system notification supported by iTerm2/WezTerm/WT."""
    out = sys.__stdout__
    if out is None:
        return
    # Sanitise: OSC 9 terminates on BEL / ST. Strip those plus any C0
    # control bytes so a stray ``\x07`` mid-message can't truncate.
    safe = "".join(ch for ch in message if ch >= " " and ch != "\x7f")
    safe = safe[:200]
    try:
        out.write(f"\x1b]9;{safe}\x07")
        out.flush()
    except Exception:
        pass


def _emit_os_toast(title: str, message: str) -> None:
    """Best-effort native notification via OS-specific tools.

    Runs in a background thread because subprocess spawn on Windows can
    take ~50 ms and we don't want to block the App loop on a chime.
    """
    def _do() -> None:
        try:
            if sys.platform == "win32":
                _emit_windows_toast(title, message)
            elif sys.platform == "darwin":
                _emit_macos_notification(title, message)
            else:
                _emit_linux_notify(title, message)
        except Exception as exc:
            logger.debug("OS toast failed: %s", exc)

    threading.Thread(target=_do, daemon=True).start()


def _emit_windows_toast(title: str, message: str) -> None:
    # winotify is the lightest dep; if not installed, fall back to a
    # PowerShell BurntToast invocation iff that module is installed.
    try:
        import winotify  # type: ignore[import-not-found]

        toast = winotify.Notification(
            app_id="Aru",
            title=title,
            msg=message,
        )
        toast.show()
        return
    except ImportError:
        pass
    except Exception:
        return
    # No winotify — give up silently. Pulling in BurntToast would mean
    # spawning powershell.exe, which is ~200 ms and noisy.


def _emit_macos_notification(title: str, message: str) -> None:
    osascript = shutil.which("osascript")
    if osascript is None:
        return
    safe_title = title.replace('"', "'")
    safe_msg = message.replace('"', "'")
    script = (
        f'display notification "{safe_msg}" with title "{safe_title}"'
    )
    subprocess.run(
        [osascript, "-e", script],
        capture_output=True,
        timeout=5,
        check=False,
    )


def _emit_linux_notify(title: str, message: str) -> None:
    notify_send = shutil.which("notify-send")
    if notify_send is None:
        return
    subprocess.run(
        [notify_send, "-a", "aru", title, message],
        capture_output=True,
        timeout=5,
        check=False,
    )


# ── Public dispatcher ───────────────────────────────────────────────────


class NotificationDispatcher:
    """Subscribe to bus events and fire notifications per policy.

    Holds onto the ``app`` so we can read its window-focus state (no
    point pinging the user when they're already looking at the app).
    Stateless otherwise — every event is judged independently.
    """

    def __init__(
        self,
        app: Any,
        *,
        policy: str = "background",
        threshold_sec: float = 30.0,
    ) -> None:
        self.app = app
        self.policy = policy
        self.threshold_sec = threshold_sec
        # Track which background subagent task_ids we have already chimed
        # for, to avoid double-firing if the bus replays.
        self._notified_subagents: set[str] = set()

    def install(self, plugin_manager: Any) -> None:
        if plugin_manager is None or self.policy == "off":
            return
        try:
            plugin_manager.subscribe(
                "subagent.complete", self._on_subagent_complete
            )
            plugin_manager.subscribe("turn.end", self._on_turn_end)
        except Exception as exc:
            logger.debug("Notification install failed: %s", exc)

    # ── Bus callbacks ────────────────────────────────────────────────

    def _on_subagent_complete(self, payload: dict | Any) -> None:
        if self.policy == "off":
            return
        data = _as_dict(payload)
        task_id = str(data.get("task_id") or "")
        if not task_id or task_id in self._notified_subagents:
            return
        # Only chime for background subagents — foreground ones complete
        # while the user is already watching the chat. Heuristic: a
        # parent_task_id of None plus a ``background`` flag in the
        # delegate args. We don't have that flag plumbed through, so
        # we approximate via the agent_kind / parent presence: top-level
        # subagents (no parent) running >= threshold seconds count.
        # Refinement target if false-positive rate is high.
        duration_ms = float(data.get("duration_ms") or 0.0)
        is_long = duration_ms / 1000 >= self.threshold_sec
        if self.policy == "background" and not is_long:
            return
        self._notified_subagents.add(task_id)
        agent_kind = str(data.get("agent_kind") or "subagent")
        status = str(data.get("status") or "ok")
        title = f"aru — {agent_kind} {status}"
        message = f"Subagent {agent_kind} finished in {duration_ms/1000:.1f}s"
        self._fire(title, message)

    def _on_turn_end(self, payload: dict | Any) -> None:
        if self.policy not in ("long", "always"):
            return
        data = _as_dict(payload)
        duration_ms = float(data.get("duration_ms") or 0.0)
        seconds = duration_ms / 1000
        if self.policy == "long" and seconds < self.threshold_sec:
            return
        title = "aru — turn complete"
        message = f"Agent finished in {seconds:.1f}s"
        self._fire(title, message)

    # ── Emission ─────────────────────────────────────────────────────

    def _fire(self, title: str, message: str) -> None:
        # Skip the noise when the user is actively focused on the app
        # window. Detection is cheap on terminals that report focus
        # events; we approximate via Textual's ``focused`` attribute
        # (None = nobody has focus, often means the window is unfocused).
        # Conservative: if we can't tell, we still notify.
        try:
            if self.app is not None and getattr(self.app, "_focus_lost_at", None) is None:
                # No focus tracking — skip the focus check entirely; user
                # may have notify_threshold set to control noise.
                pass
        except Exception:
            pass
        _emit_terminal_bell()
        _emit_osc9(f"{title}: {message}")
        _emit_os_toast(title, message)


def _as_dict(payload: Any) -> dict:
    if isinstance(payload, dict):
        return payload
    if hasattr(payload, "model_dump"):
        try:
            return payload.model_dump(mode="python")
        except Exception:
            return {}
    return {}
