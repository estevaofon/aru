"""Agent-facing LSP tools (Tier 2 #5).

Four semantic operations. Each returns a short, agent-readable string so
the LLM can integrate the result directly into reasoning. Failures (LSP
unavailable, server crashed, file unsupported) surface as explanatory
strings rather than raises — the model can fall back to grep-based tools.
"""

from __future__ import annotations

import asyncio
import logging
import os

from rich.console import Group
from rich.text import Text

from aru.lsp.client import LspRequestError
from aru.lsp.manager import get_lsp_manager
from aru.lsp.protocol import Location, Position, path_to_uri, uri_to_path
from aru.permissions import check_permission, consume_rejection_feedback

logger = logging.getLogger("aru.lsp")


async def _ensure_doc(client, path: str) -> tuple[str, str]:
    uri = path_to_uri(path)
    ext = os.path.splitext(path)[1].lower()
    lang_hint = {
        ".py": "python", ".pyi": "python",
        ".ts": "typescript", ".tsx": "typescriptreact",
        ".js": "javascript", ".jsx": "javascriptreact",
        ".rs": "rust", ".go": "go",
    }.get(ext, "plaintext")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise LspRequestError(f"cannot read {path}: {exc}") from exc
    await client.ensure_open(uri, lang_hint, text)
    return uri, text


async def _call(path: str, method: str, position: Position,
                extra_params: dict | None = None) -> str | list[Location] | dict | None:
    mgr = get_lsp_manager()
    if mgr is None:
        return "[LSP not configured. Add an \"lsp\" section to aru.json.]"
    client = await mgr.get_client_for(path)
    if client is None:
        health = mgr.health.get(mgr.language_for_file(path) or "")
        if health and health.state == "failed":
            return f"[LSP server unavailable: {health.last_error}]"
        return f"[LSP not configured for {path}]"
    try:
        uri, _ = await _ensure_doc(client, path)
    except LspRequestError as exc:
        return f"[LSP error: {exc}]"
    params = {
        "textDocument": {"uri": uri},
        "position": position.to_wire(),
    }
    if extra_params:
        params.update(extra_params)
    try:
        return await client.request(method, params)
    except LspRequestError as exc:
        return f"[LSP error: {exc}]"


def _format_locations(raw) -> str:
    if raw is None:
        return "No result."
    if isinstance(raw, str):
        return raw  # error message
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list) and not raw:
        return "No result."
    lines: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            loc = Location.from_wire(item)
        except KeyError:
            continue
        lines.append(loc.as_human())
    return "\n".join(lines) if lines else "No result."


# ── Tools ───────────────────────────────────────────────────────────

async def lsp_definition(file_path: str, line: int, column: int) -> str:
    """Locate the definition of the symbol at the given file:line:column.

    Line and column are 0-indexed (LSP convention). Returns a ``path:line:col``
    string (1-indexed for human readability) or an explanatory error. Use this
    instead of ``grep_search`` when you need to jump to where a name is defined
    in a codebase that may have multiple symbols with the same string.
    """
    raw = await _call(file_path, "textDocument/definition", Position(line, column))
    return _format_locations(raw)


async def lsp_references(file_path: str, line: int, column: int,
                         include_declaration: bool = True) -> str:
    """Find every reference to the symbol at file:line:column.

    Returns one ``path:line:col`` per line. ``include_declaration=False`` skips
    the declaration site itself (useful when you want only call sites).
    """
    raw = await _call(
        file_path, "textDocument/references", Position(line, column),
        extra_params={"context": {"includeDeclaration": bool(include_declaration)}},
    )
    return _format_locations(raw)


async def lsp_hover(file_path: str, line: int, column: int) -> str:
    """Return the type/docstring for the symbol at file:line:column.

    Condenses the LSP hover ``MarkupContent`` into plain text.
    """
    raw = await _call(file_path, "textDocument/hover", Position(line, column))
    if isinstance(raw, str):
        return raw
    if not raw or not isinstance(raw, dict):
        return "No hover info."
    contents = raw.get("contents")
    if contents is None:
        return "No hover info."
    if isinstance(contents, dict):
        return str(contents.get("value") or "").strip() or "No hover info."
    if isinstance(contents, list):
        pieces: list[str] = []
        for c in contents:
            if isinstance(c, dict):
                pieces.append(str(c.get("value") or ""))
            else:
                pieces.append(str(c))
        return "\n".join(p for p in pieces if p).strip() or "No hover info."
    return str(contents).strip()


# ── Rename code action (Tier 3 #4) ──────────────────────────────────

