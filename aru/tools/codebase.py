"""Custom tools for codebase exploration and manipulation."""

import fnmatch
import html.parser
import os
import re
import shlex
import subprocess
import sys
import threading
import textwrap

from aru.tools.gitignore import is_ignored, walk_filtered

import httpx

from rich.console import Console, Group
from rich.syntax import Syntax
from rich.text import Text

from aru.permissions import check_permission, get_skip_permissions

_console = Console()
_live = None       # Reference to the active Rich Live instance
_display = None    # Reference to the active StreamingDisplay
_model_id: str = "claude-sonnet-4-5-20250929"  # Current model for sub-agents
_on_file_mutation = None  # Callback to invalidate context cache after file writes


def set_on_file_mutation(callback):
    """Set a callback invoked after any file write/edit/bash operation."""
    global _on_file_mutation
    _on_file_mutation = callback


def _notify_file_mutation():
    """Notify the session that files changed so caches are invalidated."""
    _read_cache.clear()
    if _on_file_mutation:
        _on_file_mutation()



_small_model_ref: str = "anthropic/claude-haiku-4-5"  # Small model for sub-agents


def set_model_id(model_id: str):
    global _model_id
    _model_id = model_id


def set_small_model_ref(model_ref: str):
    """Set the small/fast model reference used by sub-agents."""
    global _small_model_ref
    _small_model_ref = model_ref


def _get_small_model_ref() -> str:
    """Get the small model reference for sub-agents."""
    return _small_model_ref


def set_live(live):
    """Set the active Live instance so tools can pause it during permission prompts."""
    global _live
    _live = live


def set_display(display):
    """Set the active StreamingDisplay so tools can flush content before permission prompts."""
    global _display
    _display = display


def set_console(console: Console):
    """Share the main console instance to avoid conflicts with Live display."""
    global _console
    _console = console



def _format_diff(old_string: str, new_string: str) -> Group:
    """Format old/new strings as a colored diff (red background for deletions, green for additions)."""
    parts = []
    if old_string:
        for line in old_string.splitlines():
            parts.append(Text.assemble(
                ("- " + line, "on red"),
            ))
    if new_string:
        for line in new_string.splitlines():
            parts.append(Text.assemble(
                ("+ " + line, "white on green"),
            ))
    return Group(*parts)



# Hard ceiling per tool result (~15K tokens). Even max_size=0 respects this per chunk.
_READ_HARD_CAP = 60_000  # bytes

# Per-session read cache: avoids re-reading the same file+range multiple times.
# Key = (resolved_path, start_line, end_line, max_size), Value = short metadata description.
_read_cache: dict[tuple, str] = {}


def clear_read_cache():
    """Clear the read cache. Call after file mutations to avoid stale data."""
    _read_cache.clear()


def read_file(file_path: str, start_line: int = 0, end_line: int = 0, max_size: int = 15_000) -> str:
    """Read file contents. Returns chunked output for large files.

    Args:
        file_path: Path to the file (absolute or relative).
        start_line: First line (1-indexed, inclusive). 0 = beginning.
        end_line: Last line (1-indexed, inclusive). 0 = end.
        max_size: Max bytes before truncation. Default 15KB.
            Set to 0 to read the full file in chunks — each chunk up to ~60KB.
            The first chunk includes a continuation hint so you can call again
            with start_line to get the next chunk.
    """
    try:
        resolved = os.path.abspath(file_path)
        cache_key = (resolved, start_line, end_line, max_size)
        # Only cache specific range reads — full-file reads may have been compressed
        # out of context, so blocking them causes the agent to get stuck
        if cache_key in _read_cache and (start_line > 0 or end_line > 0):
            lines_info = _read_cache[cache_key]
            return (
                f"[cached] Already read ({lines_info})."
                f" Use the content from your earlier call."
            )

        # Check if file exists and get size
        file_size = os.path.getsize(file_path)

        full_read = max_size == 0
        effective_limit = _READ_HARD_CAP if full_read else max_size

        # Detect binary files by checking for null bytes in the first 1KB
        with open(file_path, "rb") as f:
            sample = f.read(1024)
        if b"\x00" in sample:
            return f"Error: Binary file detected ({file_size} bytes): {file_path}"

        # Read with line range support
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total_lines = len(lines)

        if start_line > 0 or end_line > 0:
            # Line range mode (1-indexed, inclusive)
            s = max(start_line, 1) - 1  # Convert to 0-indexed
            e = end_line if end_line > 0 else total_lines
            e = min(e, total_lines)

            selected = lines[s:e]

            # Apply chunk limit based on bytes
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

        # Full file mode — check if it fits in one chunk
        if file_size <= effective_limit:
            numbered = [f"{i + 1:4d} | {line}" for i, line in enumerate(lines)]
            output = "".join(numbered)
            result = output if full_read else _truncate_output(output)
            _read_cache[cache_key] = f"{total_lines} lines"
            return result

        # File exceeds limit — return first chunk + outline of the rest
        # First chunk: up to effective_limit bytes
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

        # Outline of remaining content (definitions after the first chunk)
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


