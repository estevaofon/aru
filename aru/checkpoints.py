"""File checkpoint system for undo/rewind support.

Tracks file state before tool mutations so changes can be reverted.
Inspired by Claude Code's fileHistory system.

Architecture:
- Each user message creates a "snapshot" identified by a turn index.
- Before any file mutation (write_file, edit_file, bash), the pre-edit
  content is saved as a versioned backup in .aru/file-history/{session_id}/.
- On /undo, the most recent snapshot is applied: files are restored to
  their pre-turn state and the conversation is rewound.

Backup naming: {sha256(path)[:16]}@v{version}
Snapshot: {turn_index: {file_path: BackupEntry}}
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from dataclasses import dataclass, field


@dataclass
class BackupEntry:
    """A single file backup."""
    backup_path: str | None  # None = file didn't exist before this turn
    version: int
    original_path: str


@dataclass
class Snapshot:
    """Checkpoint at a specific conversation turn."""
    turn_index: int
    backups: dict[str, BackupEntry] = field(default_factory=dict)  # abs_path → BackupEntry


MAX_SNAPSHOTS = 100


class CheckpointManager:
    """Manages file checkpoints for a session.

    Thread-safe: multiple tools may run in parallel within a turn.
    """

    def __init__(self, session_id: str, base_dir: str | None = None):
        self._session_id = session_id
        self._base_dir = base_dir or os.path.join(os.getcwd(), ".aru", "file-history", session_id)
        self._lock = threading.Lock()
        self._snapshots: list[Snapshot] = []
        self._current_turn: int = 0
        self._tracked_files: set[str] = set()
        # Per-file version counter (monotonic)
        self._file_versions: dict[str, int] = {}
        self._dir_created = False

    def _ensure_dir(self):
        if not self._dir_created:
            os.makedirs(self._base_dir, exist_ok=True)
            self._dir_created = True

    def _backup_filename(self, file_path: str, version: int) -> str:
        path_hash = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:16]
        return f"{path_hash}@v{version}"

    def begin_turn(self, turn_index: int):
        """Start a new turn — creates a fresh snapshot for this turn."""
        with self._lock:
            self._current_turn = turn_index
            # Create snapshot for this turn (backups added lazily as files are edited)
            snapshot = Snapshot(turn_index=turn_index)
            self._snapshots.append(snapshot)
            # Enforce cap
            if len(self._snapshots) > MAX_SNAPSHOTS:
                evicted = self._snapshots.pop(0)
                self._cleanup_snapshot_backups(evicted)

    def track_edit(self, file_path: str):
        """Capture pre-edit state of a file before mutation.

        Call this BEFORE writing/editing a file. If the file was already
        captured in the current turn's snapshot, this is a no-op.
        """
        abs_path = os.path.abspath(file_path)

        with self._lock:
            if not self._snapshots:
                return

            current_snapshot = self._snapshots[-1]

            # Already tracked in this turn
            if abs_path in current_snapshot.backups:
                return

            # Increment version
            version = self._file_versions.get(abs_path, 0) + 1
            self._file_versions[abs_path] = version
            self._tracked_files.add(abs_path)

        # Read file outside lock (IO)
        backup_path = None
        if os.path.isfile(abs_path):
            self._ensure_dir()
            backup_name = self._backup_filename(abs_path, version)
            backup_path = os.path.join(self._base_dir, backup_name)
            try:
                shutil.copy2(abs_path, backup_path)
            except OSError:
                backup_path = None

        # Commit to snapshot
        with self._lock:
            if not self._snapshots:
                return
            entry = BackupEntry(
                backup_path=backup_path,
                version=version,
                original_path=abs_path,
            )
            self._snapshots[-1].backups[abs_path] = entry

    def undo_last_turn(self) -> tuple[list[str], int]:
        """Revert files changed in the most recent snapshot.

        Returns:
            (list of restored file paths, turn_index that was undone)
        """
        with self._lock:
            if not self._snapshots:
                return [], 0
            snapshot = self._snapshots.pop()

        restored = []
        for abs_path, entry in snapshot.backups.items():
            try:
                if entry.backup_path is None:
                    # File didn't exist before — delete it
                    if os.path.isfile(abs_path):
                        os.unlink(abs_path)
                        restored.append(abs_path)
                elif os.path.isfile(entry.backup_path):
                    # Restore from backup
                    shutil.copy2(entry.backup_path, abs_path)
                    restored.append(abs_path)
            except OSError:
                pass  # best effort

        return restored, snapshot.turn_index

    def get_snapshot_count(self) -> int:
        with self._lock:
            return len(self._snapshots)

    def get_last_snapshot_files(self) -> list[str]:
        """Return files that would be affected by undo."""
        with self._lock:
            if not self._snapshots:
                return []
            return list(self._snapshots[-1].backups.keys())

    def _cleanup_snapshot_backups(self, snapshot: Snapshot):
        """Remove backup files for an evicted snapshot (if not referenced by others)."""
        # Collect all backup paths still referenced
        referenced = set()
        for s in self._snapshots:
            for entry in s.backups.values():
                if entry.backup_path:
                    referenced.add(entry.backup_path)

        # Delete unreferenced backups
        for entry in snapshot.backups.values():
            if entry.backup_path and entry.backup_path not in referenced:
                try:
                    os.unlink(entry.backup_path)
                except OSError:
                    pass

    def cleanup(self):
        """Remove all backup files for this session."""
        try:
            if os.path.isdir(self._base_dir):
                shutil.rmtree(self._base_dir, ignore_errors=True)
        except OSError:
            pass
