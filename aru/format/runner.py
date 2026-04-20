"""Subprocess helper for formatter invocation.

Runs a formatter binary with the file content piped on stdin and captures
stdout. Non-zero exit, stderr-only output, or empty stdout all count as
"formatter declined to format" — the caller preserves the original content.

Timeout is applied via ``asyncio.wait_for`` so a wedged formatter never
freezes the REPL.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("aru.format")


async def run_formatter(
    command: list[str],
    content: str,
    *,
    timeout: float = 30.0,
) -> str | None:
    """Pipe *content* through *command* on stdin; return stdout or None.

    Returns ``None`` when the formatter should be treated as unavailable or
    produced no usable output. The caller is expected to leave the file
    content untouched in that case.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        logger.warning("formatter binary not found: %s (%s)", command[0], exc)
        return None
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("formatter spawn failed %s: %s", command[0], exc)
        return None

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=content.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("formatter %s timed out after %ss", command[0], timeout)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None

    if proc.returncode != 0:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        logger.debug(
            "formatter %s exited %s: %s",
            command[0], proc.returncode, stderr_text[:200],
        )
        return None

    output = stdout_bytes.decode("utf-8", errors="replace")
    if not output:
        logger.debug("formatter %s produced empty output", command[0])
        return None
    return output
