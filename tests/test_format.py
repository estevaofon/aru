"""Stage 1 Tier 3: auto-format via file.changed hook.

We don't shell out to real formatters — the runner is monkeypatched to
return scripted output so tests are hermetic and fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aru.format.manager import (
    FormatManager,
    install_format_from_config,
)


@pytest.fixture
def scripted_runner(monkeypatch):
    """Replace ``run_formatter`` with a scripted double.

    Yields a callable ``set_response(output | None)`` and a ``record`` dict
    that captures each invocation.
    """
    record = {"calls": [], "response": None}

    async def fake_run_formatter(command, content, *, timeout=30.0):
        record["calls"].append({
            "command": list(command),
            "content": content,
            "timeout": timeout,
        })
        return record["response"]

    monkeypatch.setattr("aru.format.manager.run_formatter", fake_run_formatter)

    def set_response(value):
        record["response"] = value

    return record, set_response


@pytest.mark.asyncio
async def test_formats_python_write(tmp_path, scripted_runner):
    record, set_response = scripted_runner
    f = tmp_path / "x.py"
    f.write_text("def foo( ):\n    pass\n")  # pre-format
    set_response("def foo():\n    pass\n")   # post-format

    mgr = FormatManager(config={
        "enabled": True,
        "python": {"command": "black", "args": ["-q", "-"]},
    })
    await mgr.handle_file_changed({"path": str(f), "mutation_type": "write"})

    assert f.read_text() == "def foo():\n    pass\n"
    assert record["calls"][0]["command"] == ["black", "-q", "-"]


@pytest.mark.asyncio
async def test_disabled_is_noop(tmp_path, scripted_runner):
    record, _ = scripted_runner
    f = tmp_path / "x.py"
    f.write_text("def foo( ):\n    pass\n")
    mgr = FormatManager(config={"enabled": False, "python": {"command": "black"}})
    await mgr.handle_file_changed({"path": str(f), "mutation_type": "write"})
    assert record["calls"] == []
    assert f.read_text() == "def foo( ):\n    pass\n"


@pytest.mark.asyncio
async def test_delete_mutation_skips(tmp_path, scripted_runner):
    record, set_response = scripted_runner
    set_response("whatever\n")
    mgr = FormatManager(config={"enabled": True, "python": {"command": "black"}})
    await mgr.handle_file_changed({"path": str(tmp_path / "x.py"), "mutation_type": "delete"})
    assert record["calls"] == []


@pytest.mark.asyncio
async def test_unknown_extension_skipped(tmp_path, scripted_runner):
    record, set_response = scripted_runner
    set_response("formatted")
    f = tmp_path / "README.md"
    f.write_text("# hi\n")
    mgr = FormatManager(config={"enabled": True, "python": {"command": "black"}})
    await mgr.handle_file_changed({"path": str(f), "mutation_type": "write"})
    assert record["calls"] == []


@pytest.mark.asyncio
async def test_formatter_failure_preserves_original(tmp_path, scripted_runner):
    """run_formatter returning None must NOT overwrite the file."""
    _, set_response = scripted_runner
    set_response(None)
    f = tmp_path / "x.py"
    f.write_text("def foo( ):\n    pass\n")
    mgr = FormatManager(config={"enabled": True, "python": {"command": "black"}})
    await mgr.handle_file_changed({"path": str(f), "mutation_type": "write"})
    assert f.read_text() == "def foo( ):\n    pass\n"
    # Language is now marked failed — next call short-circuits before runner.
    assert "python" in mgr._failed_langs


@pytest.mark.asyncio
async def test_byte_match_skips_write(tmp_path, scripted_runner):
    """Formatter returning identical content must not trigger a write."""
    record, set_response = scripted_runner
    f = tmp_path / "x.py"
    pre = "def foo():\n    pass\n"
    f.write_text(pre)
    set_response(pre)  # same bytes

    # Stat the file before; check mtime unchanged
    mtime_before = f.stat().st_mtime_ns

    mgr = FormatManager(config={"enabled": True, "python": {"command": "black"}})
    await mgr.handle_file_changed({"path": str(f), "mutation_type": "write"})

    assert f.read_text() == pre
    # Runner was called (there's no way to know in advance if it's a no-op)
    assert len(record["calls"]) == 1
    # Guaranteed: no write happened (mtime preserved bit-for-bit)
    assert f.stat().st_mtime_ns == mtime_before


@pytest.mark.asyncio
async def test_reentrancy_blocked_by_in_progress(tmp_path, monkeypatch):
    """Recursive file.changed during format must not trigger double format."""
    f = tmp_path / "x.py"
    f.write_text("x\n")

    mgr = FormatManager(config={"enabled": True, "python": {"command": "black"}})
    call_count = {"n": 0}

    async def recursive_runner(command, content, *, timeout=30.0):
        call_count["n"] += 1
        # Simulate the reentrancy that would happen if the write after
        # formatting fired file.changed — handler_file_changed is invoked
        # again for the same path. The _in_progress guard must short-circuit.
        await mgr.handle_file_changed({"path": str(f), "mutation_type": "write"})
        return "y\n"

    monkeypatch.setattr("aru.format.manager.run_formatter", recursive_runner)
    await mgr.handle_file_changed({"path": str(f), "mutation_type": "write"})

    # Recursive call saw _in_progress and returned without re-entering runner.
    assert call_count["n"] == 1


def test_install_format_from_config_empty_returns_none():
    assert install_format_from_config(None) is None
    assert install_format_from_config({}) is None


def test_install_format_from_config_returns_manager():
    mgr = install_format_from_config({"enabled": True, "python": {"command": "black"}})
    assert isinstance(mgr, FormatManager)
    assert mgr.enabled()


@pytest.mark.asyncio
async def test_path_substitution_in_args(tmp_path, scripted_runner):
    """{path} placeholder in args should resolve to the absolute file path."""
    record, set_response = scripted_runner
    set_response("formatted\n")
    f = tmp_path / "sub" / "x.ts"
    f.parent.mkdir()
    f.write_text("const x = 1\n")

    mgr = FormatManager(config={
        "enabled": True,
        "typescript": {
            "command": "prettier",
            "args": ["--stdin-filepath", "{path}"],
        },
    })
    await mgr.handle_file_changed({"path": str(f), "mutation_type": "write"})

    assert record["calls"][0]["command"] == [
        "prettier", "--stdin-filepath", str(f.resolve()),
    ] or record["calls"][0]["command"] == [
        "prettier", "--stdin-filepath", str(f),
    ]