# Threshold: files smaller than this are returned as-is (not worth a model call)
_SMART_READ_THRESHOLD = 3_000  # chars (~750 tokens)


async def read_file_smart(file_path: str, query: str) -> str:
    """Read a file and answer a specific question about it — returns a concise answer, NOT raw content.

    Use this instead of read_file when you only need a specific piece of information
    about a file (e.g., "does this file have tests for X?", "what does function Y do?",
    "which classes are exported?"). This is much cheaper on tokens than reading the full file.

    Use plain read_file only when you need to see the actual code/content.

    Args:
        file_path: Path to the file to read.
        query: The specific question you want answered about this file.
    """
    # Read raw content first (reuse existing read_file logic)
    raw = read_file(file_path, max_size=20_000)

    if raw.startswith("Error:"):
        return raw

    # Strip line number prefixes for the model (cleaner input)
    lines = raw.splitlines()
    clean_lines = []
    for line in lines:
        # Lines have format "  42 | content" — strip the prefix
        if " | " in line[:8]:
            clean_lines.append(line.split(" | ", 1)[1] if " | " in line else line)
        else:
            clean_lines.append(line)
    clean = "\n".join(clean_lines)

    # Small file — just return raw content (model call not worth it)
    if len(clean) <= _SMART_READ_THRESHOLD:
        return raw

    # Large file — call small model to answer the query
    from agno.agent import Agent
    from aru.providers import create_model

    small_ref = _get_small_model_ref()
    prompt = (
        f"Answer this question about the file `{file_path}`:\n\n"
        f"**Question:** {query}\n\n"
        f"**File content:**\n```\n{clean[:15_000]}\n```\n\n"
        f"Give a concise, direct answer. If code is relevant, quote only the essential snippet."
    )

    try:
        summarizer = Agent(
            name="FileReader",
            model=create_model(small_ref, max_tokens=512),
            instructions="You answer specific questions about source code files. Be concise and direct.",
            markdown=False,
        )
        result = await summarizer.arun(prompt, stream=False)
        answer = result.content.strip() if result and result.content else ""
        if not answer:
            return raw  # fallback
        return f"[{file_path}] {answer}"
    except Exception:
        return raw  # fallback to raw content on any error



def write_file(file_path: str, content: str) -> str:
    """Write content to a file, creating parent directories if needed.

    Args:
        file_path: Path to the file to write.
        content: The content to write to the file.
    """
    preview = content[:500] + ("..." if len(content) > 500 else "")
    header = Text(file_path, style="bold")
    diff = _format_diff("", preview)
    if not check_permission("write", file_path, Group(header, Text(), diff)):
        return f"Permission denied: write to {file_path}"
    try:
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
        preview = content[:300] + ("..." if len(content) > 300 else "")
        parts.append(Text(p, style="bold dim"))
        parts.append(_format_diff("", preview))
        parts.append(Text())
    if not check_permission("write", ", ".join(e.get("path", "") for e in file_list), Group(*parts)):
        return f"Permission denied: batch write of {len(file_list)} files"

    results = []
    errors = []
    for entry in file_list:
        path = entry.get("path", "")
        content = entry.get("content", "")
        if not path:
            errors.append("Error: missing 'path' in entry")
            continue
        try:
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
    header = Text(file_path, style="bold")
    diff = _format_diff(old_string, new_string)
    if not check_permission("edit", file_path, Group(header, Text(), diff)):
        return f"Permission denied: edit {file_path}"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {file_path}"
        if count > 1:
            return f"Error: old_string found {count} times in {file_path}. Must be unique."

        new_content = content.replace(old_string, new_string, 1)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        _notify_file_mutation()
        return f"Successfully edited {file_path}"
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error editing file: {e}"


