"""Durability tests for the chat database: WAL mode and its verified backups.

Mirrors tests/test_sqlite_settings_recovery.py's coverage of the same
validated-backup, corrupt-primary recovery pattern, now shared by the chat
store.
"""

from datetime import datetime
import errno
import os
from pathlib import Path
import shutil
import sqlite3
import sys

import pytest

from cortex_backend.repositories import storage
from cortex_backend.repositories.storage import DatabaseManager, PersistenceError


def _manager_with_data(tmp_path: Path) -> tuple[DatabaseManager, dict]:
    db_path = str(tmp_path / "chat.sqlite")
    legacy_dir = str(tmp_path / "legacy")
    manager = DatabaseManager(db_path=db_path, legacy_history_dir=legacy_dir)
    manager.create_chat_from_messages(
        "thread-1",
        "Original title",
        [{"role": "user", "content": "hello"}],
    )
    # The backup only refreshes at startup (see _create_backup's docstring),
    # not on every write. Re-opening simulates a restart so the backup
    # actually captures this data before a test corrupts the primary.
    manager = DatabaseManager(db_path=db_path, legacy_history_dir=legacy_dir)
    return manager, manager.load_chat("thread-1")


def test_wal_mode_is_enabled(tmp_path: Path) -> None:
    manager = DatabaseManager(
        db_path=str(tmp_path / "chat.sqlite"),
        legacy_history_dir=str(tmp_path / "legacy"),
    )
    with manager.connect() as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_backup_is_created_at_startup(tmp_path: Path) -> None:
    manager, original = _manager_with_data(tmp_path)
    assert Path(manager.backup_path).exists()
    assert DatabaseManager._database_is_valid(manager.backup_path)

    # A second instance against the same file refreshes the backup and
    # rotates the prior verified copy into the older generation.
    DatabaseManager(db_path=manager.db_path, legacy_history_dir=manager.legacy_history_dir)
    assert Path(manager.previous_backup_path).exists()


def test_corrupt_primary_recovers_without_overwriting_valid_backup(tmp_path: Path) -> None:
    manager, original = _manager_with_data(tmp_path)
    backup_before = Path(manager.backup_path).read_bytes()
    Path(manager.db_path).write_bytes(b"corrupt-primary")

    recovered = DatabaseManager(db_path=manager.db_path, legacy_history_dir=manager.legacy_history_dir)

    assert recovered.load_chat("thread-1") == original
    assert Path(manager.backup_path).read_bytes() == backup_before
    assert recovered.last_corrupt_path is not None
    assert Path(recovered.last_corrupt_path).read_bytes() == b"corrupt-primary"


def test_corrupt_primary_without_valid_backup_fails_closed_and_preserves_files(tmp_path: Path) -> None:
    # Nothing has ever successfully initialized this path -- e.g. corruption
    # struck before the very first launch could complete -- so no backup of
    # any generation exists yet.
    db_path = tmp_path / "chat.sqlite"
    db_path.write_bytes(b"corrupt-primary")

    with pytest.raises(PersistenceError, match="corrupt"):
        DatabaseManager(db_path=str(db_path), legacy_history_dir=str(tmp_path / "legacy"))

    assert db_path.read_bytes() == b"corrupt-primary"
    assert not (tmp_path / "chat.sqlite.bak").exists()


def test_corrupt_primary_and_backup_fail_without_overwriting_either_file(tmp_path: Path) -> None:
    manager, _ = _manager_with_data(tmp_path)
    Path(manager.db_path).write_bytes(b"corrupt-primary")
    Path(manager.backup_path).write_bytes(b"corrupt-backup")
    Path(manager.previous_backup_path).write_bytes(b"corrupt-older-backup")

    with pytest.raises(PersistenceError, match="corrupt"):
        DatabaseManager(db_path=manager.db_path, legacy_history_dir=manager.legacy_history_dir)

    assert Path(manager.db_path).read_bytes() == b"corrupt-primary"
    assert Path(manager.backup_path).read_bytes() == b"corrupt-backup"
    assert Path(manager.previous_backup_path).read_bytes() == b"corrupt-older-backup"


