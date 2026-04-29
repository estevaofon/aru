"""Clickable ``path:line`` references in chat messages.

Detects file-path patterns in the rendered Text and stylises matched
spans with a Textual ``@click`` action. Clicking opens the file in
``$EDITOR`` (with a ``+<line>`` flag when the reference includes one).

Why deferred to ``finalize_render``: streaming buffers contain partial
paths character-by-character, so wrapping them as clickable links mid-
stream would produce a flicker of "almost-paths" the user could
mis-click. The author of the message has finished by the time
``finalize_render`` runs, so all paths are stable. Streaming
performance is unaffected.

Conservative regex — matches only paths with a recognised file
extension AND a slash/backslash OR an explicit line suffix. Naked
words like ``main.py`` (which could also be code prose) require at
least a directory component to count. Tunable in ``_PATH_RE`` below.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import Optional

from rich.style import Style
from rich.text import Text

logger = logging.getLogger("aru.tui.file_link")


# Recognise:
#   - ``aru/foo.py``                — relative posix path with extension
#   - ``aru/foo.py:42``             — same with line
#   - ``D:\proj\foo.py:42``         — windows absolute
#   - ``./scripts/run.sh``          — leading ./
#   - ``../parent/util.ts:7``
#
# Avoids matching:
#   - Bare words like ``main.py`` (no slash, no line suffix)
#   - URLs (``http://``)
#
# The ``(?:^|(?<=[\s(\[]))`` lookbehind ensures we don't grab paths
# embedded in larger identifiers (``my-foo.py`` shouldn't match if
# preceded by alphanumeric).
_PATH_RE = re.compile(
    r"""
    (?:^|(?<=[\s(\[`'\"]))                # boundary: start or after whitespace/brackets/quotes/backticks
    (?P<path>
        (?:[A-Za-z]:[\\/]?)?              # optional drive letter (Windows): D:  or  D:\
        (?:\.{1,2}[\\/])?                 # optional ./  ../
        (?:[\w.\-]+[\\/])+                # at least one directory component
        [\w.\-]+\.[A-Za-z][A-Za-z0-9]{0,5}  # filename with extension (1-6 letter ext)
    )
    (?::(?P<line>\d+))?                   # optional :line
    (?=$|[\s)\]:,.;`'\"])                 # boundary: end or punctuation
    """,
    re.VERBOSE,
)


def _is_url_match(text: str, start: int) -> bool:
    """Skip matches that are part of a URL like ``https://...``."""
    head = text[max(0, start - 8) : start].lower()
    return "://" in head or head.endswith("//")


def add_path_links(
    rich_text: Text,
    link_targets: list[tuple[str, Optional[int]]],
    action_name: str = "open_file",
) -> Text:
    """Stylise file-path matches in ``rich_text`` with ``@click`` actions.

    Mutates ``rich_text`` in place (also returns it for chaining) and
    appends each matched (path, line) tuple to ``link_targets``. The
    Textual click action is keyed by index into that list — strings
    with quotes / backslashes are tricky to escape inside the
    ``@click=open_file('...')`` markup, so we route by index instead.

    Returns the same Text object (callers that want a copy should
    ``rich_text.copy()`` first).
    """
    plain = rich_text.plain
    if not plain:
        return rich_text
    for match in _PATH_RE.finditer(plain):
        if _is_url_match(plain, match.start()):
            continue
        path = match.group("path")
        line = match.group("line")
        # Skip obvious false positives: a long sentence period like "I
        # mean.bar" would match if a slash precedes it. Require the
        # extension to be a known plausible code/text extension OR the
        # match to include a line number (which is the strongest signal).
        if not _has_known_extension(path) and not line:
            continue
        idx = len(link_targets)
        link_targets.append((path, int(line) if line else None))
        # Textual recognises ``@click`` in segment meta and routes the
        # click to the widget's ``action_<name>`` method. Underline so
        # the user sees a hint that the path is interactive.
        style = Style(
            underline=True,
            color="bright_cyan",
            meta={"@click": f"{action_name}({idx})"},
        )
        rich_text.stylize(style, match.start(), match.end())
    return rich_text


# Conservative allow-list. We err toward "paths only become clickable
# when likely to be code/text" rather than "every dotted thing in chat".
_KNOWN_EXTENSIONS: frozenset[str] = frozenset(
    {
        # source code
        "py", "pyi", "pyx", "ipynb",
        "js", "jsx", "mjs", "cjs", "ts", "tsx",
        "go", "rs", "rb", "php", "java", "kt", "kts", "scala", "swift",
        "c", "h", "cc", "cpp", "hpp", "cxx", "hxx", "m", "mm",
        "cs", "fs", "fsx", "vb",
        "sh", "bash", "zsh", "fish", "ps1", "psm1", "bat", "cmd",
        "lua", "pl", "pm", "ex", "exs", "erl", "hrl", "clj", "cljs",
        "sql", "tf", "hcl",
        # config / data
        "json", "jsonc", "json5", "yaml", "yml", "toml", "ini", "cfg",
        "xml", "html", "htm", "css", "scss", "sass", "less",
        "env", "lock",
        # docs
        "md", "mdx", "rst", "txt", "adoc",
    }
)


def _has_known_extension(path: str) -> bool:
    _, ext = os.path.splitext(path)
    return ext.lstrip(".").lower() in _KNOWN_EXTENSIONS


def open_in_editor(path: str, line: Optional[int] = None) -> bool:
    """Spawn ``$EDITOR`` against the given path. Returns True on launch.

    Resolution rules:

    * Absolute path → used as-is.
    * Relative path → resolved against ``aru.runtime.get_cwd()`` if
      available, else ``os.getcwd()``.
    * If the resolved path doesn't exist, we still spawn the editor
      against the resolved string — most editors handle "file not
      found" gracefully (open buffer, show message). Returning False
      would surprise users when paths are typed-but-not-yet-saved.

    Editor selection:

    * ``ARU_EDITOR``  (Aru-specific override) — wins.
    * ``VISUAL``      — POSIX convention for full-screen editors.
    * ``EDITOR``      — POSIX standard.
    * Platform fallbacks: ``code -g`` if VS Code is on PATH, then
      ``notepad`` (Windows) / ``open -t`` (macOS) / ``xdg-open``.

    Line-number support:

    * VS Code: ``code -g <path>:<line>``
    * Vim/Neovim/Emacs/Helix: ``<editor> +<line> <path>``
    * Generic: ``<editor> <path>`` (line dropped silently).
    """
    target = path
    if not os.path.isabs(target):
        try:
            from aru.runtime import get_cwd

            base = get_cwd()
        except Exception:
            base = os.getcwd()
        target = os.path.join(base, target)
    target = os.path.normpath(target)

    cmd = _build_editor_command(target, line)
    if not cmd:
        return False
    try:
        # ``Popen`` so we don't block the App loop. Detached: the
        # editor lives past the Python process if the user backgrounds
        # Aru. ``start_new_session=True`` avoids signal sharing.
        if sys.platform == "win32":
            # On Windows, ``DETACHED_PROCESS`` is the equivalent.
            DETACHED_PROCESS = 0x00000008
            subprocess.Popen(
                cmd,
                creationflags=DETACHED_PROCESS,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                cmd, start_new_session=True, close_fds=True
            )
        return True
    except Exception as exc:
        logger.debug("Editor launch failed: %s", exc)
        return False


def _build_editor_command(
    target: str, line: Optional[int]
) -> Optional[list[str]]:
    """Pick an editor and assemble argv with line-number flag if supported."""
    editor = (
        os.environ.get("ARU_EDITOR")
        or os.environ.get("VISUAL")
        or os.environ.get("EDITOR")
    )
    if editor:
        # Editor strings can include args (e.g. "code --wait"). Split.
        try:
            argv = shlex.split(editor, posix=(sys.platform != "win32"))
        except Exception:
            argv = [editor]
        return _augment_argv_with_line(argv, target, line)

    # No env-configured editor — try platform fallbacks.
    if shutil.which("code"):
        argv = ["code", "-g", f"{target}:{line}" if line else target]
        return argv
    if sys.platform == "win32":
        return ["notepad", target]
    if sys.platform == "darwin":
        return ["open", "-t", target]
    if shutil.which("xdg-open"):
        return ["xdg-open", target]
    return None


def _augment_argv_with_line(
    argv: list[str], target: str, line: Optional[int]
) -> list[str]:
    if line is None:
        return [*argv, target]
    head = (argv[0] or "").lower()
    # VS Code: ``code -g <path>:<line>`` (or ``code --goto``).
    if head.endswith("code") or head == "code":
        return [*argv, "-g", f"{target}:{line}"]
    # Sublime: ``subl <path>:<line>``.
    if head in ("subl", "subl.exe"):
        return [*argv, f"{target}:{line}"]
    # Vim / Neovim / Helix / Emacs / nano use ``+<line>``.
    if head in ("vim", "vim.exe", "nvim", "nvim.exe", "vi", "hx", "emacs", "emacsclient", "nano"):
        return [*argv, f"+{line}", target]
    # Generic: drop the line, log nothing — many editors will ignore
    # an unknown ``+<line>`` flag and open the buffer at line 1.
    return [*argv, target]