async def lsp_rename(file_path: str, line: int, column: int, new_name: str) -> str:
    """Rename the symbol at file:line:column (0-indexed) across the workspace.

    Uses the LSP ``textDocument/rename`` code action. The server returns a
    WorkspaceEdit describing every file to touch; this tool applies it
    atomically with in-memory rollback — if any file write fails, already-
    applied files are restored before the error is surfaced.

    Supports both the simple ``changes`` format (used by pylsp) and the
    richer ``documentChanges`` format (used by typescript-language-server).
    ``documentChanges`` entries that are CreateFile / DeleteFile /
    RenameFile operations are logged and skipped — only TextDocumentEdit
    is applied, which covers symbol rename.

    Args:
        file_path: Absolute or ``ctx.cwd``-relative path of the file
            containing the symbol to rename.
        line: 0-indexed line of the symbol occurrence to anchor on.
        column: 0-indexed column of the symbol occurrence.
        new_name: New identifier. Must be a valid name for the language —
            if the server rejects it (reserved word, invalid characters)
            the error message comes back as a string.
    """
    mgr = get_lsp_manager()
    if mgr is None:
        return "[LSP not configured. Add an \"lsp\" section to aru.json.]"
    client = await mgr.get_client_for(file_path)
    if client is None:
        health = mgr.health.get(mgr.language_for_file(file_path) or "")
        if health and health.state == "failed":
            return f"[LSP server unavailable: {health.last_error}]"
        return f"[LSP not configured for {file_path}]"
    try:
        uri, _ = await _ensure_doc(client, file_path)
    except LspRequestError as exc:
        return f"[LSP error: {exc}]"
    try:
        edit = await client.request("textDocument/rename", {
            "textDocument": {"uri": uri},
            "position": Position(line, column).to_wire(),
            "newName": new_name,
        })
    except LspRequestError as exc:
        return f"[LSP error: {exc}]"
    if edit is None:
        return "No rename possible at that position."

    try:
        per_file_edits, skipped = _normalize_workspace_edit(edit)
    except ValueError as exc:
        return f"[Unsupported WorkspaceEdit: {exc}]"
    if not per_file_edits:
        return "No files to edit."

    # Gate on permission before touching any file — a rename rewrites every
    # referencing file across the workspace, so the user must approve the
    # full set. ``check_permission`` is sync and may open a TUI modal that
    # blocks on ``threading.Event``; calling it directly from this async tool
    # (running on the App loop) would deadlock the loop so the modal never
    # resolves. Hop to a worker thread — same pattern as ``bash``.
    paths = sorted({uri_to_path(uri) for uri in per_file_edits})
    preview = Group(
        Text(f"Rename symbol → {new_name!r} across {len(paths)} file(s):", style="bold"),
        *[Text(f"  ~ {p}") for p in paths],
    )
    if not await asyncio.to_thread(check_permission, "edit", paths, preview):
        feedback = consume_rejection_feedback()
        if feedback:
            return (
                f"PERMISSION DENIED by user: lsp_rename → {new_name!r}. "
                f"The user said: {feedback}\n"
                f"Follow the user's instructions instead of retrying."
            )
        return (
            f"PERMISSION DENIED by user: lsp_rename → {new_name!r}. Do NOT retry "
            f"this operation. Stop and ask the user for new instructions."
        )

    applied = _apply_workspace_edit(per_file_edits)
    if isinstance(applied, str):  # error path
        return applied

    summary_parts = [
        f"Renamed symbol → {new_name!r} across {len(applied)} file(s):",
        *[f"  {p}" for p in applied],
    ]
    if skipped:
        summary_parts.append(
            f"Skipped {len(skipped)} non-edit operation(s) "
            f"(create/delete/rename-file not supported in this stage)."
        )
    return "\n".join(summary_parts)


def _normalize_workspace_edit(edit: dict) -> tuple[dict[str, list[dict]], list[str]]:
    """Unify ``changes`` and ``documentChanges`` into ``{uri: [TextEdit]}``.

    Returns ``(per_file_edits, skipped_descriptions)``. Skipped entries
    are CreateFile / DeleteFile / RenameFile operations from the rich
    ``documentChanges`` format — we don't apply them in this stage.
    """
    per_file: dict[str, list[dict]] = {}
    skipped: list[str] = []

    has_changes_key = isinstance(edit, dict) and "changes" in edit
    has_docchanges_key = isinstance(edit, dict) and "documentChanges" in edit

    simple = edit.get("changes") if isinstance(edit, dict) else None
    if isinstance(simple, dict):
        for uri, text_edits in simple.items():
            if isinstance(text_edits, list):
                per_file.setdefault(uri, []).extend(text_edits)

    rich = edit.get("documentChanges") if isinstance(edit, dict) else None
    if isinstance(rich, list):
        for entry in rich:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if kind in ("create", "delete", "rename"):
                skipped.append(f"{kind}:{entry.get('uri') or entry.get('oldUri')}")
                logger.warning("lsp_rename skipping %s op: %s", kind, entry)
                continue
            td = entry.get("textDocument")
            text_edits = entry.get("edits")
            if not isinstance(td, dict) or not isinstance(text_edits, list):
                continue
            uri = td.get("uri")
            if not uri:
                continue
            per_file.setdefault(uri, []).extend(text_edits)

    # Empty `changes: {}` or `documentChanges: []` is a legitimate "no
    # files to edit" response. Only raise when NEITHER key is present
    # (the server gave us a WorkspaceEdit shape we don't understand).
    if not has_changes_key and not has_docchanges_key:
        raise ValueError("neither 'changes' nor 'documentChanges' present")
    return per_file, skipped


