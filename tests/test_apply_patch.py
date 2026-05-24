"""Stage 2 Tier 2: atomic apply_patch tool tests.

Covers:
- Parse + roundtrip of Add/Delete/Update operations
- Update with anchor-guided hunk placement
- Stale context (file changed since the patch was drafted) rejects cleanly
- Partial failure rolls back every previously-applied operation
- Move operation renames + preserves new contents
- Whole-patch validation happens BEFORE disk mutation
"""

import os
from pathlib import Path

import pytest

from aru.tools.apply_patch import (
    AddFile,
    DeleteFile,
    PatchApplyError,
    PatchParseError,
    PatchValidationError,
    UpdateFile,
    apply_patch,
    apply_patch_text,
    parse_patch,
)


# ── Parser tests ─────────────────────────────────────────────────────

def test_parse_rejects_missing_envelope():
    with pytest.raises(PatchParseError, match="Begin Patch"):
        parse_patch("+hello")


def test_parse_add_delete_update():
    text = """\
*** Begin Patch
*** Add File: new.txt
+hello
+world
*** Delete File: gone.txt
*** Update File: keep.py
@@ def foo():
-    pass
+    return 1
*** End Patch
"""
    patch = parse_patch(text)
    assert len(patch.operations) == 3
    assert isinstance(patch.operations[0], AddFile)
    assert patch.operations[0].path == "new.txt"
    assert patch.operations[0].content == "hello\nworld\n"
    assert isinstance(patch.operations[1], DeleteFile)
    assert patch.operations[1].path == "gone.txt"
    assert isinstance(patch.operations[2], UpdateFile)
    assert patch.operations[2].path == "keep.py"
    assert len(patch.operations[2].hunks) == 1
    assert patch.operations[2].hunks[0].anchor == "def foo():"


def test_parse_update_with_move():
    text = """\
*** Begin Patch
*** Update File: old.py
*** Move to: new.py
@@ def bar():
-    return 0
+    return 42
*** End Patch
"""
    patch = parse_patch(text)
    op = patch.operations[0]
    assert isinstance(op, UpdateFile)
    assert op.move_to == "new.py"


def test_parse_add_file_requires_plus_prefix():
    text = """\
*** Begin Patch
*** Add File: bad.txt
this has no plus
*** End Patch
"""
    with pytest.raises(PatchParseError, match="'\\+'"):
        parse_patch(text)


# ── Integration tests against a real tmpdir ──────────────────────────

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_add_file_creates_it(workspace):
    text = """\
*** Begin Patch
*** Add File: hello.txt
+Hello
+World
*** End Patch
"""
    result = apply_patch_text(text)
    assert (workspace / "hello.txt").read_text() == "Hello\nWorld\n"
    assert "1 added" in result


def test_delete_file_removes_it(workspace):
    p = workspace / "bye.txt"
    p.write_text("bye\n")
    text = """\
*** Begin Patch
*** Delete File: bye.txt
*** End Patch
"""
    result = apply_patch_text(text)
    assert not p.exists()
    assert "1 deleted" in result


def test_update_file_applies_hunk(workspace):
    p = workspace / "greet.py"
    p.write_text("def greet(name):\n    print('Hi')\n    return None\n")
    text = """\
*** Begin Patch
*** Update File: greet.py
@@ def greet(name):
-    print('Hi')
+    print(f'Hello, {name}!')
*** End Patch
"""
    apply_patch_text(text)
    assert "Hello, {name}!" in p.read_text()
    assert "'Hi'" not in p.read_text()


def test_update_with_move_renames(workspace):
    p = workspace / "old.py"
    p.write_text("def foo():\n    return 0\n")
    text = """\
*** Begin Patch
*** Update File: old.py
*** Move to: new.py
@@ def foo():
-    return 0
+    return 42
*** End Patch
"""
    apply_patch_text(text)
    assert not p.exists()
    assert (workspace / "new.py").read_text() == "def foo():\n    return 42\n"


def test_validation_rejects_stale_context_before_any_disk_write(workspace):
    # File exists but its content doesn't match the patch's context lines.
    (workspace / "stale.py").write_text("def bar():\n    return 7\n")
    # A file that the Add operation would have happily created.
    text = """\
*** Begin Patch
*** Update File: stale.py
@@ def foo():
-    pass
+    return 1
*** Add File: after.txt
+should not appear
*** End Patch
"""
    with pytest.raises(PatchValidationError):
        apply_patch_text(text)
    # After operation was NOT executed because validation blocks the whole patch.
    assert not (workspace / "after.txt").exists()
    # The stale.py file is untouched.
    assert (workspace / "stale.py").read_text() == "def bar():\n    return 7\n"


def test_apply_failure_rolls_back_prior_operations(workspace, monkeypatch):
    """Force a failure on the second operation and verify the first is reverted."""
    p1 = workspace / "a.txt"
    p1.write_text("AAA\n")
    p2 = workspace / "b.txt"
    p2.write_text("BBB\n")

    text = """\
*** Begin Patch
*** Update File: a.txt
@@ AAA
-AAA
+aaa
*** Update File: b.txt
@@ BBB
-BBB
+bbb
*** End Patch
"""
    # Parse + validate succeed; inject a failure right before writing b.txt.
    import aru.tools.apply_patch as mod
    real_apply_hunks = mod._apply_hunks
    calls = {"n": 0}

    def flaky(original, hunks):
        calls["n"] += 1
        if calls["n"] == 3:  # 2 during validate for b, 1 during apply for a, 2nd apply for b trips
            raise RuntimeError("disk exploded")
        return real_apply_hunks(original, hunks)

    monkeypatch.setattr(mod, "_apply_hunks", flaky)

    with pytest.raises(PatchApplyError):
        apply_patch_text(text)

    # a.txt must be back to its original content
    assert p1.read_text() == "AAA\n"
    # b.txt untouched
    assert p2.read_text() == "BBB\n"


