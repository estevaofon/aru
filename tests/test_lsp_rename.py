"""Stage 4 Tier 3: LSP rename code action.

Exercises WorkspaceEdit application with both ``changes`` (pylsp-style) and
``documentChanges`` (typescript-language-server-style) formats, plus
rollback on mid-apply failure.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from aru.lsp.manager import LspManager, LspServerHealth, set_lsp_manager
from aru.lsp.protocol import path_to_uri
from aru.tools.lsp import (
    _apply_text_edits,
    _normalize_workspace_edit,
    lsp_rename,
)


# ── Text-edit unit tests ─────────────────────────────────────────────

def test_apply_text_edits_single_replacement():
    original = "def foo():\n    return 1\n"
    edits = [{
        "range": {"start": {"line": 0, "character": 4},
                  "end": {"line": 0, "character": 7}},
        "newText": "bar",
    }]
    assert _apply_text_edits(original, edits) == "def bar():\n    return 1\n"


def test_apply_text_edits_multiple_reverse_order_safe():
    """Edits sorted by offset descending so early edits don't shift late ones."""
    original = "aaa BBB ccc DDD eee\n"
    edits = [
        # Replace "aaa" at 0-3
        {"range": {"start": {"line": 0, "character": 0},
                   "end":   {"line": 0, "character": 3}},
         "newText": "XXXX"},
        # Replace "DDD" at 12-15 (same line)
        {"range": {"start": {"line": 0, "character": 12},
                   "end":   {"line": 0, "character": 15}},
         "newText": "YY"},
    ]
    # XXXX BBB ccc YY eee
    assert _apply_text_edits(original, edits) == "XXXX BBB ccc YY eee\n"


def test_apply_text_edits_out_of_bounds_raises():
    with pytest.raises(ValueError, match="out of bounds"):
        _apply_text_edits("short", [{
            "range": {"start": {"line": 10, "character": 0},
                      "end":   {"line": 10, "character": 3}},
            "newText": "x",
        }])


# ── Normalizer ───────────────────────────────────────────────────────

def test_normalize_simple_changes_format():
    edit = {
        "changes": {
            "file:///a.py": [{"range": {}, "newText": "x"}],
            "file:///b.py": [{"range": {}, "newText": "y"}],
        }
    }
    per_file, skipped = _normalize_workspace_edit(edit)
    assert set(per_file.keys()) == {"file:///a.py", "file:///b.py"}
    assert skipped == []


def test_normalize_documentChanges_format():
    edit = {
        "documentChanges": [
            {
                "textDocument": {"uri": "file:///a.ts", "version": 1},
                "edits": [{"range": {}, "newText": "x"}],
            },
            {
                "textDocument": {"uri": "file:///b.ts", "version": 1},
                "edits": [{"range": {}, "newText": "y"}],
            },
        ]
    }
    per_file, skipped = _normalize_workspace_edit(edit)
    assert len(per_file) == 2
    assert skipped == []


def test_normalize_mixed_documentChanges_skips_non_edits():
    edit = {
        "documentChanges": [
            {
                "textDocument": {"uri": "file:///a.ts", "version": 1},
                "edits": [{"range": {}, "newText": "x"}],
            },
            {"kind": "create", "uri": "file:///new.ts"},
            {"kind": "rename", "oldUri": "file:///old.ts", "newUri": "file:///new2.ts"},
        ]
    }
    per_file, skipped = _normalize_workspace_edit(edit)
    assert len(per_file) == 1
    assert len(skipped) == 2
    assert any("create" in s for s in skipped)
    assert any("rename" in s for s in skipped)


def test_normalize_empty_raises():
    with pytest.raises(ValueError, match="neither"):
        _normalize_workspace_edit({})


# ── End-to-end rename with FakeClient ───────────────────────────────