def edit_files(edits: list[dict]) -> str:
    """Apply multiple find-and-replace edits across files in a single call. Use this instead of
    multiple edit_file calls when making independent edits to different files (or multiple edits
    to the same file, applied in order).

    Each entry must have 'path', 'old_string', and 'new_string' keys.

    Args:
        edits: List of dicts with 'path' (file path), 'old_string' (text to find), and 'new_string' (replacement).
               Example: [{"path": "src/main.py", "old_string": "foo", "new_string": "bar"}]
    """
    parts = [Text(f"Apply {len(edits)} edits:", style="bold"), Text()]
    for e in edits:
        p = e.get("path", "<missing>")
        old = e.get("old_string", "")
        new = e.get("new_string", "")
        parts.append(Text(p, style="bold dim"))
        parts.append(_format_diff(old, new))
        parts.append(Text())
    if not check_permission("edit", ", ".join(e.get("path", "") for e in edits), Group(*parts)):
        return f"Permission denied: batch edit of {len(edits)} files"

    results = []
    errors = []
    # Cache file contents to support multiple edits to the same file
    cache: dict[str, str] = {}

    for entry in edits:
        path = entry.get("path", "")
        old = entry.get("old_string", "")
        new = entry.get("new_string", "")
        if not path or not old:
            errors.append(f"Error: missing 'path' or 'old_string' in entry")
            continue
        try:
            if path not in cache:
                with open(path, "r", encoding="utf-8") as f:
                    cache[path] = f.read()

            content = cache[path]
            count = content.count(old)
            if count == 0:
                errors.append(f"{path}: old_string not found")
                continue
            if count > 1:
                errors.append(f"{path}: old_string found {count} times, must be unique")
                continue

            cache[path] = content.replace(old, new, 1)
            results.append(path)
        except FileNotFoundError:
            errors.append(f"{path}: file not found")
        except Exception as e:
            errors.append(f"{path}: {e}")

    # Flush all modified files
    written = set()
    for path, content in cache.items():
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            written.add(path)
        except Exception as e:
            errors.append(f"Error writing {path}: {e}")

    parts = []
    if results:
        _notify_file_mutation()
        unique = list(dict.fromkeys(results))  # preserve order, dedupe
        parts.append(f"Successfully applied {len(results)} edits across {len(unique)} files: {', '.join(unique)}")
    if errors:
        parts.append("\n".join(errors))
    return "\n".join(parts) or "No edits to apply."


def glob_search(pattern: str, directory: str = ".") -> str:
    """Find files matching a glob pattern recursively.

    Args:
        pattern: Glob pattern to match (e.g. '**/*.py', 'src/**/*.ts').
        directory: Directory to search in. Defaults to current directory.
    """
    matches = []
    for root, dirs, files in walk_filtered(directory):
        for filename in files:
            filepath = os.path.join(root, filename)
            rel_path = os.path.relpath(filepath, directory)
            # Normalize to forward slashes for consistent fnmatch behaviour on Windows
            rel_posix = rel_path.replace('\\', '/')
            matched = fnmatch.fnmatch(rel_posix, pattern)
            # For patterns like **/*.py, also match root-level files against the suffix
            # because fnmatch requires a path separator before the file part
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


