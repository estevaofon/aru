"""Tests for the checkpoint/undo system."""

import os
import tempfile

import pytest

from aru.checkpoints import CheckpointManager


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary workspace with some files."""
    (tmp_path / "hello.py").write_text("print('hello')\n")
    (tmp_path / "config.json").write_text('{"key": "value"}\n')
    return tmp_path


@pytest.fixture
def manager(tmp_path):
    """Create a CheckpointManager with temp backup dir."""
    backup_dir = str(tmp_path / "backups")
    return CheckpointManager("test-session", base_dir=backup_dir)


class TestCheckpointManager:
    def test_begin_turn_creates_snapshot(self, manager):
        manager.begin_turn(1)
        assert manager.get_snapshot_count() == 1

    def test_track_edit_captures_file_state(self, manager, tmp_workspace):
        manager.begin_turn(1)
        file_path = str(tmp_workspace / "hello.py")
        manager.track_edit(file_path)

        affected = manager.get_last_snapshot_files()
        assert os.path.abspath(file_path) in affected

    def test_track_edit_idempotent_within_turn(self, manager, tmp_workspace):
        manager.begin_turn(1)
        file_path = str(tmp_workspace / "hello.py")
        manager.track_edit(file_path)
        manager.track_edit(file_path)  # should be no-op

        affected = manager.get_last_snapshot_files()
        assert len(affected) == 1

    def test_undo_restores_edited_file(self, manager, tmp_workspace):
        file_path = str(tmp_workspace / "hello.py")
        original_content = "print('hello')\n"

        manager.begin_turn(1)
        manager.track_edit(file_path)

        # Simulate edit
        with open(file_path, "w") as f:
            f.write("print('CHANGED')\n")
        assert open(file_path).read() == "print('CHANGED')\n"

        # Undo
        restored, turn = manager.undo_last_turn()
        assert turn == 1
        assert os.path.abspath(file_path) in restored
        assert open(file_path).read() == original_content

    def test_undo_deletes_newly_created_file(self, manager, tmp_workspace):
        new_file = str(tmp_workspace / "new_file.py")

        manager.begin_turn(1)
        manager.track_edit(new_file)  # file doesn't exist yet

        # Simulate creation
        with open(new_file, "w") as f:
            f.write("new content\n")
        assert os.path.isfile(new_file)

        # Undo should delete the file
        restored, turn = manager.undo_last_turn()
        assert os.path.abspath(new_file) in restored
        assert not os.path.isfile(new_file)

    def test_undo_multiple_files(self, manager, tmp_workspace):
        file1 = str(tmp_workspace / "hello.py")
        file2 = str(tmp_workspace / "config.json")

        manager.begin_turn(1)
        manager.track_edit(file1)
        manager.track_edit(file2)

        # Edit both
        with open(file1, "w") as f:
            f.write("changed1\n")
        with open(file2, "w") as f:
            f.write("changed2\n")

        # Undo
        restored, _ = manager.undo_last_turn()
        assert len(restored) == 2
        assert open(file1).read() == "print('hello')\n"
        assert open(file2).read() == '{"key": "value"}\n'

    def test_undo_only_affects_last_turn(self, manager, tmp_workspace):
        file_path = str(tmp_workspace / "hello.py")

        # Turn 1: edit file
        manager.begin_turn(1)
        manager.track_edit(file_path)
        with open(file_path, "w") as f:
            f.write("turn1\n")

        # Turn 2: edit file again
        manager.begin_turn(2)
        manager.track_edit(file_path)
        with open(file_path, "w") as f:
            f.write("turn2\n")

        # Undo turn 2 → should restore to turn1 state
        restored, turn = manager.undo_last_turn()
        assert turn == 2
        assert open(file_path).read() == "turn1\n"

        # Undo turn 1 → should restore to original
        restored, turn = manager.undo_last_turn()
        assert turn == 1
        assert open(file_path).read() == "print('hello')\n"

    def test_undo_empty_returns_empty(self, manager):
        restored, turn = manager.undo_last_turn()
        assert restored == []
        assert turn == 0

    def test_get_last_snapshot_files_empty(self, manager):
        assert manager.get_last_snapshot_files() == []

    def test_max_snapshots_enforced(self, manager, tmp_workspace):
        file_path = str(tmp_workspace / "hello.py")
        for i in range(105):
            manager.begin_turn(i)
            manager.track_edit(file_path)
            with open(file_path, "w") as f:
                f.write(f"v{i}\n")

        assert manager.get_snapshot_count() == 100

    def test_cleanup_removes_backup_dir(self, manager, tmp_workspace):
        file_path = str(tmp_workspace / "hello.py")
        manager.begin_turn(1)
        manager.track_edit(file_path)

        assert os.path.isdir(manager._base_dir)
        manager.cleanup()
        assert not os.path.isdir(manager._base_dir)


class TestSessionUndoLastTurn:
    """Tests for Session.undo_last_turn (conversation history only)."""

    def test_undo_removes_last_turn(self):
        from aru.session import Session
        session = Session()
        session.add_message("user", "hello")
        session.add_message("assistant", "hi there")
        session.add_message("user", "how are you")
        session.add_message("assistant", "good")

        removed = session.undo_last_turn()
        assert removed == 2  # user + assistant
        assert len(session.history) == 2
        assert session.history[-1]["role"] == "assistant"

    def test_undo_removes_tool_messages(self):
        from aru.session import Session
        session = Session()
        session.add_message("user", "fix the bug")
        session.add_message("assistant", "reading file")
        session.add_message("tool", "file contents here")
        session.add_message("assistant", "done")

        removed = session.undo_last_turn()
        # Should remove: user + assistant + tool + assistant = 4 if they go back to last user
        # Actually: pops from end until user is found
        # done (assistant) → tool → reading file (assistant) → fix the bug (user) = 4
        assert removed == 4
        assert len(session.history) == 0

    def test_undo_empty_history(self):
        from aru.session import Session
        session = Session()
        removed = session.undo_last_turn()
        assert removed == 0