def test_failed_recovery_restores_corrupt_primary_from_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _manager_with_data(tmp_path)
    Path(manager.db_path).write_bytes(b"corrupt-primary")

    def fail_recovery_copy(cls, source, destination):
        raise PersistenceError("injected recovery failure", operation="backup")

    monkeypatch.setattr(
        DatabaseManager,
        "_atomic_copy_database",
        classmethod(fail_recovery_copy),
    )

    with pytest.raises(PersistenceError, match="injected recovery failure"):
        DatabaseManager(db_path=manager.db_path, legacy_history_dir=manager.legacy_history_dir)

    assert Path(manager.db_path).read_bytes() == b"corrupt-primary"
    assert not list(tmp_path.glob("chat.sqlite.corrupt-*"))


def test_recovery_falls_back_to_older_verified_backup_generation(tmp_path: Path) -> None:
    manager, original = _manager_with_data(tmp_path)
    # Force a second generation to exist, then corrupt everything except it.
    manager.update_chat_title("thread-1", "Updated title")
    DatabaseManager(db_path=manager.db_path, legacy_history_dir=manager.legacy_history_dir)
    Path(manager.db_path).write_bytes(b"corrupt-primary")
    Path(manager.backup_path).write_bytes(b"corrupt-backup")

    recovered = DatabaseManager(db_path=manager.db_path, legacy_history_dir=manager.legacy_history_dir)

    assert recovered.load_chat("thread-1") == original
    assert recovered.load_chat("thread-1")["title"] == "Original title"
    assert recovered.recovery_report is not None
    assert recovered.recovery_report.recovered_from == manager.previous_backup_path


def _abandon_an_uncheckpointed_write(manager: DatabaseManager, tmp_path: Path) -> tuple[Path, Path]:
    """Capture a real -wal holding a committed frame, the way a crash leaves one.

    Writing through a raw connection and copying the sidecar before closing
    reproduces an unclean exit: the frame is committed to the log but not yet
    folded back into the primary.
    """
    raw = sqlite3.connect(manager.db_path)
    try:
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("UPDATE threads SET title = 'written just before the crash' WHERE id = 'thread-1'")
        raw.commit()
        saved_wal = tmp_path / "captured.wal"
        saved_shm = tmp_path / "captured.shm"
        shutil.copy2(f"{manager.db_path}-wal", saved_wal)
        shm = Path(f"{manager.db_path}-shm")
        if shm.exists():
            shutil.copy2(shm, saved_shm)
    finally:
        raw.close()
    return saved_wal, saved_shm


def test_recovery_quarantines_the_crashed_databases_write_ahead_log(tmp_path: Path) -> None:
    """A restored primary must not inherit the dead database's write-ahead log.

    The event that corrupts the primary -- a crash, a power loss, a forced
    reboot -- is the same event that leaves an uncheckpointed -wal behind.
    Once recovery replaces the primary with a verified backup, that log
    describes a database that no longer exists. SQLite has no way to know
    that, so the next connection replays it straight onto the replacement and
    silently overwrites recovered rows with content from the file that was
    just declared corrupt.

    The log is moved next to the quarantined primary rather than deleted: it
    may hold the newest committed messages, and deleting it would foreclose
    salvaging them with ``sqlite3 .recover``.
    """
    manager, original = _manager_with_data(tmp_path)
    saved_wal, saved_shm = _abandon_an_uncheckpointed_write(manager, tmp_path)

    # The crash itself: a torn primary, with the pre-crash log still on disk.
    Path(manager.db_path).write_bytes(b"corrupt-primary")
    shutil.copy2(saved_wal, f"{manager.db_path}-wal")
    if saved_shm.exists():
        shutil.copy2(saved_shm, f"{manager.db_path}-shm")

    recovered = DatabaseManager(
        db_path=manager.db_path, legacy_history_dir=manager.legacy_history_dir
    )

    assert recovered.load_chat("thread-1") == original
    assert not Path(f"{manager.db_path}-wal").exists()
    assert not Path(f"{manager.db_path}-shm").exists()
    # Moved, not vanished: the same bytes now sit beside the quarantined file.
    assert recovered.recovery_report is not None
    quarantined = recovered.recovery_report.quarantined_path
    assert Path(f"{quarantined}-wal").read_bytes() == saved_wal.read_bytes()
    if saved_shm.exists():
        # The shared-memory index is rebuilt state that SQLite rewrites when it
        # opens the file, so only its presence is preserved, not its bytes.
        assert Path(f"{quarantined}-shm").exists()


