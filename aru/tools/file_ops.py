"""File read/write/edit/list tools.

Contains sync implementations plus the async ``_thread_tool`` wrappers that
agents actually invoke. Split out of the former monolithic codebase.py.
"""

from __future__ import annotations

import asyncio
import os

from rich.console import Group
from rich.text import Text

from aru.permissions import check_permission
from aru.runtime import get_ctx
from aru.tools.gitignore import is_ignored, walk_filtered
from aru.tools._diff import _compact_diff, _format_unified_diff
from aru.tools._shared import (
    _checkpoint_file,
    _notify_file_mutation,
    _thread_tool,
    _truncate_output,
)


# Hard ceiling per tool result (~7K tokens). Even max_size=0 respects this per chunk.
_READ_HARD_CAP = 40_000  # bytes (~11K tokens)


def clear_read_cache():
    """Clear the read cache. Call after file mutations to avoid stale data."""
    get_ctx().read_cache.clear()


def read_file(file_path: str, start_line: int = 0, end_line: int = 0, max_size: int = 12_000) -> str:
    """Read file contents. Returns chunked output for large files.

    Args:
        file_path: Path to the file (absolute or relative).
        start_line: First line (1-indexed, inclusive). 0 = beginning.
        end_line: Last line (1-indexed, inclusive). 0 = end.
        max_size: Max bytes before truncation. Default 12KB.
            Set to 0 to read the full file in chunks — each chunk up to ~40KB.
            The first chunk includes a continuation hint so you can call again
            with start_line to get the next chunk.
    """
    try:
        resolved = os.path.abspath(file_path)
        cache_key = (resolved, start_line, end_line, max_size)
        _read_cache = get_ctx().read_cache
        if cache_key in _read_cache and (start_line > 0 or end_line > 0):
            lines_info = _read_cache[cache_key]
            return (
                f"[cached] Already read ({lines_info})."
                f" Use the content from your earlier call."
            )

        file_size = os.path.getsize(file_path)

        full_read = max_size == 0
        effective_limit = _READ_HARD_CAP if full_read else max_size

        with open(file_path, "rb") as f:
            sample = f.read(1024)
        if b"\x00" in sample:
            return f"Error: Binary file detected ({file_size} bytes): {file_path}"

        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total_lines = len(lines)

        if start_line > 0 or end_line > 0:
            s = max(start_line, 1) - 1
            e = end_line if end_line > 0 else total_lines
            e = min(e, total_lines)

            selected = lines[s:e]

            accumulated = []
            char_count = 0
            for i, line in enumerate(selected):
                char_count += len(line)
                if char_count > effective_limit:
                    break
                accumulated.append(f"{s + i + 1:4d} | {line}")

            lines_returned = len(accumulated)
            actual_end = s + lines_returned
            header = f"[Lines {s + 1}-{actual_end} of {total_lines}]\n"
            result = header + "".join(accumulated)

            if lines_returned < len(selected):
                next_start = actual_end + 1
                result += (
                    f"\n\n[CHUNK] Returned {lines_returned} of {e - s} requested lines."
                    f" Call read_file(\"{file_path}\", start_line={next_start}, end_line={e})"
                    f" to continue."
                )
            _read_cache[cache_key] = f"{lines_returned} lines returned"
            return result

        if file_size <= effective_limit:
            numbered = [f"{i + 1:4d} | {line}" for i, line in enumerate(lines)]
            output = "".join(numbered)
            result = output if full_read else _truncate_output(output, source_file=file_path)
            _read_cache[cache_key] = f"{total_lines} lines"
            return result

        accumulated = []
        char_count = 0
        for i, line in enumerate(lines):
            char_count += len(line)
            if char_count > effective_limit and accumulated:
                break
            accumulated.append(f"{i + 1:4d} | {line}")
            if char_count > effective_limit:
                break

        lines_shown = len(accumulated)
        first_chunk = "".join(accumulated)

        import re as _re
        toc_entries = []
        toc_pattern = _re.compile(r"^(\s*)(def |class |async def )(\w+)")
        for li in range(lines_shown, total_lines):
            m = toc_pattern.match(lines[li])
            if m:
                indent = len(m.group(1))
                prefix = "  " if indent > 0 else ""
                toc_entries.append(f"{prefix}{m.group(2).strip()} {m.group(3)} (line {li + 1})")

        outline = "\n".join(toc_entries) if toc_entries else "(no more definitions)"
        result = (
            f"{first_chunk}\n\n"
            f"[Showing lines 1-{lines_shown} of {total_lines} ({file_size:,} bytes)]\n\n"
            f"[Remaining definitions]\n{outline}\n\n"
            f"To read more: read_file(\"{file_path}\", start_line={lines_shown + 1}, end_line=N)"
        )
        _read_cache[cache_key] = f"{lines_shown}/{total_lines} lines + outline"
        return result
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"


