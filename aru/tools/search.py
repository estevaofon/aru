"""Glob and grep search tools with ripgrep fast path + pure-Python fallback."""

from __future__ import annotations

import asyncio
import fnmatch
import functools
import json
import os
import shutil

from aru.tools.gitignore import walk_filtered
from aru.tools._shared import _truncate_output


_rg_path_cached: str | None = None
_rg_path_resolved = False


def _rg_path() -> str | None:
    """Return absolute path to the `rg` binary, or None if unavailable."""
    global _rg_path_cached, _rg_path_resolved
    if not _rg_path_resolved:
        _rg_path_cached = shutil.which("rg")
        _rg_path_resolved = True
    return _rg_path_cached


async def _run_rg(args: list[str], cwd: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run ripgrep with *args* asynchronously. Returns (code, stdout, stderr)."""
    rg = _rg_path()
    if not rg:
        return (-1, "", "rg not available")
    try:
        proc = await asyncio.create_subprocess_exec(
            rg,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return (-1, "", "rg timed out")
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    except FileNotFoundError:
        return (-1, "", "rg not available")
    except Exception as e:
        return (-1, "", f"rg error: {e}")


async def _glob_search_rg(pattern: str, directory: str = ".") -> str | None:
    """ripgrep-backed glob. Returns None to signal fallback."""
    if not _rg_path():
        return None

    rg_pattern = pattern
    args = ["--files", "-g", rg_pattern, directory]
    code, stdout, stderr = await _run_rg(args, cwd=directory)
    if code not in (0, 1):
        return None

    matches = [line for line in stdout.splitlines() if line]
    rels = []
    for m in matches:
        rel = os.path.relpath(os.path.abspath(os.path.join(directory, m)), directory)
        rels.append(rel)

    if not rels:
        return f"No files matched pattern: {pattern}"

    rels.sort()
    if len(rels) > 100:
        return "\n".join(rels[:100]) + f"\n... and {len(rels) - 100} more matches (use a more specific pattern to narrow results)"
    return "\n".join(rels)


def _glob_search_python(pattern: str, directory: str = ".") -> str:
    """Pure-Python glob fallback used when ripgrep is unavailable."""
    matches = []
    for root, dirs, files in walk_filtered(directory):
        for filename in files:
            filepath = os.path.join(root, filename)
            rel_path = os.path.relpath(filepath, directory)
            rel_posix = rel_path.replace('\\', '/')
            matched = fnmatch.fnmatch(rel_posix, pattern)
            if not matched and pattern.startswith('**/'):
                matched = fnmatch.fnmatch(filename, pattern[3:])
            if not matched:
                matched = fnmatch.fnmatch(filename, pattern)
            if matched:
                matches.append(rel_path)

    if not matches:
        return f"No files matched pattern: {pattern}"

    matches.sort()
    if len(matches) > 100:
        return "\n".join(matches[:100]) + f"\n... and {len(matches) - 100} more matches (use a more specific pattern to narrow results)"
    return "\n".join(matches)


def glob_search(pattern: str, directory: str = ".") -> str:
    """Find files matching a glob pattern recursively.

    Args:
        pattern: Glob pattern to match (e.g. '**/*.py', 'src/**/*.ts').
        directory: Directory to search in. Defaults to current directory.
    """
    return _glob_search_python(pattern, directory)


def _grep_search_python(pattern: str, directory: str = ".", file_glob: str = "", context_lines: int = 10) -> str:
    """Pure-Python grep fallback used when ripgrep is unavailable."""
    import re

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    results = []
    match_count = 0
    files_with_matches: dict[str, list[int]] = {}
    MAX_MATCHES = 20 if context_lines > 0 else 50
    stopped_early = False

    for root, dirs, files in walk_filtered(directory):
        for filename in files:
            if file_glob and not fnmatch.fnmatch(filename, file_glob):
                continue
            filepath = os.path.join(root, filename)
            rel_path = os.path.relpath(filepath, directory)
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()

                if context_lines > 0:
                    match_indices = [i for i, line in enumerate(lines) if regex.search(line)]
                    if not match_indices:
                        continue

                    files_with_matches[rel_path] = [i + 1 for i in match_indices]

                    if match_count < MAX_MATCHES:
                        shown: set[int] = set()
                        blocks = []
                        current_block: list[str] = []
                        for mi in match_indices:
                            start = max(0, mi - context_lines)
                            end = min(len(lines), mi + context_lines + 1)
                            for li in range(start, end):
                                if li not in shown:
                                    if current_block and li > max(shown) + 1:
                                        blocks.append(current_block)
                                        current_block = []
                                    shown.add(li)
                                    marker = ">" if li == mi else " "
                                    current_block.append(f"{rel_path}:{li + 1}:{marker} {lines[li].rstrip()}")
                        if current_block:
                            blocks.append(current_block)

                        for block in blocks:
                            results.extend(block)
                            results.append("---")
                    match_count += len(match_indices)
                else:
                    for i, line in enumerate(lines, 1):
                        if regex.search(line):
                            results.append(f"{rel_path}:{i}: {line.rstrip()}")
                            match_count += 1
                            if rel_path not in files_with_matches:
                                files_with_matches[rel_path] = []
                            files_with_matches[rel_path].append(i)

            except (OSError, PermissionError):
                continue

        if match_count >= MAX_MATCHES:
            stopped_early = True
            break

    if not results:
        return f"No matches found for pattern: {pattern}"

    if results and results[-1] == "---":
        results.pop()

    if match_count > MAX_MATCHES and context_lines == 0:
        output = "\n".join(results[:MAX_MATCHES])
    else:
        output = "\n".join(results)

    if len(files_with_matches) > 1 or stopped_early:
        summary_lines = ["\n[Match summary]"]
        for fpath, line_nums in files_with_matches.items():
            nums = ", ".join(str(n) for n in line_nums[:10])
            extra = f" +{len(line_nums) - 10} more" if len(line_nums) > 10 else ""
            summary_lines.append(f"  {fpath}: lines {nums}{extra}")
        if stopped_early:
            summary_lines.append(f"  ... search stopped at {match_count} matches. Use file_glob or a more specific pattern.")
        output += "\n".join(summary_lines)

    return _truncate_output(output, source_tool="grep")


async def _grep_search_rg(
    pattern: str,
    directory: str = ".",
    file_glob: str = "",
    context_lines: int = 10,
) -> str | None:
    """ripgrep-backed grep. Returns None when rg can't be used so callers can fall back."""
    if not _rg_path():
        return None

    MAX_MATCHES = 20 if context_lines > 0 else 50

    args = [
        "--json",
        "--no-messages",
        "-e",
        pattern,
    ]
    if context_lines > 0:
        args.extend(["--context", str(context_lines)])
    if file_glob:
        args.extend(["--glob", file_glob])
    args.extend(["--max-count", str(MAX_MATCHES * 5)])
    args.append(directory)

    code, stdout, _stderr = await _run_rg(args, cwd=directory)
    if code not in (0, 1):
        return None
    if not stdout:
        return f"No matches found for pattern: {pattern}"

    per_file: dict[str, list[tuple[int, str, bool]]] = {}
    match_count_total = 0
    for raw in stdout.splitlines():
        if not raw:
            continue
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        if etype not in ("match", "context"):
            continue
        data = evt.get("data") or {}
        path_info = data.get("path") or {}
        rel = path_info.get("text")
        if not rel:
            continue
        rel = os.path.relpath(os.path.abspath(os.path.join(directory, rel)), directory)
        lines_info = data.get("lines") or {}
        text = lines_info.get("text") or ""
        line_no = data.get("line_number")
        if line_no is None:
            continue
        is_match = etype == "match"
        per_file.setdefault(rel, []).append((line_no, text.rstrip("\n"), is_match))
        if is_match:
            match_count_total += 1

    if not per_file:
        return f"No matches found for pattern: {pattern}"

    files_with_matches: dict[str, list[int]] = {
        rel: [ln for ln, _t, is_m in entries if is_m]
        for rel, entries in per_file.items()
    }

    results: list[str] = []
    emitted_matches = 0
    stopped_early = False

    for rel, entries in per_file.items():
        entries.sort(key=lambda e: e[0])

        if context_lines > 0:
            block: list[str] = []
            last_line: int | None = None
            for ln, text, is_m in entries:
                if last_line is not None and ln > last_line + 1:
                    results.extend(block)
                    results.append("---")
                    block = []
                marker = ">" if is_m else " "
                block.append(f"{rel}:{ln}:{marker} {text}")
                last_line = ln
                if is_m:
                    emitted_matches += 1
            if block:
                results.extend(block)
                results.append("---")
        else:
            for ln, text, is_m in entries:
                if not is_m:
                    continue
                results.append(f"{rel}:{ln}: {text}")
                emitted_matches += 1

        if emitted_matches >= MAX_MATCHES:
            stopped_early = True
            break

    if results and results[-1] == "---":
        results.pop()

    if not results:
        return f"No matches found for pattern: {pattern}"

    output = "\n".join(results)

    if len(files_with_matches) > 1 or stopped_early:
        summary_lines = ["\n[Match summary]"]
        for fpath, line_nums in files_with_matches.items():
            nums = ", ".join(str(n) for n in line_nums[:10])
            extra = f" +{len(line_nums) - 10} more" if len(line_nums) > 10 else ""
            summary_lines.append(f"  {fpath}: lines {nums}{extra}")
        if stopped_early:
            summary_lines.append(
                f"  ... search stopped at {emitted_matches} matches. Use file_glob or a more specific pattern."
            )
        output += "\n".join(summary_lines)

    return _truncate_output(output, source_tool="grep")


def grep_search(pattern: str, directory: str = ".", file_glob: str = "", context_lines: int = 10) -> str:
    """Search for a regex pattern in file contents.

    Args:
        pattern: Regular expression pattern to search for.
        directory: Directory to search in. Defaults to current directory.
        file_glob: Optional glob to filter which files to search (e.g. '*.py').
        context_lines: Lines of context before and after each match (like grep -C). Default 10.
            Use 0 for file-level matches only. Use 30+ for full function bodies.
    """
    return _grep_search_python(pattern, directory, file_glob, context_lines)


@functools.wraps(glob_search)
async def _glob_search_tool(pattern: str, directory: str = ".") -> str:
    rg_result = await _glob_search_rg(pattern, directory)
    if rg_result is not None:
        return rg_result
    return await asyncio.to_thread(_glob_search_python, pattern, directory)


@functools.wraps(grep_search)
async def _grep_search_tool(
    pattern: str,
    directory: str = ".",
    file_glob: str = "",
    context_lines: int = 10,
) -> str:
    rg_result = await _grep_search_rg(pattern, directory, file_glob, context_lines)
    if rg_result is not None:
        return rg_result
    return await asyncio.to_thread(
        _grep_search_python, pattern, directory, file_glob, context_lines
    )
