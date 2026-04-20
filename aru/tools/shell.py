"""Shell execution tool with background process tracking and long-running detection."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text

from aru.permissions import check_permission, consume_rejection_feedback
from aru.runtime import get_ctx
from aru.tools._shared import _notify_file_mutation, _truncate_output


BACKGROUND_PATTERNS = (
    "uvicorn", "gunicorn", "flask run", "django", "manage.py runserver",
    "npm start", "npm run dev", "npx ", "next dev", "next start",
    "vite", "webpack serve", "ng serve",
    "node server", "nodemon",
    "docker compose up", "docker-compose up",
    "celery worker", "celery beat",
    "redis-server", "mongod", "postgres",
    "streamlit run", "gradio",
    "http-server", "live-server", "serve ",
)


def _register_process(process):
    """Track a background process for cleanup on exit."""
    from aru.runtime import append_tracked_process
    append_tracked_process(process)


def cleanup_processes(processes: list | None = None):
    """Kill all tracked background processes on exit.

    Args:
        processes: Explicit list to clean up. If None, snapshots the
            RuntimeContext tracked-processes list under a lock so a
            concurrent ``_register_process`` call cannot cause
            ``RuntimeError: list changed size during iteration``.
    """
    if processes is not None:
        procs = list(processes)
    else:
        from aru.runtime import snapshot_tracked_processes
        procs = snapshot_tracked_processes()
    for proc in procs:
        still_running = proc.poll() is None if hasattr(proc, "poll") else proc.returncode is None
        if still_running:
            _kill_process_tree(proc)


def _kill_process_tree(process):
    """Kill a process and all its children. On Windows, process.kill() only
    kills the shell wrapper — child processes (e.g. npm → node) keep running.
    Use taskkill /T to kill the entire tree."""
    pid = process.pid
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _is_long_running(command: str) -> bool:
    """Detect commands that start servers or long-running processes."""
    cmd = command.strip()
    if cmd.endswith("&"):
        return True
    return any(pattern in cmd for pattern in BACKGROUND_PATTERNS)


async def _fire_plugin_hook(event_name: str, data: dict) -> dict:
    """Fire a plugin hook if the plugin manager is available. Returns the (mutated) data."""
    try:
        ctx = get_ctx()
        mgr = ctx.plugin_manager
        if mgr is not None and mgr.loaded:
            event = await mgr.fire(event_name, data)
            return event.data
    except (LookupError, AttributeError):
        pass
    return data


async def run_command(command: str, timeout: int = 60, working_directory: str = "", extra_env: dict | None = None) -> str:
    """Execute a shell command and return output (async, non-blocking).

    Args:
        command: The command to execute.
        timeout: Max seconds. Default 60.
        working_directory: Directory to run in. Default: ctx.cwd.
        extra_env: Extra environment variables to inject (from plugins).
    """
    # Tier 3 #2: default to ctx.cwd so sub-agents in different worktrees
    # see isolated working directories. Explicit working_directory still
    # wins (agent passed it deliberately).
    if working_directory:
        cwd = working_directory
    else:
        from aru.runtime import get_cwd as _get_cwd
        cwd = _get_cwd()

    env = None
    if extra_env and isinstance(extra_env, dict) and any(extra_env.values()):
        env = {**os.environ, **{k: str(v) for k, v in extra_env.items() if v is not None}}

    if _is_long_running(command):
        startup_seconds = 5
        try:
            bg_kwargs: dict = dict(
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
            if env:
                bg_kwargs["env"] = env
            if sys.platform != "win32":
                bg_kwargs["start_new_session"] = True
            process = await asyncio.create_subprocess_shell(command, **bg_kwargs)

            lines: list[str] = []
            try:
                async with asyncio.timeout(startup_seconds):
                    while True:
                        line = await process.stdout.readline()
                        if not line:
                            break
                        lines.append(line.decode("utf-8", errors="replace").rstrip())
            except TimeoutError:
                pass

            exit_code = process.returncode
            output = "\n".join(lines) if lines else "(no output yet)"

            if exit_code is not None:
                return f"Process exited immediately (code {exit_code}):\n{output}"

            _register_process(process)

            return (
                f"Process running in background (PID {process.pid}).\n"
                f"Initial output ({startup_seconds}s):\n{output}"
            )
        except Exception as e:
            return f"Error starting background process: {e}"

    try:
        create_kwargs: dict = dict(
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        if env:
            create_kwargs["env"] = env
        if sys.platform == "win32":
            create_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            create_kwargs["start_new_session"] = True

        process = await asyncio.create_subprocess_shell(command, **create_kwargs)
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            _kill_process_tree(process)
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=5
                )
            except (asyncio.TimeoutError, Exception):
                stdout_bytes, stderr_bytes = b"", b""
            partial = (
                (stdout_bytes or b"") + (stderr_bytes or b"")
            ).decode("utf-8", errors="replace").strip()
            msg = f"Error: Command timed out after {timeout} seconds."
            if partial:
                tail = "\n".join(partial.splitlines()[-20:])
                msg += f"\nLast output:\n{tail}"
            msg += "\nHint: if this is a server/long-running process, it will be detected and run in background automatically."
            return msg

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

        parts = []
        if stdout:
            parts.append(_truncate_output(stdout, source_tool="bash"))
        if stderr:
            parts.append(f"STDERR:\n{_truncate_output(stderr, source_tool='bash')}")
        if process.returncode != 0:
            parts.append(f"Exit code: {process.returncode}")

        return "\n".join(parts).strip() or "(no output)"
    except Exception as e:
        return f"Error running command: {e}"


async def bash(command: str, timeout: int = 60, working_directory: str = "") -> str:
    """Execute a shell command (tests, git, install, build, etc).

    Args:
        command: The command to execute.
        timeout: Max seconds to wait. Default 60.
        working_directory: Directory to run in. Default: ctx.cwd (worktree-aware).
    """
    if working_directory:
        cwd = working_directory
    else:
        from aru.runtime import get_cwd as _get_cwd
        cwd = _get_cwd()
    cmd_display = Group(
        Syntax(command, "bash", theme="monokai"),
        Text(f"cwd: {cwd}", style="dim"),
    )
    if not check_permission("bash", command, cmd_display):
        feedback = consume_rejection_feedback()
        if feedback:
            return (
                f"PERMISSION DENIED by user: {command}. The user said: {feedback}\n"
                f"Follow the user's instructions instead of retrying."
            )
        return f"PERMISSION DENIED by user: {command}. Do NOT retry this operation. Stop and ask the user for new instructions."

    hook_data = await _fire_plugin_hook("shell.env", {"cwd": cwd, "command": command, "env": {}})
    if isinstance(hook_data, dict):
        command = hook_data.get("command", command)
        shell_env = hook_data.get("env") or None
    else:
        shell_env = None

    result = await run_command(command, timeout=timeout, working_directory=working_directory, extra_env=shell_env)
    _notify_file_mutation()
    return result