class FakeClient:
    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.scripted: dict[str, object] = {}
        self.opened_docs: set[str] = set()
        self.diagnostics: dict[str, list] = {}

    async def ensure_open(self, uri: str, language_id: str, text: str) -> None:
        self.opened_docs.add(uri)

    async def request(self, method, params):
        self.requests.append((method, params))
        return self.scripted.get(method)

    def diagnostics_for(self, uri: str) -> list[dict]:
        return list(self.diagnostics.get(uri, []))


@pytest.fixture
def rename_env(tmp_path):
    a = tmp_path / "a.py"
    a.write_text("def foo():\n    return 1\n")
    b = tmp_path / "b.py"
    b.write_text("from a import foo\n\nfoo()\n")

    mgr = LspManager(config_lsp={"python": {"command": "pylsp"}}, root=str(tmp_path))
    fake = FakeClient()
    mgr.get_client_for = AsyncMock(return_value=fake)  # type: ignore[assignment]
    set_lsp_manager(mgr)
    return mgr, fake, a, b


@pytest.mark.asyncio
async def test_rename_applies_changes_format(rename_env):
    _mgr, fake, a, b = rename_env
    fake.scripted["textDocument/rename"] = {
        "changes": {
            path_to_uri(str(a)): [
                {"range": {"start": {"line": 0, "character": 4},
                           "end":   {"line": 0, "character": 7}},
                 "newText": "bar"},
            ],
            path_to_uri(str(b)): [
                {"range": {"start": {"line": 0, "character": 14},
                           "end":   {"line": 0, "character": 17}},
                 "newText": "bar"},
                {"range": {"start": {"line": 2, "character": 0},
                           "end":   {"line": 2, "character": 3}},
                 "newText": "bar"},
            ],
        }
    }
    result = await lsp_rename(str(a), 0, 5, "bar")
    assert "Renamed symbol" in result
    assert a.read_text() == "def bar():\n    return 1\n"
    assert b.read_text() == "from a import bar\n\nbar()\n"


@pytest.mark.asyncio
async def test_rename_applies_documentChanges_format(rename_env):
    _mgr, fake, a, b = rename_env
    fake.scripted["textDocument/rename"] = {
        "documentChanges": [
            {
                "textDocument": {"uri": path_to_uri(str(a)), "version": 1},
                "edits": [{
                    "range": {"start": {"line": 0, "character": 4},
                              "end":   {"line": 0, "character": 7}},
                    "newText": "bar",
                }],
            },
        ],
    }
    result = await lsp_rename(str(a), 0, 5, "bar")
    assert "Renamed symbol" in result
    assert a.read_text() == "def bar():\n    return 1\n"


@pytest.mark.asyncio
async def test_rename_null_response_reports_no_rename(rename_env):
    _mgr, fake, a, _b = rename_env
    fake.scripted["textDocument/rename"] = None
    result = await lsp_rename(str(a), 0, 5, "bar")
    assert "No rename possible" in result


@pytest.mark.asyncio
async def test_rename_no_files_message(rename_env):
    _mgr, fake, a, _b = rename_env
    fake.scripted["textDocument/rename"] = {"changes": {}}
    result = await lsp_rename(str(a), 0, 5, "bar")
    assert "No files to edit" in result


@pytest.mark.asyncio
async def test_rename_rolls_back_on_mid_apply_failure(rename_env, monkeypatch):
    """Force _apply_text_edits to fail on the 2nd file; first must be reverted."""
    _mgr, fake, a, b = rename_env
    original_a = a.read_text()
    original_b = b.read_text()

    fake.scripted["textDocument/rename"] = {
        "changes": {
            path_to_uri(str(a)): [{
                "range": {"start": {"line": 0, "character": 4},
                          "end":   {"line": 0, "character": 7}},
                "newText": "bar",
            }],
            path_to_uri(str(b)): [{
                "range": {"start": {"line": 0, "character": 14},
                          "end":   {"line": 0, "character": 17}},
                "newText": "bar",
            }],
        }
    }

    import aru.tools.lsp as mod
    real_apply = mod._apply_text_edits
    call_count = {"n": 0}

    def flaky(text, edits):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise ValueError("simulated disk failure")
        return real_apply(text, edits)

    monkeypatch.setattr(mod, "_apply_text_edits", flaky)

    result = await lsp_rename(str(a), 0, 5, "bar")
    assert "rolled back" in result
    # Both files restored to original
    assert a.read_text() == original_a
    assert b.read_text() == original_b