def grep_search(pattern: str, directory: str = ".", file_glob: str = "", context_lines: int = 10) -> str:
    """Search for a regex pattern in file contents.

    Args:
        pattern: Regular expression pattern to search for.
        directory: Directory to search in. Defaults to current directory.
        file_glob: Optional glob to filter which files to search (e.g. '*.py').
        context_lines: Lines of context before and after each match (like grep -C). Default 10.
            Use 0 for file-level matches only. Use 30+ for full function bodies.
    """
    import re

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    results = []
    match_count = 0
    files_with_matches: dict[str, list[int]] = {}  # rel_path -> list of match line numbers
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
                    # Collect match line indices
                    match_indices = [i for i, line in enumerate(lines) if regex.search(line)]
                    if not match_indices:
                        continue

                    files_with_matches[rel_path] = [i + 1 for i in match_indices]

                    # Only emit context blocks if we haven't exceeded the limit
                    if match_count < MAX_MATCHES:
                        # Merge overlapping context windows
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

    # Trim trailing separator
    if results and results[-1] == "---":
        results.pop()

    if match_count > MAX_MATCHES and context_lines == 0:
        output = "\n".join(results[:MAX_MATCHES])
    else:
        output = "\n".join(results)

    # Append file summary so the model knows where ALL matches are
    if len(files_with_matches) > 1 or stopped_early:
        summary_lines = ["\n[Match summary]"]
        for fpath, line_nums in files_with_matches.items():
            nums = ", ".join(str(n) for n in line_nums[:10])
            extra = f" +{len(line_nums) - 10} more" if len(line_nums) > 10 else ""
            summary_lines.append(f"  {fpath}: lines {nums}{extra}")
        if stopped_early:
            summary_lines.append(f"  ... search stopped at {match_count} matches. Use file_glob or a more specific pattern.")
        output += "\n".join(summary_lines)

    return _truncate_output(output)


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
    import os
    from aru.tools.gitignore import walk_filtered

    lines = []
    root_dir = os.path.abspath(root_dir)
    
    if not os.path.exists(root_dir):
        return ""

    for dirpath, dirs, files in walk_filtered(root_dir):
        rel_path = os.path.relpath(dirpath, root_dir)
        
        # Calculate depth
        if rel_path == ".":
            depth = 0
            lines.append(os.path.basename(root_dir) + "/")
        else:
            depth = rel_path.count(os.sep) + 1
            if depth > max_depth:
                dirs.clear()  # Stop descending
                continue
            
            indent = "  " * depth
            lines.append(f"{indent}{os.path.basename(dirpath)}/")
            
        # Add files
        file_indent = "  " * (depth + 1)
        sorted_files = sorted(files)
        for i, f in enumerate(sorted_files):
            if i >= max_files_per_dir:
                lines.append(f"{file_indent}... ({len(files) - max_files_per_dir} more files)")
                break
            lines.append(f"{file_indent}{f}")
            
    result = "\n".join(lines)
    if len(result) > 15000:
        return result[:15000] + "\n... [Tree truncated due to size]"
    return result



import atexit

# ── Process tracking ──────────────────────────────────────────────
# Keep references to long-running background processes so we can kill
# them when the main ARC process exits (avoid zombie / ghost processes).
_tracked_processes: list[subprocess.Popen] = []


def _register_process(process: subprocess.Popen):
    """Track a background process for cleanup on exit."""
    _tracked_processes.append(process)


def _cleanup_processes():
    """Kill all tracked background processes on exit."""
    for proc in _tracked_processes:
        if proc.poll() is None:  # still running
            _kill_process_tree(proc)


atexit.register(_cleanup_processes)


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


def _kill_process_tree(process: subprocess.Popen):
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
        # Fallback to basic kill
        try:
            process.kill()
        except Exception:
            pass


_MAX_OUTPUT_CHARS = 10_000
_TRUNCATE_KEEP = 3_000  # chars to keep from start and end


def _truncate_output(text: str) -> str:
    """Truncate long tool output to save tokens. Keeps start + end with a marker in the middle."""
    from aru.context import truncate_output
    return truncate_output(text)


def _is_long_running(command: str) -> bool:
    """Detect commands that start servers or long-running processes."""
    cmd = command.strip()
    # Explicit background indicator
    if cmd.endswith("&"):
        return True
    return any(pattern in cmd for pattern in BACKGROUND_PATTERNS)


