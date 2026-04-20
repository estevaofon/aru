"""Atomic multi-file patch application — Tier 2 Stage 2.

Parses the stripped-down diff envelope documented in ``apply_patch_prompt.txt``
and applies it transactionally: the entire patch is validated first (no disk
writes) and then each operation is executed with an in-memory rollback log so
a failure in the middle reverts every step already taken.

Compared with ``edit_files(edits=[...])``:

- Atomic: failure at operation N rolls back operations 1..N-1.
- First-class support for Add/Delete/Move, not just in-place edits.
- Errors surface as structured messages including the offending hunk index.
- Integrates with ``ctx.checkpoint_manager.track_edit`` so ``/undo`` still
  reverses a *successful* apply at end-of-turn.

Not included in this stage (future work):
- Auto-formatting post-apply (waits on a format service)
- LSP diagnostics round-trip (covered in Stage 5)
- Interactive per-hunk approval UI
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from aru.tools._shared import _checkpoint_file, _notify_file_mutation


# ── Exceptions ────────────────────────────────────────────────────────

class PatchError(Exception):
    """Base for all apply_patch errors."""


class PatchParseError(PatchError):
    """Envelope or operation header malformed."""


class PatchValidationError(PatchError):
    """Patch parsed fine but does not apply cleanly to the current tree.

    Raised BEFORE any disk mutation — caller can trust that no files changed.
    """


class PatchApplyError(PatchError):
    """Disk-level failure while applying an already-validated patch.

    Raised AFTER rollback completes so the caller sees a clean state.
    """


# ── Patch data model ─────────────────────────────────────────────────

@dataclass
class Hunk:
    """A contiguous change region in an Update File operation."""

    anchor: str = ""
    # Lines in the form [("-" | "+" | " ", text)].
    lines: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class AddFile:
    path: str
    content: str

    @property
    def target_path(self) -> str:
        return self.path


@dataclass
class DeleteFile:
    path: str

    @property
    def target_path(self) -> str:
        return self.path


@dataclass
class UpdateFile:
    path: str
    move_to: str | None = None
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def target_path(self) -> str:
        return self.path


FileOp = AddFile | DeleteFile | UpdateFile


@dataclass
class Patch:
    operations: list[FileOp] = field(default_factory=list)


# ── Parser ───────────────────────────────────────────────────────────

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_ADD = re.compile(r"^\*\*\* Add File:\s*(.+)$")
_DEL = re.compile(r"^\*\*\* Delete File:\s*(.+)$")
_UPD = re.compile(r"^\*\*\* Update File:\s*(.+)$")
_MOVE = re.compile(r"^\*\*\* Move to:\s*(.+)$")


def parse_patch(text: str) -> Patch:
    """Parse the *** Begin Patch / *** End Patch envelope.

    Raises PatchParseError for malformed structure (missing envelope,
    unexpected headers, Add File lines that aren't `+`-prefixed, etc.).
    """
    if not text or _BEGIN not in text or _END not in text:
        raise PatchParseError(
            "Patch must be wrapped in *** Begin Patch / *** End Patch markers."
        )

    # Keep only the body between markers.
    body = text.split(_BEGIN, 1)[1].split(_END, 1)[0]
    lines = body.splitlines()
    # Trim leading / trailing blank lines.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    patch = Patch()
    i = 0
    n = len(lines)

    def _is_op_header(line: str) -> bool:
        return bool(_ADD.match(line) or _DEL.match(line) or _UPD.match(line))

    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        m = _ADD.match(line)
        if m:
            path = m.group(1).strip()
            i += 1
            body_lines: list[str] = []
            while i < n and not _is_op_header(lines[i]):
                bl = lines[i]
                if bl.startswith("+"):
                    body_lines.append(bl[1:])
                elif not bl.strip():
                    # Tolerate blank separator BEFORE next op but reject
                    # blank lines in the middle of an add body.
                    # We resolve this by peeking: if the next non-blank line
                    # is an op header, treat blank as separator; else fail.
                    j = i + 1
                    while j < n and not lines[j].strip():
                        j += 1
                    if j >= n or _is_op_header(lines[j]):
                        i = j
                        break
                    raise PatchParseError(
                        f"Add File {path!r} body must have every line prefixed with '+'."
                    )
                else:
                    raise PatchParseError(
                        f"Add File {path!r} body must have every line prefixed with '+' "
                        f"(got: {bl!r})."
                    )
                i += 1
            content = "\n".join(body_lines)
            if body_lines:
                content += "\n"
            patch.operations.append(AddFile(path=path, content=content))
            continue

        m = _DEL.match(line)
        if m:
            path = m.group(1).strip()
            patch.operations.append(DeleteFile(path=path))
            i += 1
            continue

        m = _UPD.match(line)
        if m:
            path = m.group(1).strip()
            i += 1
            move_to: str | None = None
            if i < n:
                mv = _MOVE.match(lines[i])
                if mv:
                    move_to = mv.group(1).strip()
                    i += 1
            hunks: list[Hunk] = []
            current: Hunk | None = None
            while i < n and not _is_op_header(lines[i]):
                hl = lines[i]
                if hl.startswith("@@"):
                    anchor = hl[2:].strip()
                    current = Hunk(anchor=anchor)
                    hunks.append(current)
                elif not hl.strip():
                    # Blank line inside hunk = context blank line
                    if current is not None:
                        current.lines.append((" ", ""))
                elif hl[:1] in ("+", "-", " "):
                    if current is None:
                        # No `@@` yet — implicit anchor ""
                        current = Hunk(anchor="")
                        hunks.append(current)
                    current.lines.append((hl[0], hl[1:]))
                else:
                    # Any other leading char is invalid INSIDE a hunk.
                    raise PatchParseError(
                        f"Update File {path!r}: hunk line must start with "
                        f"'+', '-', ' ', or '@@' (got: {hl!r})."
                    )
                i += 1
            if not hunks and move_to is None:
                raise PatchParseError(
                    f"Update File {path!r} has no hunks and no Move to:."
                )
            patch.operations.append(UpdateFile(path=path, move_to=move_to, hunks=hunks))
            continue

        raise PatchParseError(f"Unexpected line in patch: {line!r}")

    if not patch.operations:
        raise PatchParseError("Patch contains no operations.")
    return patch


# ── Validation (no disk mutation) ────────────────────────────────────

def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def validate(patch: Patch, root: str | None = None) -> None:
    """Verify the patch will apply cleanly. Raises PatchValidationError on any issue.

    Runs purely against the filesystem as it currently stands — no mutation.
    Context (` `) and removal (`-`) lines in hunks must match the file verbatim.
    """
    if root is None:
        from aru.runtime import get_cwd as _get_cwd
        root = _get_cwd()

    for idx, op in enumerate(patch.operations):
        abs_path = os.path.abspath(os.path.join(root, op.target_path))

        if isinstance(op, AddFile):
            if os.path.exists(abs_path):
                raise PatchValidationError(
                    f"Op {idx}: Add File {op.path!r} — target already exists."
                )
            continue

        if isinstance(op, DeleteFile):
            if not os.path.isfile(abs_path):
                raise PatchValidationError(
                    f"Op {idx}: Delete File {op.path!r} — file does not exist."
                )
            continue

        if isinstance(op, UpdateFile):
            if not os.path.isfile(abs_path):
                raise PatchValidationError(
                    f"Op {idx}: Update File {op.path!r} — file does not exist."
                )
            if op.move_to is not None:
                move_abs = os.path.abspath(os.path.join(root, op.move_to))
                if os.path.exists(move_abs) and os.path.abspath(move_abs) != abs_path:
                    raise PatchValidationError(
                        f"Op {idx}: Update File {op.path!r} — Move to target "
                        f"{op.move_to!r} already exists."
                    )
            original = _read_text(abs_path)
            # Apply hunks in simulation; any mismatch raises.
            try:
                _apply_hunks(original, op.hunks)
            except PatchValidationError as exc:
                raise PatchValidationError(
                    f"Op {idx}: Update File {op.path!r} — {exc}"
                )


def _apply_hunks(original: str, hunks: list[Hunk]) -> str:
    """Return *original* with every hunk applied, or raise PatchValidationError.

    Strategy: each hunk's `-`+` `-tagged lines form the expected "before" block;
    we locate it in the current text (left-to-right, after the previous hunk's
    end) and replace with the `+`+` `-tagged "after" block.
    """
    text = original
    cursor = 0
    for idx, hunk in enumerate(hunks):
        before_lines = [t for tag, t in hunk.lines if tag in ("-", " ")]
        after_lines = [t for tag, t in hunk.lines if tag in ("+", " ")]
        if not hunk.lines:
            raise PatchValidationError(f"hunk {idx} has no body.")

        # Find the before block in text[cursor:]. If anchor provided, prefer
        # matches after the anchor occurrence.
        search_start = cursor
        if hunk.anchor:
            anchor_pos = text.find(hunk.anchor, cursor)
            if anchor_pos != -1:
                search_start = anchor_pos

        before_block = "\n".join(before_lines)
        after_block = "\n".join(after_lines)

        match_pos = _locate_block(text, before_block, search_start)
        if match_pos == -1:
            raise PatchValidationError(
                f"hunk {idx} context/removal lines do not match the file "
                f"(anchor: {hunk.anchor!r})"
            )

        # Replace — preserve terminating newline if we consumed one.
        end_pos = match_pos + len(before_block)
        text = text[:match_pos] + after_block + text[end_pos:]
        cursor = match_pos + len(after_block)
    return text


def _locate_block(haystack: str, needle: str, start: int = 0) -> int:
    """Locate *needle* in *haystack[start:]* on line boundaries.

    Accepts trailing-whitespace variations in the file as long as the block
    matches after `splitlines`. Returns the starting offset or -1.
    """
    if not needle:
        return start
    # Fast path: exact substring
    direct = haystack.find(needle, start)
    if direct != -1:
        return direct

    # Line-based fallback: compare block lines against haystack lines.
    needle_lines = needle.split("\n")
    hay_lines = haystack.split("\n")

    # Map from line index to char offset
    offsets = [0]
    for line in hay_lines:
        offsets.append(offsets[-1] + len(line) + 1)  # +1 for the split \n

    # Find starting line corresponding to `start`
    line_cursor = 0
    while line_cursor + 1 < len(offsets) and offsets[line_cursor + 1] <= start:
        line_cursor += 1

    nlen = len(needle_lines)
    for i in range(line_cursor, len(hay_lines) - nlen + 1):
        if all(hay_lines[i + j].rstrip() == needle_lines[j].rstrip()
               for j in range(nlen)):
            return offsets[i]
    return -1


# ── Apply (with rollback) ────────────────────────────────────────────

@dataclass
class _RollbackAction:
    kind: str                      # "write" | "delete" | "move"
    path: str
    original_content: str | None = None   # for write / delete rollback
    other_path: str | None = None         # for move rollback


def apply_patch_text(patch_text: str, root: str | None = None) -> str:
    """Parse, validate, and apply *patch_text* atomically.

    Returns a human-readable summary (list of operations + counts). Raises
    PatchParseError / PatchValidationError / PatchApplyError on failure. By
    the time a PatchApplyError is raised, any partial changes have been
    reverted.
    """
    patch = parse_patch(patch_text)
    validate(patch, root=root)

    if root is None:
        from aru.runtime import get_cwd as _get_cwd
        root = _get_cwd()
    applied: list[_RollbackAction] = []

    try:
        for idx, op in enumerate(patch.operations):
            abs_path = os.path.abspath(os.path.join(root, op.target_path))

            if isinstance(op, AddFile):
                _checkpoint_file(abs_path)
                os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
                with open(abs_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(op.content)
                applied.append(_RollbackAction(kind="delete", path=abs_path))

            elif isinstance(op, DeleteFile):
                _checkpoint_file(abs_path)
                pre = _read_text(abs_path)
                os.unlink(abs_path)
                applied.append(_RollbackAction(
                    kind="write", path=abs_path, original_content=pre,
                ))

            elif isinstance(op, UpdateFile):
                _checkpoint_file(abs_path)
                pre = _read_text(abs_path)
                new_text = _apply_hunks(pre, op.hunks)
                with open(abs_path, "w", encoding="utf-8", newline="") as f:
                    f.write(new_text)
                applied.append(_RollbackAction(
                    kind="write", path=abs_path, original_content=pre,
                ))
                if op.move_to is not None:
                    move_abs = os.path.abspath(os.path.join(root, op.move_to))
                    if os.path.abspath(move_abs) != abs_path:
                        os.makedirs(os.path.dirname(move_abs) or ".", exist_ok=True)
                        _checkpoint_file(move_abs)
                        shutil.move(abs_path, move_abs)
                        applied.append(_RollbackAction(
                            kind="move", path=move_abs, other_path=abs_path,
                        ))
    except Exception as exc:
        _rollback(applied)
        raise PatchApplyError(
            f"apply_patch failed at operation {idx}: {exc} "
            f"({len(applied)} operations rolled back)"
        ) from exc

    _notify_file_mutation()
    return _summarise(patch)


def _rollback(actions: list[_RollbackAction]) -> None:
    """Undo the recorded actions in reverse order. Best-effort, never raises."""
    for action in reversed(actions):
        try:
            if action.kind == "write":
                if action.original_content is None:
                    if os.path.exists(action.path):
                        os.unlink(action.path)
                else:
                    os.makedirs(os.path.dirname(action.path) or ".", exist_ok=True)
                    with open(action.path, "w", encoding="utf-8", newline="") as f:
                        f.write(action.original_content)
            elif action.kind == "delete":
                if os.path.exists(action.path):
                    os.unlink(action.path)
            elif action.kind == "move":
                # Undo a rename (moved from other_path -> path)
                if action.other_path and os.path.exists(action.path):
                    shutil.move(action.path, action.other_path)
        except OSError:
            pass


def _summarise(patch: Patch) -> str:
    added = sum(1 for op in patch.operations if isinstance(op, AddFile))
    deleted = sum(1 for op in patch.operations if isinstance(op, DeleteFile))
    updated = sum(1 for op in patch.operations if isinstance(op, UpdateFile))
    moved = sum(1 for op in patch.operations
                if isinstance(op, UpdateFile) and op.move_to)
    lines: list[str] = [
        f"apply_patch: {added} added, {updated} updated "
        f"({moved} moved), {deleted} deleted."
    ]
    for op in patch.operations:
        if isinstance(op, AddFile):
            lines.append(f"  + {op.path}")
        elif isinstance(op, DeleteFile):
            lines.append(f"  - {op.path}")
        elif isinstance(op, UpdateFile):
            if op.move_to:
                lines.append(f"  ~ {op.path} → {op.move_to}")
            else:
                lines.append(f"  ~ {op.path}")
    return "\n".join(lines)


# ── Agent-facing tool ────────────────────────────────────────────────

# Docstring loaded from sibling .txt file on import so the LLM-facing
# schema mirrors the documented format without duplication.
_PROMPT_PATH = Path(__file__).with_name("apply_patch_prompt.txt")
try:
    _PROMPT_TEXT = _PROMPT_PATH.read_text(encoding="utf-8")
except OSError:
    _PROMPT_TEXT = "Apply a multi-file patch atomically (see apply_patch_prompt.txt)."


def apply_patch(patch: str) -> str:
    try:
        return apply_patch_text(patch)
    except PatchParseError as exc:
        return f"Parse error: {exc}"
    except PatchValidationError as exc:
        return f"Validation error (no files modified): {exc}"
    except PatchApplyError as exc:
        return f"Apply error: {exc}"


apply_patch.__doc__ = _PROMPT_TEXT