def test_add_file_fails_if_target_exists(workspace):
    (workspace / "already.txt").write_text("oops\n")
    text = """\
*** Begin Patch
*** Add File: already.txt
+new
*** End Patch
"""
    with pytest.raises(PatchValidationError, match="already exists"):
        apply_patch_text(text)


def test_delete_file_fails_if_missing(workspace):
    text = """\
*** Begin Patch
*** Delete File: nope.txt
*** End Patch
"""
    with pytest.raises(PatchValidationError, match="does not exist"):
        apply_patch_text(text)


def test_move_target_collision_rejected(workspace):
    (workspace / "src.py").write_text("x\n")
    (workspace / "dst.py").write_text("y\n")
    text = """\
*** Begin Patch
*** Update File: src.py
*** Move to: dst.py
@@
-x
+z
*** End Patch
"""
    with pytest.raises(PatchValidationError, match="Move to target"):
        apply_patch_text(text)


def test_apply_patch_wrapper_returns_string_on_parse_error(workspace):
    """The public `apply_patch` tool must not raise — it wraps errors as strings."""
    result = apply_patch("not a patch")
    assert "Parse error" in result


def test_apply_patch_wrapper_returns_string_on_validation_error(workspace):
    (workspace / "x.py").write_text("not matching\n")
    text = """\
*** Begin Patch
*** Update File: x.py
@@ something
-different
+better
*** End Patch
"""
    result = apply_patch(text)
    assert "Validation error" in result


# ── Permission gate (security boundary) ──────────────────────────────
#
# apply_patch can delete and move files. It MUST NOT mutate the tree without
# an explicit allow — the bug that prompted this work was apply_patch applying
# silently with no prompt at all.


def test_apply_patch_denied_blocks_update(workspace, monkeypatch):
    import aru.tools.apply_patch as mod

    p = workspace / "greet.py"
    p.write_text("def greet():\n    return 1\n")
    monkeypatch.setattr(mod, "check_permission", lambda *a, **k: False)

    text = """\
*** Begin Patch
*** Update File: greet.py
@@ def greet():
-    return 1
+    return 2
*** End Patch
"""
    result = mod.apply_patch(text)
    assert "PERMISSION DENIED" in result
    # File must be completely untouched.
    assert p.read_text() == "def greet():\n    return 1\n"


def test_apply_patch_denied_blocks_delete(workspace, monkeypatch):
    """The 'could delete a database' case: a denied Delete must not remove the file."""
    import aru.tools.apply_patch as mod

    p = workspace / "victim.txt"
    p.write_text("important data\n")
    monkeypatch.setattr(mod, "check_permission", lambda *a, **k: False)

    text = """\
*** Begin Patch
*** Delete File: victim.txt
*** End Patch
"""
    result = mod.apply_patch(text)
    assert "PERMISSION DENIED" in result
    assert p.exists()
    assert p.read_text() == "important data\n"


def test_apply_patch_allowed_applies_and_consults_edit_category(workspace, monkeypatch):
    import aru.tools.apply_patch as mod

    p = workspace / "greet.py"
    p.write_text("def greet():\n    return 1\n")
    seen: list = []

    def fake_check(category, subjects, details):
        seen.append((category, subjects))
        return True

    monkeypatch.setattr(mod, "check_permission", fake_check)

    text = """\
*** Begin Patch
*** Update File: greet.py
@@ def greet():
-    return 1
+    return 2
*** End Patch
"""
    result = mod.apply_patch(text)
    assert "1 updated" in result
    assert "return 2" in p.read_text()
    # Gated under the "edit" category, with the affected path as a subject.
    assert seen and seen[0][0] == "edit"
    assert any("greet.py" in s for s in seen[0][1])


def test_apply_patch_prompt_shows_real_diff(workspace, monkeypatch):
    """The user must see the actual +/- lines, not a blind 'update' summary."""
    import aru.tools.apply_patch as mod
    from rich.console import Console

    p = workspace / "greet.py"
    p.write_text("def greet():\n    return 1\n")
    captured = {}

    def fake_check(category, subjects, details):
        captured["details"] = details
        return False  # block — we only care about what was shown

    monkeypatch.setattr(mod, "check_permission", fake_check)
    text = """\
*** Begin Patch
*** Update File: greet.py
@@ def greet():
-    return 1
+    return 2
*** End Patch
"""
    mod.apply_patch(text)
    console = Console(width=120)
    with console.capture() as cap:
        console.print(captured["details"])
    out = cap.get()
    assert "-    return 1" in out
    assert "+    return 2" in out


def test_apply_patch_does_not_prompt_for_unapplicable_patch(workspace, monkeypatch):
    """Validation runs BEFORE the prompt — a stale patch never reaches the user."""
    import aru.tools.apply_patch as mod

    (workspace / "x.py").write_text("actual content\n")
    prompts = {"n": 0}

    def counting_check(*a, **k):
        prompts["n"] += 1
        return True

    monkeypatch.setattr(mod, "check_permission", counting_check)

    text = """\
*** Begin Patch
*** Update File: x.py
@@ nonexistent anchor
-does not match
+replacement
*** End Patch
"""
    result = mod.apply_patch(text)
    assert "Validation error" in result
    assert prompts["n"] == 0