def run_command(command: str, timeout: int = 60, working_directory: str = "") -> str:
    """Execute a shell command and return output.

    Args:
        command: The command to execute.
        timeout: Max seconds. Default 60.
        working_directory: Directory to run in. Default: cwd.
    """
    cwd = working_directory or os.getcwd()

    # Long-running commands: start, capture initial output for a few seconds, then detach
    if _is_long_running(command):
        import threading
        import time

        startup_seconds = 5
        try:
            bg_kwargs: dict = dict(
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
            )
            if sys.platform != "win32":
                bg_kwargs["start_new_session"] = True
            process = subprocess.Popen(command, **bg_kwargs)

            # Read stdout in a thread so we don't block on Windows
            lines: list[str] = []
            stop_event = threading.Event()

            def _reader():
                while not stop_event.is_set():
                    try:
                        line = process.stdout.readline()
                        if line:
                            lines.append(line.rstrip())
                        elif process.poll() is not None:
                            break
                    except Exception:
                        break

            reader_thread = threading.Thread(target=_reader, daemon=True)
            reader_thread.start()

            # Wait for startup output or early exit
            time.sleep(startup_seconds)
            stop_event.set()
            reader_thread.join(timeout=1)

            exit_code = process.poll()
            output = "\n".join(lines) if lines else "(no output yet)"

            if exit_code is not None:
                # Process already finished (likely an error)
                return f"Process exited immediately (code {exit_code}):\n{output}"

            # Track so it gets killed when ARC exits
            _register_process(process)

            return (
                f"Process running in background (PID {process.pid}).\n"
                f"Initial output ({startup_seconds}s):\n{output}"
            )
        except Exception as e:
            return f"Error starting background process: {e}"

    try:
        popen_kwargs = dict(
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
        )
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # Start in a new process group so _kill_process_tree (os.killpg)
            # does not accidentally kill the parent process when timing out.
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(command, **popen_kwargs)
        stdout, stderr = process.communicate(timeout=timeout)

        parts = []
        if stdout:
            parts.append(_truncate_output(stdout))
        if stderr:
            parts.append(f"STDERR:\n{_truncate_output(stderr)}")
        if process.returncode != 0:
            parts.append(f"Exit code: {process.returncode}")

        return "\n".join(parts).strip() or "(no output)"
    except subprocess.TimeoutExpired:
        # Kill the entire process tree, not just the shell wrapper
        _kill_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", ""
        partial = (stdout or "") + (stderr or "")
        partial = partial.strip()
        msg = f"Error: Command timed out after {timeout} seconds."
        if partial:
            tail = "\n".join(partial.splitlines()[-20:])
            msg += f"\nLast output:\n{tail}"
        msg += "\nHint: if this is a server/long-running process, it will be detected and run in background automatically."
        return msg
    except Exception as e:
        return f"Error running command: {e}"


def bash(command: str, timeout: int = 60, working_directory: str = "") -> str:
    """Execute a shell command (tests, git, install, build, etc).

    Args:
        command: The command to execute.
        timeout: Max seconds to wait. Default 60.
        working_directory: Directory to run in. Default: cwd.
    """
    cwd = working_directory or os.getcwd()
    cmd_display = Group(
        Syntax(command, "bash", theme="monokai"),
        Text(f"cwd: {cwd}", style="dim"),
    )
    if not check_permission("bash", command, cmd_display):
        return f"Permission denied: {command}"
    result = run_command(command, timeout=timeout, working_directory=working_directory)
    # Bash can modify files, so always invalidate cache
    _notify_file_mutation()
    return result


class _HTMLToText(html.parser.HTMLParser):
    """Minimal HTML-to-text converter — no external dependencies."""

    SKIP_TAGS = {"script", "style", "svg", "noscript", "head"}
    BLOCK_TAGS = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
                  "li", "tr", "blockquote", "pre", "section", "article", "header", "footer"}

    def __init__(self):
        super().__init__()
        self._pieces: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS and not self._skip_depth:
            self._pieces.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self.BLOCK_TAGS and not self._skip_depth:
            self._pieces.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._pieces.append(data)

    def get_text(self) -> str:
        raw = "".join(self._pieces)
        # Collapse whitespace within lines, preserve line breaks
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        # Collapse multiple blank lines
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
        return text.strip()