def test_a_failed_recovery_keeps_the_original_sidecars(tmp_path: Path) -> None:
    """Rollback must be a true rollback.

    When no backup is usable the corrupt primary is put back, so its sidecars
    still describe it. Discarding them on that path would throw away the only
    remaining copy of the most recent writes.
    """
    manager, _ = _manager_with_data(tmp_path)
    Path(manager.db_path).write_bytes(b"corrupt-primary")
    Path(manager.backup_path).write_bytes(b"corrupt-backup")
    Path(manager.previous_backup_path).write_bytes(b"corrupt-previous")
    wal = Path(f"{manager.db_path}-wal")
    wal.write_bytes(b"still describes the primary")

    with pytest.raises(PersistenceError):
        DatabaseManager(
            db_path=manager.db_path, legacy_history_dir=manager.legacy_history_dir
        )

    assert wal.read_bytes() == b"still describes the primary"
    assert not list(tmp_path.glob("chat.sqlite.corrupt-*"))


def _reopen(manager: DatabaseManager) -> DatabaseManager:
    return DatabaseManager(
        db_path=manager.db_path, legacy_history_dir=manager.legacy_history_dir
    )


def _leftover_temporaries(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir() if ".tmp" in entry.name)


# -- BE-46: a failed startup backup must not make Cortex unlaunchable -------


def test_a_failed_startup_backup_does_not_block_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, original = _manager_with_data(tmp_path)
    backup_before = Path(manager.backup_path).read_bytes()

    def fail_copy(cls, source, destination):
        raise PersistenceError("injected copy failure", operation="backup")

    monkeypatch.setattr(DatabaseManager, "_atomic_copy_database", classmethod(fail_copy))

    reopened = _reopen(manager)

    assert reopened.backup_status[0] == "failed"
    assert reopened.load_chat("thread-1") == original
    reopened.add_message("thread-1", "assistant", "still writable")
    assert len(reopened.load_chat("thread-1")["messages"]) == 2
    assert Path(manager.backup_path).read_bytes() == backup_before


