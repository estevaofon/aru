"""Custom tools for codebase exploration and manipulation."""

import fnmatch
import os
import subprocess


def read_file(file_path: str) -> str:
    """Read the contents of a file.

    Args:
        file_path: Path to the file to read (absolute or relative to working directory).
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
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
    for root, dirs, files in os.walk(directory):
        # Skip hidden and common ignored directories
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git", "venv", ".venv")]
        for filename in files:
            filepath = os.path.join(root, filename)
            rel_path = os.path.relpath(filepath, directory)
            if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(filename, pattern):
                matches.append(rel_path)

    if not matches:
        return f"No files matched pattern: {pattern}"
    return "\n".join(sorted(matches))


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
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git", "venv", ".venv")]
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
    if len(results) > 100:
        return "\n".join(results[:100]) + f"\n... and {len(results) - 100} more matches"
    return "\n".join(results)


def list_directory(directory: str = ".") -> str:
    """List files and directories in the given path.

    Args:
        directory: Directory to list. Defaults to current directory.
    """
    try:
        entries = os.listdir(directory)
        result = []
        for entry in sorted(entries):
            full_path = os.path.join(directory, entry)
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


def run_command(command: str, timeout: int = 120, working_directory: str = "") -> str:
    """Execute a shell command and return its output. Use this for any system operation:
    git commands, running tests, installing packages, building projects, checking processes, etc.

    Args:
        command: The shell command to execute (e.g. 'git status', 'python -m pytest', 'npm install').
        timeout: Max seconds to wait for the command to finish. Defaults to 120.
        working_directory: Directory to run the command in. Defaults to current working directory.
    """
    cwd = working_directory or os.getcwd()
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )
        stdout, stderr = process.communicate(timeout=timeout)

        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"STDERR:\n{stderr}")
        if process.returncode != 0:
            parts.append(f"Exit code: {process.returncode}")

        return "\n".join(parts).strip() or "(no output)"
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return f"Error: Command timed out after {timeout} seconds. Consider increasing the timeout."
    except Exception as e:
        return f"Error running command: {e}"


def bash(command: str, timeout: int = 120, working_directory: str = "") -> str:
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
        timeout: Max seconds to wait. Defaults to 120.
        working_directory: Directory to run in. Defaults to current working directory.
    """
    return run_command(command, timeout=timeout, working_directory=working_directory)


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
]
