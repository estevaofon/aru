"""Custom tools for codebase exploration and manipulation."""

import fnmatch
import html.parser
import os
import re
import shlex
import subprocess
import threading
import textwrap

from arc.tools.gitignore import is_ignored, walk_filtered

import httpx

from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

_console = Console()
_skip_permissions = False
_live = None       # Reference to the active Rich Live instance
_permission_lock = threading.Lock()  # Serialize permission prompts
_allowed_actions: set[str] = set()   # Actions auto-approved via "allow all"
_display = None    # Reference to the active StreamingDisplay
_model_id: str = "claude-sonnet-4-5-20250929"  # Current model for sub-agents
_permission_rules: list[str] = []  # User-defined glob patterns from arc.json


def set_skip_permissions(value: bool):
    global _skip_permissions
    _skip_permissions = value


def set_model_id(model_id: str):
    global _model_id
    _model_id = model_id


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


def set_permission_rules(rules: list[str]):
    """Set user-defined permission rules (glob patterns) from arc.json."""
    global _permission_rules
    _permission_rules = list(rules)


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


def reset_allowed_actions():
    """Reset auto-approved actions (call between conversations if needed)."""
    _allowed_actions.clear()


def _ask_permission(action: str, details: str | Text | Group) -> bool:
    """Ask user permission before executing a dangerous action.

    Uses a lock to serialize prompts when multiple tools run in parallel.
    Supports 'a' (allow all) to auto-approve all future calls of the same action type.
    """
    if _skip_permissions:
        return True

    if action in _allowed_actions:
        return True

    with _permission_lock:
        # Re-check after acquiring lock (another thread may have allowed it)
        if action in _allowed_actions:
            return True

        # Pause Live and flush already-streamed content so it doesn't re-render
        if _live:
            _live.stop()
        if _display:
            _display.flush()

        _console.print()
        _console.print(Panel(
            details,
            title=f"[bold yellow]{action}[/bold yellow]",
            border_style="yellow",
            expand=False,
        ))
        try:
            answer = _console.input(
                "[bold yellow]Allow? (y)es / (a)llow all / (n)o:[/bold yellow] "
            ).strip().lower()
            if answer in ("a", "allow all", "all"):
                _allowed_actions.add(action)
                allowed = True
            else:
                allowed = answer in ("y", "yes", "s", "sim")
        except (EOFError, KeyboardInterrupt):
            allowed = False

        # Resume Live display (now clean — flushed content won't re-render)
        if _live:
            _live.start()
            _live._live_render._shape = None  # prevent overwriting static Panel

        return allowed


def read_file(file_path: str, start_line: int = 0, end_line: int = 0, max_size: int = 30_000) -> str:
    """Read the contents of a file.

    Args:
        file_path: Path to the file to read (absolute or relative to working directory).
        start_line: First line to read (1-indexed, inclusive). 0 means from the beginning.
        end_line: Last line to read (1-indexed, inclusive). 0 means to the end.
        max_size: Maximum file size in bytes before truncation. Defaults to 30KB. Ignored when line range is specified.
    """
    try:
        # Check if file exists and get size
        file_size = os.path.getsize(file_path)

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
            
            # Cap the maximum lines returned to prevent huge context blowouts
            max_lines = 1000
            truncated = False
            if e - s > max_lines:
                e = s + max_lines
                truncated = True
                
            selected = lines[s:e]
            numbered = [f"{s + i + 1:4d} | {line}" for i, line in enumerate(selected)]
            header = f"[Lines {s + 1}-{e} of {total_lines}]\n"
            result = header + "".join(numbered)
            if truncated:
                result += f"\n\n[WARNING] Output truncated to {max_lines} lines. Use a smaller range to read further."
            return result

        # Full file mode with size limit
        if file_size > max_size:
            # Read up to max_size bytes worth of lines
            accumulated = []
            char_count = 0
            for i, line in enumerate(lines):
                char_count += len(line)
                if char_count > max_size:
                    break
                accumulated.append(f"{i + 1:4d} | {line}")
            lines_shown = len(accumulated)
            return (
                "".join(accumulated)
                + f"\n\n[WARNING] File truncated at ~{max_size:,} bytes ({file_size:,} total, "
                + f"{lines_shown}/{total_lines} lines shown). "
                + "Use start_line/end_line to read specific sections."
            )

        numbered = [f"{i + 1:4d} | {line}" for i, line in enumerate(lines)]
        return "".join(numbered)
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
    preview = content[:500] + ("..." if len(content) > 500 else "")
    header = Text(file_path, style="bold")
    diff = _format_diff("", preview)
    if not _ask_permission("Write File", Group(header, Text(), diff)):
        return f"Permission denied: write to {file_path}"
    try:
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote to {file_path}"
    except Exception as e:
        return f"Error writing file: {e}"