def _apply_workspace_edit(per_file_edits: dict[str, list[dict]]) -> list[str] | str:
    """Apply *per_file_edits* atomically; return applied paths or an error string.

    Phase 1: read every file and keep original contents in memory.
    Phase 2: for each file, apply text-edits in reverse order of start
    offset so earlier edits don't shift later offsets. Write and record.
    On failure anywhere in phase 2: restore all previously-written files
    from the phase-1 backups and return a structured error string.
    """
    from aru.tools._shared import _checkpoint_file, _notify_file_mutation

    # Phase 1: read + backup
    backups: dict[str, str] = {}
    for uri in per_file_edits:
        path = uri_to_path(uri)
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                backups[path] = f.read()
        except FileNotFoundError:
            return f"[LSP rename failed: {path} not found]"
        except OSError as exc:
            return f"[LSP rename failed: cannot read {path}: {exc}]"

    # Phase 2: apply
    applied: list[str] = []
    try:
        for uri, text_edits in per_file_edits.items():
            path = uri_to_path(uri)
            _checkpoint_file(path)
            original = backups[path]
            try:
                new_text = _apply_text_edits(original, text_edits)
            except ValueError as exc:
                raise RuntimeError(f"{path}: {exc}") from exc
            if new_text == original:
                continue
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)
            applied.append(path)
            _notify_file_mutation(path=path, mutation_type="rename")
    except Exception as exc:
        # Rollback everything we've written so far
        for p in applied:
            try:
                with open(p, "w", encoding="utf-8", newline="") as f:
                    f.write(backups[p])
            except OSError:
                pass
        return f"[LSP rename failed mid-apply, rolled back {len(applied)} file(s): {exc}]"

    return applied


def _apply_text_edits(text: str, text_edits: list[dict]) -> str:
    """Apply a list of LSP TextEdits to *text*.

    Sorts edits by start offset descending so earlier edits don't shift
    later ones. Raises ``ValueError`` if any range is out-of-bounds.
    """
    offset_map = _build_line_offset_map(text)

    def _edit_start_offset(edit: dict) -> int:
        rng = edit.get("range") or {}
        start = rng.get("start") or {}
        return _position_to_offset(offset_map, start)

    def _edit_end_offset(edit: dict) -> int:
        rng = edit.get("range") or {}
        end = rng.get("end") or {}
        return _position_to_offset(offset_map, end)

    # Sort descending by start so later offsets apply first
    sorted_edits = sorted(text_edits, key=_edit_start_offset, reverse=True)
    result = text
    for edit in sorted_edits:
        start_off = _edit_start_offset(edit)
        end_off = _edit_end_offset(edit)
        if start_off < 0 or end_off < start_off or end_off > len(result):
            raise ValueError(f"edit range out of bounds: {edit.get('range')}")
        new_text = edit.get("newText") or ""
        result = result[:start_off] + new_text + result[end_off:]
    return result


def _build_line_offset_map(text: str) -> list[int]:
    """Return a list where index i is the char offset of line i's start."""
    offsets = [0]
    for idx, ch in enumerate(text):
        if ch == "\n":
            offsets.append(idx + 1)
    return offsets


def _position_to_offset(line_offsets: list[int], position: dict) -> int:
    line = int(position.get("line", 0))
    char = int(position.get("character", 0))
    if line < 0 or line >= len(line_offsets):
        return -1
    return line_offsets[line] + char


async def lsp_diagnostics(file_path: str) -> str:
    """Current errors/warnings for *file_path*, as reported by the LSP server.

    Relies on the server's ``textDocument/publishDiagnostics`` push — results
    reflect whatever the server has sent so far. The client auto-opens the
    document on first access, so most servers populate diagnostics soon after.
    """
    mgr = get_lsp_manager()
    if mgr is None:
        return "[LSP not configured.]"
    client = await mgr.get_client_for(file_path)
    if client is None:
        return f"[LSP not available for {file_path}]"
    try:
        await _ensure_doc(client, file_path)
    except LspRequestError as exc:
        return f"[LSP error: {exc}]"
    uri = path_to_uri(file_path)
    diags = client.diagnostics_for(uri)
    if not diags:
        return "No diagnostics."
    lines: list[str] = []
    sev_names = {1: "error", 2: "warning", 3: "info", 4: "hint"}
    for d in diags:
        try:
            rng = d.get("range", {}).get("start", {})
            line = int(rng.get("line", 0)) + 1
            col = int(rng.get("character", 0)) + 1
            severity = sev_names.get(int(d.get("severity", 1)), "error")
            source = d.get("source", "")
            msg = d.get("message", "")
            prefix = f"{file_path}:{line}:{col}"
            tag = f"[{severity}]"
            if source:
                tag = f"[{severity}:{source}]"
            lines.append(f"{prefix} {tag} {msg}")
        except Exception:  # pragma: no cover — defensive
            continue
    return "\n".join(lines) if lines else "No diagnostics."