def write_file(file_path: str, content: str) -> str:
    """Write content to a file, creating parent directories if needed.

    Args:
        file_path: Path to the file to write.
        content: The content to write to the file.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            existing = f.read()
    except (FileNotFoundError, UnicodeDecodeError, OSError):
        existing = ""
    diff = _format_unified_diff(existing, content, file_path)
    if not check_permission("write", file_path, diff):
        return f"PERMISSION DENIED by user: write to {file_path}. Do NOT retry this operation. Stop and ask the user for new instructions."
    try:
        _checkpoint_file(file_path)
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        _notify_file_mutation()
        return f"Successfully wrote to {file_path}"
    except Exception as e:
        return f"Error writing file: {e}"


def write_files(file_list: list[dict]) -> str:
    """Write multiple files at once. Use this instead of multiple write_file calls when creating
    or updating several files that don't depend on each other (e.g. scaffolding a project).

    Each entry in the list must have 'path' and 'content' keys.

    Args:
        file_list: List of dicts with 'path' (file path) and 'content' (file content) keys.
                   Example: [{"path": "src/main.py", "content": "print('hello')"}, {"path": "src/utils.py", "content": "..."}]
    """
    parts = [Text(f"Write {len(file_list)} files:", style="bold"), Text()]
    for e in file_list:
        p = e.get("path", "<missing>")
        content = e.get("content", "")
        try:
            with open(p, "r", encoding="utf-8") as f:
                existing = f.read()
        except (FileNotFoundError, UnicodeDecodeError, OSError):
            existing = ""
        parts.append(_format_unified_diff(existing, content, p))
        parts.append(Text())
    if not check_permission("write", ", ".join(e.get("path", "") for e in file_list), Group(*parts)):
        return f"PERMISSION DENIED by user: batch write of {len(file_list)} files. Do NOT retry this operation. Stop and ask the user for new instructions."

    results = []
    errors = []
    for entry in file_list:
        path = entry.get("path", "")
        content = entry.get("content", "")
        if not path:
            errors.append("Error: missing 'path' in entry")
            continue
        try:
            _checkpoint_file(path)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            results.append(path)
        except Exception as e:
            errors.append(f"Error writing {path}: {e}")

    parts = []
    if results:
        _notify_file_mutation()
        parts.append(f"Successfully wrote {len(results)} files: {', '.join(results)}")
    if errors:
        parts.append("\n".join(errors))
    return "\n".join(parts) or "No files to write."


def edit_file(file_path: str, old_string: str, new_string: str) -> str:
    """Replace an exact string in a file. The old_string must appear exactly once.

    Args:
        file_path: Path to the file to edit.
        old_string: The exact text to find and replace. Must be unique in the file.
        new_string: The replacement text.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"

    try:
        count = content.count(old_string)
        if count == 0:
            import difflib
            lines = content.splitlines(keepends=True)
            old_lines = old_string.splitlines(keepends=True)
            matcher = difflib.SequenceMatcher(None, lines, old_lines)
            best = matcher.find_longest_match(0, len(lines), 0, len(old_lines))
            if best.size > 0:
                ctx_start = max(0, best.a - 2)
                ctx_end = min(len(lines), best.a + best.size + 2)
                snippet = "".join(f"{ctx_start + i + 1:4d} | {lines[ctx_start + i]}" for i in range(ctx_end - ctx_start))
                return f"Error: old_string not found in {file_path}. Closest match region:\n{snippet}"
            else:
                snippet = "".join(f"{i + 1:4d} | {l}" for i, l in enumerate(lines[:20]))
                return f"Error: old_string not found in {file_path}. File starts with:\n{snippet}"
        if count > 1:
            return f"Error: old_string found {count} times in {file_path}. Must be unique."

        new_content = content.replace(old_string, new_string, 1)
    except Exception as e:
        return f"Error editing file: {e}"

    diff = _format_unified_diff(content, new_content, file_path)
    if not check_permission("edit", file_path, diff):
        return f"PERMISSION DENIED by user: edit {file_path}. Do NOT retry this operation. Stop and ask the user for new instructions."

    try:
        _checkpoint_file(file_path)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        _notify_file_mutation()

        diff_text = _compact_diff(old_string, new_string, file_path)
        if diff_text:
            return f"Edited {file_path}\n{diff_text}"
        return f"Edited {file_path}"
    except Exception as e:
        return f"Error writing file: {e}"