def write_files(files: list[dict]) -> str:
    """Write multiple files at once. Use this instead of multiple write_file calls when creating
    or updating several files that don't depend on each other (e.g. scaffolding a project).

    Each entry in the list must have 'path' and 'content' keys.

    Args:
        files: List of dicts with 'path' (file path) and 'content' (file content) keys.
               Example: [{"path": "src/main.py", "content": "print('hello')"}, {"path": "src/utils.py", "content": "..."}]
    """
    parts = [Text(f"Write {len(files)} files:", style="bold"), Text()]
    for e in files:
        p = e.get("path", "<missing>")
        content = e.get("content", "")
        preview = content[:300] + ("..." if len(content) > 300 else "")
        parts.append(Text(p, style="bold dim"))
        parts.append(_format_diff("", preview))
        parts.append(Text())
    if not _ask_permission("Write Files", Group(*parts)):
        return f"Permission denied: batch write of {len(files)} files"

    results = []
    errors = []
    for entry in files:
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
    if not _ask_permission("Edit File", Group(header, Text(), diff)):
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
    if not _ask_permission("Edit Files", Group(*parts)):
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
            if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(filename, pattern):
                matches.append(rel_path)

    if not matches:
        return f"No files matched pattern: {pattern}"
        
    matches.sort()
    if len(matches) > 100:
        return "\n".join(matches[:100]) + f"\n... and {len(matches) - 100} more matches (use a more specific pattern to narrow results)"
    return "\n".join(matches)


def grep_search(pattern: str, directory: str = ".", file_glob: str = "") -> str:
    """Search for a regex pattern in file contents.

    Args:
        pattern: Regular expression pattern to search for.
        directory: Directory to search in. Defaults to current directory.
        file_glob: Optional glob to filter which files to search (e.g. '*.py').
    """
    import re

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    results = []
    for root, dirs, files in walk_filtered(directory):
        for filename in files:
            if file_glob and not fnmatch.fnmatch(filename, file_glob):
                continue
            filepath = os.path.join(root, filename)
            rel_path = os.path.relpath(filepath, directory)
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            results.append(f"{rel_path}:{i}: {line.rstrip()}")
            except (OSError, PermissionError):
                continue

    if not results:
        return f"No matches found for pattern: {pattern}"
    if len(results) > 30:
        return "\n".join(results[:30]) + f"\n... and {len(results) - 30} more matches (use a more specific pattern to narrow results)"
    return "\n".join(results)


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
    from arc.tools.gitignore import walk_filtered

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
    import sys
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
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return (
        text[:_TRUNCATE_KEEP]
        + f"\n\n[...truncated {len(text) - 2 * _TRUNCATE_KEEP:,} chars...]\n\n"
        + text[-_TRUNCATE_KEEP:]
    )


def _is_long_running(command: str) -> bool:
    """Detect commands that start servers or long-running processes."""
    cmd = command.strip()
    # Explicit background indicator
    if cmd.endswith("&"):
        return True
    return any(pattern in cmd for pattern in BACKGROUND_PATTERNS)


def run_command(command: str, timeout: int = 60, working_directory: str = "") -> str:
    """Execute a shell command and return its output. Use this for any system operation:
    git commands, running tests, installing packages, building projects, checking processes, etc.

    Args:
        command: The shell command to execute (e.g. 'git status', 'python -m pytest', 'npm install').
        timeout: Max seconds to wait for the command to finish. Defaults to 60.
        working_directory: Directory to run the command in. Defaults to current working directory.
    """
    cwd = working_directory or os.getcwd()

    # Long-running commands: start, capture initial output for a few seconds, then detach
    if _is_long_running(command):
        import threading
        import time

        startup_seconds = 5
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=cwd,
            )

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
        import sys as _sys
        popen_kwargs = dict(
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )
        if _sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

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


SAFE_COMMAND_PREFIXES = (
    # File/directory inspection
    "ls", "dir", "find", "tree", "cat", "head", "tail", "less", "more", "wc",
    "file", "stat", "du", "df",
    # Search
    "grep", "rg", "ag", "ack",
    # Git read-only
    "git status", "git log", "git diff", "git show", "git branch", "git tag",
    "git remote", "git stash list", "git blame", "git shortlog",
    # System info / navigation
    "cd", "echo", "pwd", "whoami", "which", "where", "type", "env", "printenv",
    "uname", "hostname", "ps", "top", "free", "uptime",
    # Language versions
    "python --version", "python3 --version", "node --version", "npm --version",
    "cargo --version", "go version", "java --version", "uv --version",
    # Sort/filter (typically piped)
    "sort", "uniq", "cut", "tr", "awk", "sed -n", "jq",
)


def _shell_split(command: str, separators: tuple[str, ...]) -> list[str] | None:
    """Split command by shell operators, respecting quotes.

    Returns list of parts if any separator found, None otherwise.
    """
    parts = []
    current = []
    in_single = False
    in_double = False
    i = 0
    chars = command
    while i < len(chars):
        c = chars[i]
        if c == "'" and not in_double:
            in_single = not in_single
            current.append(c)
        elif c == '"' and not in_single:
            in_double = not in_double
            current.append(c)
        elif not in_single and not in_double:
            matched = False
            for sep in separators:
                if chars[i:i+len(sep)] == sep:
                    parts.append("".join(current).strip())
                    current = []
                    i += len(sep)
                    matched = True
                    break
            if matched:
                continue
            current.append(c)
        else:
            current.append(c)
        i += 1
    if parts:  # at least one separator was found
        parts.append("".join(current).strip())
        return [p for p in parts if p]
    return None


