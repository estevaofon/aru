"""Unit tests for arc.tools.indexer — chunking, file detection, and metadata."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from arc.tools.indexer import (
    _is_text_file,
    _chunk_file,
    _get_indexable_files,
    _load_meta,
    _save_meta,
    _get_arc_dir,
    _TEXT_EXTENSIONS,
    _FILENAMES_TO_INDEX,
    CHUNK_SIZE,
    CHUNK_OVERLAP,
    MAX_FILE_SIZE,
    ARC_DIR,
    META_FILE,
)


# ── _is_text_file ──────────────────────────────────────────────────

class TestIsTextFile:
    def test_python_file(self, tmp_path):
        f = tmp_path / "main.py"
        f.write_text("print('hello')")
        assert _is_text_file(str(f))

    def test_javascript_file(self, tmp_path):
        f = tmp_path / "app.js"
        f.write_text("console.log('hi')")
        assert _is_text_file(str(f))

    def test_known_filename(self, tmp_path):
        f = tmp_path / "Dockerfile"
        f.write_text("FROM python:3.12")
        assert _is_text_file(str(f))

    def test_makefile(self, tmp_path):
        f = tmp_path / "Makefile"
        f.write_text("all:\n\techo hello")
        assert _is_text_file(str(f))

    def test_binary_file(self, tmp_path):
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")
        assert not _is_text_file(str(f))

    def test_unknown_extension(self, tmp_path):
        f = tmp_path / "data.xyz"
        f.write_text("some text")
        assert not _is_text_file(str(f))

    def test_no_extension_text(self, tmp_path):
        f = tmp_path / "script"
        f.write_text("#!/bin/bash\necho hi")
        assert _is_text_file(str(f))

    def test_no_extension_binary(self, tmp_path):
        f = tmp_path / "blob"
        f.write_bytes(b"\x00\x01\x02\x03")
        assert not _is_text_file(str(f))

    def test_case_insensitive_extension(self, tmp_path):
        f = tmp_path / "README.MD"
        f.write_text("# Title")
        assert _is_text_file(str(f))

    def test_nonexistent_no_extension(self):
        assert not _is_text_file("/nonexistent/file_no_ext")


# ── _chunk_file ─────────────────────────────────────────────────────

class TestChunkFile:
    def test_small_file_single_chunk(self):
        content = "line1\nline2\nline3"
        chunks = _chunk_file(content, "small.py")
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["file_path"] == "small.py"
        assert chunks[0]["metadata"]["start_line"] == 1
        assert "line1" in chunks[0]["document"]

    def test_large_file_multiple_chunks(self):
        # Create content larger than CHUNK_SIZE
        lines = [f"line_{i} = 'x' * 50  # padding content here" for i in range(200)]
        content = "\n".join(lines)
        chunks = _chunk_file(content, "big.py")
        assert len(chunks) > 1

    def test_chunk_metadata_has_language(self):
        chunks = _chunk_file("code", "test.py")
        assert chunks[0]["metadata"]["language"] == "py"

    def test_chunk_metadata_unknown_language(self):
        chunks = _chunk_file("data", "Makefile")
        assert chunks[0]["metadata"]["language"] == "unknown"

    def test_chunk_ids_are_unique(self):
        lines = [f"line {i}" for i in range(300)]
        content = "\n".join(lines)
        chunks = _chunk_file(content, "file.py")
        ids = [c["id"] for c in chunks]
        assert len(ids) == len(set(ids))

    def test_backslash_normalized_in_path(self):
        chunks = _chunk_file("code", "src\\module\\file.py")
        assert chunks[0]["metadata"]["file_path"] == "src/module/file.py"

    def test_empty_content(self):
        chunks = _chunk_file("", "empty.py")
        assert len(chunks) == 1
        assert chunks[0]["document"] == ""


# ── _load_meta / _save_meta ─────────────────────────────────────────

class TestMeta:
    def test_load_meta_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        meta = _load_meta()
        assert meta == {}

    def test_save_and_load_meta(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import arc.tools.indexer as idx
        idx._index_meta = {"file.py": 12345.0}
        _save_meta()

        meta_path = tmp_path / META_FILE
        assert meta_path.exists()

        idx._index_meta = {}
        loaded = _load_meta()
        assert loaded == {"file.py": 12345.0}

    def test_load_meta_corrupt_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        meta_path = tmp_path / META_FILE
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text("{invalid json")
        meta = _load_meta()
        assert meta == {}


# ── _get_arc_dir ─────────────────────────────────────────────────────

class TestGetArcDir:
    def test_creates_arc_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        arc_dir = _get_arc_dir()
        assert os.path.isdir(arc_dir)
        assert arc_dir.endswith(ARC_DIR)


# ── _get_indexable_files ─────────────────────────────────────────────

class TestGetIndexableFiles:
    def test_finds_text_files(self, tmp_path):
        (tmp_path / "main.py").write_text("code")
        (tmp_path / "README.md").write_text("# docs")
        (tmp_path / "image.png").write_bytes(b"\x89PNG\x00\x00")

        files = _get_indexable_files(str(tmp_path))
        names = [os.path.basename(f) for f in files]
        assert "main.py" in names
        assert "README.md" in names
        assert "image.png" not in names

    def test_skips_large_files(self, tmp_path):
        big = tmp_path / "huge.py"
        big.write_text("x" * (MAX_FILE_SIZE + 1))

        files = _get_indexable_files(str(tmp_path))
        assert not any("huge.py" in f for f in files)

    def test_skips_empty_files(self, tmp_path):
        (tmp_path / "empty.py").write_text("")
        files = _get_indexable_files(str(tmp_path))
        assert not any("empty.py" in f for f in files)

    def test_includes_nested_files(self, tmp_path):
        sub = tmp_path / "src" / "lib"
        sub.mkdir(parents=True)
        (sub / "utils.py").write_text("code")

        files = _get_indexable_files(str(tmp_path))
        assert any("utils.py" in f for f in files)


# ── semantic_search ──────────────────────────────────────────────────

class TestSemanticSearch:
    def test_missing_chromadb(self):
        from arc.tools.indexer import semantic_search
        with patch.dict("sys.modules", {"chromadb": None}):
            # Force reimport check
            with patch("builtins.__import__", side_effect=ImportError("no chromadb")):
                result = semantic_search("test query")
                assert "unavailable" in result.lower() or "error" in result.lower()


# ── Constants ────────────────────────────────────────────────────────

class TestConstants:
    def test_text_extensions_include_common(self):
        assert ".py" in _TEXT_EXTENSIONS
        assert ".js" in _TEXT_EXTENSIONS
        assert ".ts" in _TEXT_EXTENSIONS
        assert ".md" in _TEXT_EXTENSIONS

    def test_filenames_include_dockerfile(self):
        assert "Dockerfile" in _FILENAMES_TO_INDEX
        assert "Makefile" in _FILENAMES_TO_INDEX

    def test_chunk_size_reasonable(self):
        assert CHUNK_SIZE > 0
        assert CHUNK_OVERLAP < CHUNK_SIZE