def edit_files(edits: list[dict]) -> str:
    """Apply multiple find-and-replace edits across files in a single call. Use this instead of
    multiple edit_file calls when making independent edits to different files (or multiple edits
    to the same file, applied in order).

    Each entry must have 'path', 'old_string', and 'new_string' keys.

    Args:
        edits: List of dicts with 'path' (file path), 'old_string' (text to find), and 'new_string' (replacement).
               Example: [{"path": "src/main.py", "old_string": "foo", "new_string": "bar"}]
    """
    original: dict[str, str] = {}
    preview: dict[str, str] = {}
    preview_errors: list[str] = []
    for entry in edits:
        path = entry.get("path", "")
        old = entry.get("old_string", "")
        new = entry.get("new_string", "")
        if not path or not old:
            preview_errors.append(f"{path or '<missing>'}: missing 'path' or 'old_string'")
            continue
        if path not in preview:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    original[path] = preview[path] = f.read()
            except FileNotFoundError:
                preview_errors.append(f"{path}: file not found")
                continue
            except Exception as e:
                preview_errors.append(f"{path}: {e}")
                continue
        buf = preview[path]
        cnt = buf.count(old)
        if cnt == 0:
            preview_errors.append(f"{path}: old_string not found")
            continue
        if cnt > 1:
            preview_errors.append(f"{path}: old_string found {cnt} times, must be unique")
            continue
        preview[path] = buf.replace(old, new, 1)

    parts = [Text(f"Apply {len(edits)} edits:", style="bold"), Text()]
    for path in preview:
        parts.append(_format_unified_diff(original[path], preview[path], path))
        parts.append(Text())
    for err in preview_errors:
        parts.append(Text(err, style="red"))
    if not check_permission("edit", ", ".join(e.get("path", "") for e in edits), Group(*parts)):
        return f"PERMISSION DENIED by user: batch edit of {len(edits)} files. Do NOT retry this operation. Stop and ask the user for new instructions."

    errors = list(preview_errors)
    results = [
        entry.get("path", "")
        for entry in edits
        if entry.get("path") in preview and entry.get("old_string")
    ]

    written = set()
    for path, content in preview.items():
        if original[path] == content:
            continue
        try:
            _checkpoint_file(path)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            written.add(path)
        except Exception as e:
            errors.append(f"Error writing {path}: {e}")

    parts = []
    if results:
        _notify_file_mutation()
        unique = list(dict.fromkeys(results))
        parts.append(f"Applied {len(results)} edits across {len(unique)} files: {', '.join(unique)}")
        for entry in edits:
            old = entry.get("old_string", "")
            new = entry.get("new_string", "")
            path = entry.get("path", "")
            if old and path in written:
                diff_text = _compact_diff(old, new, path)
                if diff_text:
                    parts.append(diff_text)
    if errors:
        parts.append("\n".join(errors))
    return "\n".join(parts) or "No edits to apply."


def list_directory(directory: str = ".") -> str:
    """List files and directories in the given path.

    Args:
        directory: Directory to list. Defaults to current directory.
    """
    try:
        abs_dir = os.path.abspath(directory)
        entries = os.listdir(abs_dir)
        result = []
        for entry in sorted(entries):
            if is_ignored(entry, abs_dir):
                continue
            full_path = os.path.join(abs_dir, entry)
            if os.path.isdir(full_path):
                result.append(f"📁 {entry}/")
            else:
                size = os.path.getsize(full_path)
                result.append(f"📄 {entry} ({size} bytes)")
        return "\n".join(result) if result else "Empty directory"
    except FileNotFoundError:
        return f"Error: Directory not found: {directory}"
    except Exception as e:
        return f"Error listing directory: {e}"


def get_project_tree(root_dir: str, max_depth: int = 3, max_files_per_dir: int = 30) -> str:
    """Generate a fast, text-based directory tree respecting .gitignore rules."""
    lines = []
    root_dir = os.path.abspath(root_dir)

    if not os.path.exists(root_dir):
        return ""

    for dirpath, dirs, files in walk_filtered(root_dir):
        rel_path = os.path.relpath(dirpath, root_dir)

        if rel_path == ".":
            depth = 0
            lines.append(os.path.basename(root_dir) + "/")
        else:
            depth = rel_path.count(os.sep) + 1
            if depth > max_depth:
                continue

            indent = "  " * depth
            lines.append(f"{indent}{os.path.basename(dirpath)}/")

        file_indent = "  " * (depth + 1)
        sorted_files = sorted(files)
        for i, f in enumerate(sorted_files):
            if i >= max_files_per_dir:
                lines.append(f"{file_indent}... ({len(files) - max_files_per_dir} more files)")
                break
            lines.append(f"{file_indent}{f}")

    result = "\n".join(lines)
    max_chars = 5000
    if len(result) > max_chars:
        return result[:max_chars] + "\n... [Tree truncated due to size]"
    return result


# ── Async tool wrappers ───────────────────────────────────────────────
# Agents see these as the actual tools. Each wrapper offloads the blocking
# sync implementation to a worker thread so the event loop stays responsive.

_read_file_tool = _thread_tool(read_file)
_write_file_tool = _thread_tool(write_file)
_write_files_tool = _thread_tool(write_files)
_edit_file_tool = _thread_tool(edit_file)
_edit_files_tool = _thread_tool(edit_files)
_list_directory_tool = _thread_tool(list_directory)


async def read_files(paths: list[str], max_size: int = 12_000) -> str:
    """Read many files in parallel. Use instead of multiple read_file calls when
    you want to pull N files at once (e.g. after rank_files).

    Args:
        paths: List of file paths (absolute or relative) to read.
        max_size: Max bytes per file before truncation. Default 12KB.
    """
    if not paths:
        return "No paths provided."

    async def _one(p: str) -> tuple[str, str]:
        return p, await asyncio.to_thread(read_file, p, 0, 0, max_size)

    results = await asyncio.gather(*(_one(p) for p in paths))
    blocks = [f"=== {path} ===\n{content}" for path, content in results]
    return "\n\n".join(blocks)