@pytest.mark.asyncio
async def test_rename_emits_file_changed_with_rename_type(rename_env):
    """Successful rename must emit file.changed with mutation_type='rename'."""
    from aru.plugins.manager import PluginManager
    from aru.runtime import get_ctx

    _mgr, fake, a, _b = rename_env
    fake.scripted["textDocument/rename"] = {
        "changes": {
            path_to_uri(str(a)): [{
                "range": {"start": {"line": 0, "character": 4},
                          "end":   {"line": 0, "character": 7}},
                "newText": "bar",
            }],
        }
    }

    mgr = PluginManager()
    mgr._loaded = True
    seen: list[dict] = []
    mgr.subscribe("file.changed", lambda payload: seen.append(payload))
    get_ctx().plugin_manager = mgr

    await lsp_rename(str(a), 0, 5, "bar")
    # Let fire-and-forget tasks settle
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert any(e.get("mutation_type") == "rename" for e in seen)


@pytest.mark.asyncio
async def test_rename_no_manager_reports_not_configured():
    set_lsp_manager(None)
    out = await lsp_rename("/tmp/x.py", 0, 0, "bar")
    assert "LSP not configured" in out


# ── Permission gate ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_denied_blocks_write(rename_env, monkeypatch):
    """A denied rename must touch no files — it rewrites the whole workspace."""
    import aru.tools.lsp as mod

    _mgr, fake, a, b = rename_env
    original_a, original_b = a.read_text(), b.read_text()
    fake.scripted["textDocument/rename"] = {
        "changes": {
            path_to_uri(str(a)): [{
                "range": {"start": {"line": 0, "character": 4},
                          "end":   {"line": 0, "character": 7}},
                "newText": "bar",
            }],
        }
    }
    monkeypatch.setattr(mod, "check_permission", lambda *a, **k: False)

    result = await mod.lsp_rename(str(a), 0, 5, "bar")
    assert "PERMISSION DENIED" in result
    assert a.read_text() == original_a
    assert b.read_text() == original_b


@pytest.mark.asyncio
async def test_rename_allowed_consults_edit_category(rename_env, monkeypatch):
    import aru.tools.lsp as mod

    _mgr, fake, a, _b = rename_env
    seen: list = []

    def fake_check(category, subjects, details):
        seen.append((category, subjects))
        return True

    monkeypatch.setattr(mod, "check_permission", fake_check)
    fake.scripted["textDocument/rename"] = {
        "changes": {
            path_to_uri(str(a)): [{
                "range": {"start": {"line": 0, "character": 4},
                          "end":   {"line": 0, "character": 7}},
                "newText": "bar",
            }],
        }
    }

    result = await mod.lsp_rename(str(a), 0, 5, "bar")
    assert "Renamed symbol" in result
    assert seen and seen[0][0] == "edit"


@pytest.mark.asyncio
async def test_rename_failed_server_surfaces_error(tmp_path):
    mgr = LspManager(config_lsp={"python": {"command": "pylsp"}}, root=str(tmp_path))
    mgr.health["python"] = LspServerHealth(
        name="python", state="failed", last_error="binary missing"
    )
    mgr.get_client_for = AsyncMock(return_value=None)  # type: ignore[assignment]
    set_lsp_manager(mgr)
    f = tmp_path / "x.py"; f.write_text("x\n")
    out = await lsp_rename(str(f), 0, 0, "bar")
    assert "unavailable" in out.lower()