def _is_safe_command(command: str) -> bool:
    """Check if a command is read-only and safe to run without permission."""
    cmd = command.strip()
    # Handle chained commands (&&, ;): safe only if ALL parts are safe
    parts = _shell_split(cmd, ("&&", ";"))
    if parts:
        return all(_is_safe_command(p) for p in parts)
    # Handle piped commands: safe only if ALL parts are safe
    parts = _shell_split(cmd, ("|",))
    if parts:
        return all(_is_safe_command(p) for p in parts)
    if any(cmd == prefix or cmd.startswith(prefix + " ") for prefix in SAFE_COMMAND_PREFIXES):
        return True
    return any(fnmatch.fnmatch(cmd, rule) for rule in _permission_rules)


def bash(command: str, timeout: int = 60, working_directory: str = "") -> str:
    """Execute a bash command. This is your primary tool for interacting with the system.
    Use it for:
    - Running tests: 'python -m pytest tests/'
    - Git operations: 'git status', 'git diff', 'git add', 'git commit'
    - Installing packages: 'pip install', 'npm install', 'uv add'
    - Building projects: 'make', 'cargo build', 'go build'
    - Checking system state: 'ls', 'ps', 'env', 'which'
    - Any other shell command

    Args:
        command: The bash command to execute.
        timeout: Max seconds to wait. Defaults to 60.
        working_directory: Directory to run in. Defaults to current working directory.
    """
    cwd = working_directory or os.getcwd()
    if not _is_safe_command(command):
        cmd_display = Group(
            Syntax(command, "bash", theme="monokai"),
            Text(f"cwd: {cwd}", style="dim"),
        )
        if not _ask_permission("Bash Command", cmd_display):
            return f"Permission denied: {command}"
    return run_command(command, timeout=timeout, working_directory=working_directory)


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
    """Search the web and return results. Use this to find information about frameworks,
    libraries, APIs, error messages, or any topic where online knowledge would help.

    Args:
        query: The search query (e.g. 'agno framework python', 'FastAPI websocket example').
        max_results: Maximum number of results to return (default 5).
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


def web_fetch(url: str, max_chars: int = 15000) -> str:
    """Fetch a URL and return its content as readable text.

    Use this to read web pages, GitHub repos/issues/PRs, documentation,
    API responses, or any publicly accessible URL.

    Args:
        url: The URL to fetch.
        max_chars: Maximum characters to return (default 15000) to avoid overwhelming context.
    """
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            resp = client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; arc-agent/0.1)",
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
    return text


_SUBAGENT_COUNTER = 0
_SUBAGENT_COUNTER_LOCK = threading.Lock()


def _next_subagent_id() -> int:
    global _SUBAGENT_COUNTER
    with _SUBAGENT_COUNTER_LOCK:
        _SUBAGENT_COUNTER += 1
        return _SUBAGENT_COUNTER


# Import new tools
from arc.tools.indexer import semantic_search
from arc.tools.ast_tools import code_structure, find_dependencies
from arc.tools.ranker import rank_files

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
    semantic_search,
    code_structure,
    find_dependencies,
    rank_files,
]


def delegate_task(task: str, context: str = "") -> str:
    """Delegate a task to a sub-agent that runs autonomously and returns the result.

    Use this when you need to:
    - Research a part of the codebase while continuing other work
    - Perform an independent subtask (e.g., fix file A while you work on file B)
    - Explore or gather information without cluttering your own context

    The sub-agent has the same tools as you (read, write, edit, search, bash, web_fetch)
    but cannot delegate further.

    When the model supports parallel tool calls, multiple delegate_task calls run concurrently.

    Args:
        task: Clear, specific description of what the sub-agent should do.
        context: Optional extra context (e.g., relevant file paths, constraints).
    """
    from agno.agent import Agent
    from agno.models.anthropic import Claude

    agent_id = _next_subagent_id()
    cwd = os.getcwd()

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
        model=Claude(id="claude-haiku-4-5-20251001", max_tokens=4096, cache_system_prompt=True),
        tools=_SUBAGENT_TOOLS,
        instructions=instructions,
        markdown=True,
    )

    try:
        result = sub.run(task, stream=False)
        if result and result.content:
            return f"[SubAgent-{agent_id}] {result.content}"
        return f"[SubAgent-{agent_id}] Task completed but no output was returned."
    except Exception as e:
        return f"[SubAgent-{agent_id}] Error: {e}"


# All tools as a list for easy import
ALL_TOOLS = [
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
    delegate_task,
    semantic_search,
    code_structure,
    find_dependencies,
    rank_files,
]