def _html_to_text(html_content: str) -> str:
    parser = _HTMLToText()
    parser.feed(html_content)
    return parser.get_text()


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web for information.

    Args:
        query: The search query.
        max_results: Max results to return (default 5).
    """
    import re as _re
    import urllib.parse

    encoded = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"

    try:
        with httpx.Client(follow_redirects=True, timeout=15) as client:
            resp = client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            resp.raise_for_status()
    except httpx.RequestError as e:
        return f"Search error: {e}"

    html = resp.text
    results = []

    # Parse DuckDuckGo HTML results
    blocks = _re.findall(
        r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html, _re.DOTALL,
    )

    for i, (link, title, snippet) in enumerate(blocks[:max_results], 1):
        # Clean HTML tags
        title_clean = _re.sub(r"<[^>]+>", "", title).strip()
        snippet_clean = _re.sub(r"<[^>]+>", "", snippet).strip()
        # DuckDuckGo wraps URLs in a redirect — extract the actual URL
        actual_url = link
        ud_match = _re.search(r"uddg=([^&]+)", link)
        if ud_match:
            actual_url = urllib.parse.unquote(ud_match.group(1))
        results.append(f"{i}. {title_clean}\n   {actual_url}\n   {snippet_clean}")

    if not results:
        return f"No results found for: {query}"
    return "\n\n".join(results)


def web_fetch(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return content as text.

    Args:
        url: The URL to fetch.
        max_chars: Max characters to return (default 8000).
    """
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            resp = client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; aru-agent/0.1)",
                "Accept": "text/html,application/json,text/plain,*/*",
            })
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return f"HTTP error {e.response.status_code}: {e.response.reason_phrase}"
    except httpx.RequestError as e:
        return f"Request error: {e}"

    content_type = resp.headers.get("content-type", "")
    body = resp.text

    if "json" in content_type:
        # JSON — return as-is (already readable)
        text = body
    elif "html" in content_type:
        text = _html_to_text(body)
    else:
        # Plain text or other
        text = body

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... [truncated at {max_chars} chars]"
    return _truncate_output(text)


_SUBAGENT_COUNTER = 0
_SUBAGENT_COUNTER_LOCK = threading.Lock()


def _next_subagent_id() -> int:
    global _SUBAGENT_COUNTER
    with _SUBAGENT_COUNTER_LOCK:
        _SUBAGENT_COUNTER += 1
        return _SUBAGENT_COUNTER


# Import new tools
from aru.tools.ast_tools import code_structure, find_dependencies
from aru.tools.ranker import rank_files

# Tools available to sub-agents (no delegate_task to prevent infinite nesting)
_SUBAGENT_TOOLS = [
    read_file,
    write_file,
    write_files,
    edit_file,
    edit_files,
    glob_search,
    grep_search,
    list_directory,
    bash,
    web_search,
    web_fetch,
    code_structure,
    find_dependencies,
    rank_files,
]


async def delegate_task(task: str, context: str = "", agent: str = "") -> str:
    """Delegate a task to a sub-agent that runs autonomously. Multiple calls run concurrently.
    Use for independent research or subtasks to keep your own context clean.

    Args:
        task: What the sub-agent should do.
        context: Optional extra context (file paths, constraints).
        agent: Optional custom agent name to use instead of the generic sub-agent.
    """
    from agno.agent import Agent
    from aru.providers import create_model

    agent_id = _next_subagent_id()
    cwd = os.getcwd()
    small_model_ref = _get_small_model_ref()

    agent_perm = None
    if agent and agent in _custom_agent_defs:
        agent_def = _custom_agent_defs[agent]
        agent_perm = agent_def.permission
        tools = resolve_tools(agent_def.tools) if agent_def.tools else list(_SUBAGENT_TOOLS)
        tools = [t for t in tools if t is not delegate_task]
        instructions = agent_def.system_prompt + f"\nThe current working directory is: {cwd}\n"
        if context:
            instructions += f"\nAdditional context:\n{context}\n"
        model_ref = agent_def.model or small_model_ref
        sub = Agent(
            name=f"{agent_def.name}-{agent_id}",
            model=create_model(model_ref, max_tokens=4096),
            tools=tools,
            instructions=instructions,
            markdown=True,
        )
    else:
        instructions = f"""\
You are a sub-agent (#{agent_id}) working on a specific task. Be focused and concise.
Complete the task and return a clear summary of what you did or found.
The current working directory is: {cwd}
Do not create documentation files unless explicitly asked.
"""
        if context:
            instructions += f"\nAdditional context:\n{context}\n"

        sub = Agent(
            name=f"SubAgent-{agent_id}",
            model=create_model(small_model_ref, max_tokens=4096),
            tools=_SUBAGENT_TOOLS,
            instructions=instructions,
            markdown=True,
        )

    try:
        from aru.permissions import permission_scope
        with permission_scope(agent_perm):
            result = await sub.arun(task, stream=False)
        if result and result.content:
            return _truncate_output(f"[SubAgent-{agent_id}] {result.content}")
        return f"[SubAgent-{agent_id}] Task completed but no output was returned."
    except Exception as e:
        return f"[SubAgent-{agent_id}] Error: {e}"