def _full_disk_while_snapshotting(monkeypatch: pytest.MonkeyPatch, manager: DatabaseManager) -> None:
    def full(*_args, **_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(storage, "snapshot_database", full)


def _full_disk_while_rotating(monkeypatch: pytest.MonkeyPatch, manager: DatabaseManager) -> None:
    def full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(storage.shutil, "copy2", full)


def _backup_file_locked_by_another_program(
    monkeypatch: pytest.MonkeyPatch, manager: DatabaseManager
) -> None:
    real_replace = os.replace
    locked = os.path.normcase(os.path.abspath(manager.backup_path))

    def replace(source, destination, *args, **kwargs):
        if os.path.normcase(os.path.abspath(destination)) == locked:
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def _snapshot_that_fails_verification(
    monkeypatch: pytest.MonkeyPatch, manager: DatabaseManager
) -> None:
    def torn(_source, destination, **_kwargs):
        Path(destination).write_bytes(b"torn snapshot")

    monkeypatch.setattr(storage, "snapshot_database", torn)


@pytest.mark.parametrize(
    "inject",
    [
        _full_disk_while_snapshotting,
        _full_disk_while_rotating,
        _backup_file_locked_by_another_program,
        _snapshot_that_fails_verification,
    ],
    ids=["disk-full-snapshot", "disk-full-rotation", "backup-locked", "snapshot-corrupt"],
)
def test_startup_survives_every_way_the_backup_can_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inject
) -> None:
    """The primary is healthy, so chatting must keep working and the existing
    backups must come through untouched -- then the next launch recovers."""
    manager, _ = _manager_with_data(tmp_path)
    manager.update_chat_title("thread-1", "Changed after the last backup")
    backup_before = Path(manager.backup_path).read_bytes()
    inject(monkeypatch, manager)

    reopened = _reopen(manager)
    monkeypatch.undo()

    state, detail = reopened.backup_status
    assert state == "failed"
    assert detail and str(tmp_path) not in detail
    assert reopened.load_chat("thread-1")["title"] == "Changed after the last backup"
    assert Path(manager.backup_path).read_bytes() == backup_before
    assert DatabaseManager._database_is_valid(manager.backup_path)
    assert DatabaseManager._database_is_valid(manager.previous_backup_path)
    assert _leftover_temporaries(tmp_path) == []

    healthy = _reopen(manager)
    assert healthy.backup_status == ("ok", None)
    assert Path(manager.backup_path).read_bytes() != backup_before


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to replace an open file")
def test_a_backup_file_held_open_by_another_program_does_not_block_startup(tmp_path: Path) -> None:
    manager, _ = _manager_with_data(tmp_path)
    backup_before = Path(manager.backup_path).read_bytes()

    with open(manager.backup_path, "rb"):
        reopened = _reopen(manager)

    assert reopened.backup_status[0] == "failed"
    assert reopened.load_chat("thread-1") is not None
    assert Path(manager.backup_path).read_bytes() == backup_before
    assert _leftover_temporaries(tmp_path) == []


def test_a_corrupt_primary_still_refuses_to_start_without_a_usable_backup(tmp_path: Path) -> None:
    """Non-fatal backups must not loosen the recovery path: that stays closed."""
    manager, _ = _manager_with_data(tmp_path)
    Path(manager.db_path).write_bytes(b"corrupt-primary")
    Path(manager.backup_path).write_bytes(b"corrupt-backup")
    Path(manager.previous_backup_path).write_bytes(b"corrupt-older-backup")

    with pytest.raises(PersistenceError, match="no valid backup"):
        _reopen(manager)

    assert Path(manager.db_path).read_bytes() == b"corrupt-primary"


# -- BE-48: the backup must include commits still in the write-ahead log ----


