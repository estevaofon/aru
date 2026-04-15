"""Diff rendering helpers for file-mutation tools.

Produces two kinds of diff output:
- ``_format_unified_diff`` returns a Rich ``Group`` with colored line numbers
  and hunk headers for permission prompts.
- ``_compact_diff`` returns a plain unified-diff string for the LLM context.
"""

from __future__ import annotations

import re

from rich.console import Group
from rich.text import Text


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_DIFF_STYLE_DEL = "white on #5f1f1f"
_DIFF_STYLE_ADD = "white on #1f5f1f"
_DIFF_STYLE_CTX = "default"
_DIFF_STYLE_GUTTER = "bright_black"
_DIFF_STYLE_GUTTER_DEL = "white on #3f0f0f"
_DIFF_STYLE_GUTTER_ADD = "white on #0f3f0f"
_DIFF_STYLE_HEADER = "cyan"


def _format_unified_diff(
    old_content: str,
    new_content: str,
    file_path: str = "",
    context_lines: int = 3,
    max_total_lines: int = 200,
) -> Group:
    """Render a unified diff of the full file before/after, with line numbers,
    hunk headers, and colored backgrounds for +/- lines.

    Handles new-file creation (empty old_content) and deletions (empty new_content).
    """
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()

    import difflib
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        n=context_lines, lineterm="",
    ))

    parts: list = []
    if file_path:
        parts.append(Text(file_path, style="bold"))
        parts.append(Text())

    if not diff:
        parts.append(Text("(no changes)", style="dim"))
        return Group(*parts)

    body = [l for l in diff if not l.startswith("---") and not l.startswith("+++")]

    adds = sum(1 for l in body if l.startswith("+"))
    dels = sum(1 for l in body if l.startswith("-"))
    parts.append(Text.assemble(
        (f"+{adds}", "green"),
        ("  ", ""),
        (f"-{dels}", "red"),
    ))
    parts.append(Text())

    old_no = new_no = 0
    shown = 0

    for idx, line in enumerate(body):
        if shown >= max_total_lines:
            remaining = len(body) - idx
            parts.append(Text(f"… {remaining} more diff lines …", style="dim italic"))
            break

        m = _HUNK_HEADER_RE.match(line)
        if m:
            old_no = int(m.group(1))
            new_no = int(m.group(2))
            if shown > 0:
                parts.append(Text())  # blank separator between hunks
            parts.append(Text(line, style=_DIFF_STYLE_HEADER))
            shown += 1
            continue

        if not line:
            continue

        marker = line[0]
        content = line[1:]

        if marker == "+":
            gutter = f"{'':>4} {new_no:>4} "
            row = Text.assemble(
                (gutter, _DIFF_STYLE_GUTTER_ADD),
                ("+ ", _DIFF_STYLE_ADD),
                (content, _DIFF_STYLE_ADD),
            )
            new_no += 1
        elif marker == "-":
            gutter = f"{old_no:>4} {'':>4} "
            row = Text.assemble(
                (gutter, _DIFF_STYLE_GUTTER_DEL),
                ("- ", _DIFF_STYLE_DEL),
                (content, _DIFF_STYLE_DEL),
            )
            old_no += 1
        else:
            gutter = f"{old_no:>4} {new_no:>4} "
            row = Text.assemble(
                (gutter, _DIFF_STYLE_GUTTER),
                ("  ", _DIFF_STYLE_CTX),
                (content, _DIFF_STYLE_CTX),
            )
            old_no += 1
            new_no += 1

        parts.append(row)
        shown += 1

    return Group(*parts)


def _compact_diff(old_string: str, new_string: str, file_path: str = "") -> str:
    """Generate a compact unified diff string for the LLM context.

    Returns only the changed lines (not the full file), saving tokens while
    giving the LLM enough context to continue working.
    """
    old_lines = old_string.splitlines(keepends=True)
    new_lines = new_string.splitlines(keepends=True)
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"

    import difflib
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=file_path, tofile=file_path,
        lineterm="",
    ))
    if not diff_lines:
        return ""
    MAX_DIFF_LINES = 40
    if len(diff_lines) > MAX_DIFF_LINES:
        return "\n".join(diff_lines[:MAX_DIFF_LINES]) + f"\n... ({len(diff_lines) - MAX_DIFF_LINES} more diff lines)"
    return "\n".join(diff_lines)