# All tools as a list for easy import
ALL_TOOLS = [
    read_file,
    read_file_smart,
    write_file,
    write_files,
    edit_file,
    edit_files,
    glob_search,
    grep_search,
    list_directory,
    bash,
    web_search,
    web_fetch,
    delegate_task,
    code_structure,
    find_dependencies,
    rank_files,
]

# Task list tools for executor subtask tracking
from aru.tools.tasklist import create_task_list, update_task

# Executor tools — full write/execute capability, no discovery overhead
EXECUTOR_TOOLS = [
    create_task_list,
    update_task,
    read_file,
    read_file_smart,
    write_file,
    write_files,
    edit_file,
    edit_files,
    glob_search,
    grep_search,
    list_directory,
    bash,
    web_search,
    web_fetch,
    delegate_task,
    code_structure,
]

# General-purpose tools — everything except niche analysis tools
GENERAL_TOOLS = [
    read_file,
    read_file_smart,
    write_file,
    write_files,
    edit_file,
    edit_files,
    glob_search,
    grep_search,
    list_directory,
    bash,
    web_search,
    web_fetch,
    delegate_task,
]

# Registry mapping tool name strings to function references
TOOL_REGISTRY: dict[str, object] = {f.__name__: f for f in ALL_TOOLS}
TOOL_REGISTRY["create_task_list"] = create_task_list
TOOL_REGISTRY["update_task"] = update_task


def resolve_tools(tool_spec: list[str] | dict[str, bool]) -> list:
    """Resolve a tool specification to a list of tool functions.

    Args:
        tool_spec: Either:
            - Empty list: returns GENERAL_TOOLS (default set)
            - List of strings: allowlist of tool names
            - Dict[str, bool]: starts from GENERAL_TOOLS, adds/removes by name
    """
    if isinstance(tool_spec, dict):
        result = list(GENERAL_TOOLS)
        for name, enabled in tool_spec.items():
            func = TOOL_REGISTRY.get(name)
            if func is None:
                continue
            if enabled and func not in result:
                result.append(func)
            elif not enabled and func in result:
                result.remove(func)
        return result

    if not tool_spec:
        return list(GENERAL_TOOLS)

    resolved = []
    for name in tool_spec:
        func = TOOL_REGISTRY.get(name)
        if func:
            resolved.append(func)
    return resolved


# Custom agent definitions for delegate_task (populated at startup)
_custom_agent_defs: dict = {}


def set_custom_agents(agents: dict):
    """Register custom agent definitions and update delegate_task docstring."""
    global _custom_agent_defs
    _custom_agent_defs = {k: v for k, v in agents.items() if v.mode == "subagent"}
    # Update delegate_task docstring with available subagents so the LLM knows about them
    _update_delegate_task_docstring()


def _update_delegate_task_docstring():
    """Dynamically update delegate_task's docstring to list available subagents."""
    base_doc = """Delegate a task to a sub-agent that runs autonomously. Multiple calls run concurrently.
    Use for independent research or subtasks to keep your own context clean.

    Args:
        task: What the sub-agent should do.
        context: Optional extra context (file paths, constraints).
        agent: Optional custom agent name to use instead of the generic sub-agent."""

    if _custom_agent_defs:
        lines = [f"\n\n    Available specialized agents (use the agent parameter to invoke):"]
        for name, agent_def in _custom_agent_defs.items():
            lines.append(f"    - agent=\"{name}\": {agent_def.description}")
        base_doc += "\n".join(lines)

    delegate_task.__doc__ = base_doc


async def load_mcp_tools():
    """Initialize MCP servers and inject their tools into tool lists dynamically."""
    from aru.tools.mcp_client import init_mcp
    try:
        mcp_tools = await init_mcp()
        if mcp_tools:
            _console.print(f"[dim]Loaded {len(mcp_tools)} tools from MCP servers.[/dim]")
            for t in mcp_tools:
                ALL_TOOLS.append(t)
                EXECUTOR_TOOLS.append(t)
                GENERAL_TOOLS.append(t)
    except Exception as e:
        _console.print(f"[dim]Failed to load MCP tools: {e}[/dim]")