def test_backup_includes_commits_a_concurrent_reader_keeps_in_the_wal(tmp_path: Path) -> None:
    """While a reader pins the log, wal_checkpoint(TRUNCATE) cannot finish and
    reports it only through its return value, so a file copy of the main
    database would be an older -- yet perfectly valid -- state."""
    manager, _ = _manager_with_data(tmp_path)
    reader = sqlite3.connect(manager.db_path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM messages").fetchall()

        manager.add_message("thread-1", "assistant", "committed while a reader held the log")
        assert Path(f"{manager.db_path}-wal").stat().st_size > 0

        manager._create_backup()
    finally:
        reader.close()

    backup = sqlite3.connect(Path(manager.backup_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        contents = [row[0] for row in backup.execute("SELECT content FROM messages")]
    finally:
        backup.close()
    assert "committed while a reader held the log" in contents


# -- BE-49: recovery is reported, and the crashed log is kept ---------------


def test_recovery_is_reported_with_the_backup_used_and_the_quarantined_file(tmp_path: Path) -> None:
    manager, _ = _manager_with_data(tmp_path)
    assert manager.recovery_report is None
    Path(manager.db_path).write_bytes(b"corrupt-primary")

    recovered = _reopen(manager)

    report = recovered.recovery_report
    assert report is not None
    assert report.recovered_from == recovered.backup_path
    assert report.quarantined_path == recovered.last_corrupt_path
    assert Path(report.quarantined_path).read_bytes() == b"corrupt-primary"
    assert datetime.fromisoformat(report.at).utcoffset() is not None
    assert recovered.backup_status[0] == "ok"


def test_a_failed_recovery_puts_the_write_ahead_log_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _manager_with_data(tmp_path)
    Path(manager.db_path).write_bytes(b"corrupt-primary")
    wal = Path(f"{manager.db_path}-wal")
    shm = Path(f"{manager.db_path}-shm")
    wal.write_bytes(b"newest committed frames")
    shm.write_bytes(b"shared memory index")

    def fail_recovery_copy(cls, source, destination):
        raise PersistenceError("injected recovery failure", operation="backup")

    monkeypatch.setattr(DatabaseManager, "_atomic_copy_database", classmethod(fail_recovery_copy))

    with pytest.raises(PersistenceError, match="injected recovery failure"):
        _reopen(manager)

    assert Path(manager.db_path).read_bytes() == b"corrupt-primary"
    assert wal.read_bytes() == b"newest committed frames"
    assert shm.exists()
    assert not list(tmp_path.glob("chat.sqlite.corrupt-*"))


def test_recovery_that_cannot_set_the_write_ahead_log_aside_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never delete the log to make room: refuse, and leave everything as found."""
    manager, _ = _manager_with_data(tmp_path)
    Path(manager.db_path).write_bytes(b"corrupt-primary")
    wal = Path(f"{manager.db_path}-wal")
    wal.write_bytes(b"newest committed frames")
    backup_before = Path(manager.backup_path).read_bytes()
    real_replace = os.replace

    def replace(source, destination, *args, **kwargs):
        if str(source).endswith("-wal"):
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(PersistenceError, match="write-ahead log"):
        _reopen(manager)
    monkeypatch.undo()

    assert Path(manager.db_path).read_bytes() == b"corrupt-primary"
    assert wal.read_bytes() == b"newest committed frames"
    assert Path(manager.backup_path).read_bytes() == backup_before
    assert not list(tmp_path.glob("chat.sqlite.corrupt-*"))


def test_recovery_that_cannot_move_the_primary_returns_the_write_ahead_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _manager_with_data(tmp_path)
    Path(manager.db_path).write_bytes(b"corrupt-primary")
    wal = Path(f"{manager.db_path}-wal")
    wal.write_bytes(b"newest committed frames")
    real_replace = os.replace

    def replace(source, destination, *args, **kwargs):
        if os.path.abspath(source) == os.path.abspath(manager.db_path):
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(PersistenceError, match="corrupt chat database before recovery"):
        _reopen(manager)
    monkeypatch.undo()

    assert Path(manager.db_path).read_bytes() == b"corrupt-primary"
    assert wal.read_bytes() == b"newest committed frames"
    assert not list(tmp_path.glob("chat.sqlite.corrupt-*"))


class _PowerLoss(BaseException):
    """Stands in for the process dying: nothing after it gets to run."""


def test_a_crash_between_setting_the_log_aside_and_moving_the_primary_recovers_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log moves before the primary, so dying in between leaves a primary
    that still looks corrupt -- and the next launch simply finishes the job,
    with the crashed log still preserved rather than replayed or lost."""
    manager, original = _manager_with_data(tmp_path)
    Path(manager.db_path).write_bytes(b"corrupt-primary")
    wal = Path(f"{manager.db_path}-wal")
    wal.write_bytes(b"newest committed frames")
    real_replace = os.replace

    def die_moving_the_primary(source, destination, *args, **kwargs):
        if os.path.abspath(source) == os.path.abspath(manager.db_path):
            raise _PowerLoss
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", die_moving_the_primary)
    with pytest.raises(_PowerLoss):
        _reopen(manager)
    monkeypatch.undo()

    assert Path(manager.db_path).read_bytes() == b"corrupt-primary"
    assert not wal.exists()
    preserved = list(tmp_path.glob("chat.sqlite.corrupt-*-wal"))
    assert [entry.read_bytes() for entry in preserved] == [b"newest committed frames"]

    recovered = _reopen(manager)

    assert recovered.load_chat("thread-1") == original
    assert [entry.read_bytes() for entry in preserved] == [b"newest committed frames"]
