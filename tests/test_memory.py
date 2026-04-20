"""Stage 4 Tier 2: auto-memory storage + extractor tests.

Covers:
- store: write/read roundtrip with frontmatter
- store: MEMORY.md index has one line per memory
- store: duplicate slug generation bumps suffix
- store: delete removes file AND index line
- store: clear wipes everything
- store: invalid type raises
- loader: memory_section_for_prompt returns '' when no index exists
- loader: index truncation respects MAX_INDEX_LINES
- extractor.should_trigger: respects auto_extract flag, min_tokens, remember hints
- extractor._parse_json_array: extracts JSON from mixed prose
- extractor._candidate_to_entry: drops malformed candidates
"""

from pathlib import Path

import pytest

from aru.memory.loader import (
    MAX_INDEX_LINES,
    load_memory_index,
    memory_section_for_prompt,
)
from aru.memory.store import (
    MAX_MEMORIES_PER_PROJECT,
    MemoryEntry,
    clear_memory,
    delete_memory,
    list_memories,
    memory_dir_for_project,
    read_memory,
    write_memory,
)
from aru.memory.extractor import (
    _candidate_to_entry,
    _parse_json_array,
    should_trigger,
)


@pytest.fixture
def memory_base(tmp_path):
    """Isolated memory root so tests don't touch ~/.aru."""
    return str(tmp_path)


@pytest.fixture
def project_root(tmp_path):
    """A distinct fake project path — its hash determines the subdir."""
    p = tmp_path / "proj_under_test"
    p.mkdir()
    return str(p)


def test_write_and_read_roundtrip(project_root, memory_base):
    entry = MemoryEntry(
        name="Use pytest",
        description="Default to pytest for tests",
        type="user",
        body="The project uses pytest exclusively; pick pytest idioms by default.",
    )
    persisted = write_memory(project_root, entry, base=memory_base)
    assert persisted.slug.startswith("user_")

    loaded = read_memory(project_root, persisted.slug, base=memory_base)
    assert loaded is not None
    assert loaded.name == entry.name
    assert loaded.type == "user"
    assert "pytest exclusively" in loaded.body


def test_index_has_one_line_per_memory(project_root, memory_base):
    a = MemoryEntry(name="A", description="desc-A", type="user", body="body a")
    b = MemoryEntry(name="B", description="desc-B", type="feedback", body="body b")
    write_memory(project_root, a, base=memory_base)
    write_memory(project_root, b, base=memory_base)

    mem_dir = memory_dir_for_project(project_root, base=memory_base)
    index = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "- [A](" in index
    assert "- [B](" in index
    assert "Memory Index" in index


def test_duplicate_names_generate_unique_slugs(project_root, memory_base):
    e1 = write_memory(
        project_root,
        MemoryEntry(name="Prefer X", description="d", type="user", body="body"),
        base=memory_base,
    )
    e2 = write_memory(
        project_root,
        MemoryEntry(name="Prefer X", description="d", type="user", body="body 2"),
        base=memory_base,
    )
    assert e1.slug != e2.slug
    assert e2.slug.endswith("_2")


def test_delete_removes_file_and_index_line(project_root, memory_base):
    entry = write_memory(
        project_root,
        MemoryEntry(name="N", description="d", type="project", body="body"),
        base=memory_base,
    )
    assert delete_memory(project_root, entry.slug, base=memory_base) is True
    assert read_memory(project_root, entry.slug, base=memory_base) is None
    mem_dir = memory_dir_for_project(project_root, base=memory_base)
    index = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert entry.slug not in index


def test_clear_removes_everything(project_root, memory_base):
    for i in range(3):
        write_memory(
            project_root,
            MemoryEntry(name=f"M{i}", description="d", type="user", body="b"),
            base=memory_base,
        )
    assert len(list_memories(project_root, base=memory_base)) == 3
    removed = clear_memory(project_root, base=memory_base)
    assert removed >= 3
    assert list_memories(project_root, base=memory_base) == []


def test_write_rejects_invalid_type(project_root, memory_base):
    with pytest.raises(ValueError, match="Invalid memory type"):
        write_memory(
            project_root,
            MemoryEntry(name="X", description="d", type="bogus", body="b"),
            base=memory_base,
        )


def test_memory_section_empty_when_no_index(project_root, memory_base):
    assert memory_section_for_prompt(project_root, base=memory_base) == ""


def test_memory_section_includes_body_when_index_exists(project_root, memory_base):
    write_memory(
        project_root,
        MemoryEntry(name="Testing rule", description="pytest only", type="user", body="b"),
        base=memory_base,
    )
    section = memory_section_for_prompt(project_root, base=memory_base)
    assert "Project memory" in section
    assert "Testing rule" in section


def test_load_memory_index_truncates_above_cap(project_root, memory_base):
    mem_dir = memory_dir_for_project(project_root, base=memory_base)
    # Seed a gigantic index file manually
    (mem_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n" + "\n".join(f"- line {i}" for i in range(MAX_INDEX_LINES + 30)),
        encoding="utf-8",
    )
    text = load_memory_index(project_root, base=memory_base)
    assert text.count("\n") <= MAX_INDEX_LINES
    assert "truncated" in text


# ── Extractor ────────────────────────────────────────────────────────

def test_should_trigger_respects_auto_extract_flag():
    ok, _ = should_trigger({"auto_extract": False}, "remember this", 1000)
    assert ok is False


def test_should_trigger_allows_explicit_remember_keyword():
    ok, reason = should_trigger({"auto_extract": True, "min_turn_tokens": 500}, "please remember", 100)
    assert ok is True
    assert "remember" in reason.lower()


def test_should_trigger_respects_min_tokens_threshold():
    ok, _ = should_trigger({"auto_extract": True, "min_turn_tokens": 500}, "ok", 100)
    assert ok is False


def test_should_trigger_fires_above_threshold():
    ok, _ = should_trigger({"auto_extract": True, "min_turn_tokens": 500}, "", 1200)
    assert ok is True


def test_parse_json_array_from_plain_list():
    assert _parse_json_array("[{\"x\":1},{\"x\":2}]") == [{"x": 1}, {"x": 2}]


def test_parse_json_array_extracts_from_prose_wrapper():
    text = "Here's the memories: [{\"x\":1}] and that's it."
    assert _parse_json_array(text) == [{"x": 1}]


def test_parse_json_array_returns_empty_on_garbage():
    assert _parse_json_array("definitely not json") == []


def test_candidate_to_entry_drops_missing_fields():
    assert _candidate_to_entry({"name": "", "type": "user", "body": "x"}) is None
    assert _candidate_to_entry({"name": "X", "type": "user"}) is None
    assert _candidate_to_entry({"name": "X", "type": "bogus", "body": "b"}) is None


def test_candidate_to_entry_builds_valid_entry():
    entry = _candidate_to_entry({
        "name": "Prefer typing",
        "description": "Use type hints",
        "type": "user",
        "body": "All new code should be annotated.",
    })
    assert entry is not None
    assert entry.type == "user"
    assert entry.name == "Prefer typing"
