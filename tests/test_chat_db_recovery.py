"""Durability tests for the chat database: WAL mode and its verified backups.

Mirrors tests/test_sqlite_settings_recovery.py's coverage of the same
validated-backup, corrupt-primary recovery pattern, now shared by the chat
store.
"""

from pathlib import Path
import shutil
import sqlite3

import pytest

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


def test_recovery_discards_the_crashed_databases_write_ahead_log(tmp_path: Path) -> None:
    """A restored primary must not inherit the dead database's write-ahead log.

    The event that corrupts the primary -- a crash, a power loss, a forced
    reboot -- is the same event that leaves an uncheckpointed -wal behind.
    Once recovery replaces the primary with a verified backup, that log
    describes a database that no longer exists. SQLite has no way to know
    that, so the next connection replays it straight onto the replacement and
    silently overwrites recovered rows with content from the file that was
    just declared corrupt.

    sqlite_settings.py discards the sidecars on this exact path and explains
    why; this store copied the backup rotation without that step.
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

    assert wal.exists()
