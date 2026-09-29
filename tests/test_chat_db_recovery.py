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

from sqlite_faults import volume_without_write_ahead_logging
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

    def fail_snapshot(cls, source, destination, **_kwargs):
        raise PersistenceError("injected snapshot failure", operation="backup")

    monkeypatch.setattr(DatabaseManager, "_atomic_snapshot_database", classmethod(fail_snapshot))

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


def _refuse_renames(monkeypatch: pytest.MonkeyPatch, *, source: str | None = None, destination: str | None = None):
    """Make os.replace fail the way Windows does for a file another program holds open."""
    real_replace = os.replace

    def normal(path) -> str:
        return os.path.normcase(os.path.abspath(path))

    def replace(src, dst, *args, **kwargs):
        if (source is not None and normal(src) == normal(source)) or (
            destination is not None and normal(dst) == normal(destination)
        ):
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def _backup_file_locked_by_another_program(
    monkeypatch: pytest.MonkeyPatch, manager: DatabaseManager
) -> None:
    # An open file cannot be renamed, so the current backup cannot be set aside.
    _refuse_renames(monkeypatch, source=manager.backup_path)


def _previous_generation_locked_by_another_program(
    monkeypatch: pytest.MonkeyPatch, manager: DatabaseManager
) -> None:
    _refuse_renames(monkeypatch, destination=manager.previous_backup_path)


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
        _backup_file_locked_by_another_program,
        _previous_generation_locked_by_another_program,
        _snapshot_that_fails_verification,
    ],
    ids=["disk-full-snapshot", "backup-locked", "previous-generation-locked", "snapshot-corrupt"],
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
    # The log did not stay stranded under the first attempt's name: the second
    # recovery moved it beside the primary it quarantined (see the adoption
    # tests below).
    assert recovered.recovery_report is not None
    beside_the_quarantined_file = Path(f"{recovered.recovery_report.quarantined_path}-wal")
    assert beside_the_quarantined_file.read_bytes() == b"newest committed frames"


def _chat_ids(path: str) -> set[str]:
    probe = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {row[0] for row in probe.execute("SELECT id FROM threads")}
    finally:
        probe.close()


# -- BE-50: WAL is read back, and NORMAL is only applied on top of it -------


def test_a_volume_that_cannot_do_write_ahead_logging_is_refused_before_anything_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "chat.sqlite"
    volume_without_write_ahead_logging(monkeypatch)

    with pytest.raises(PersistenceError, match="write-ahead logging") as refused:
        DatabaseManager(db_path=str(db_path), legacy_history_dir=str(tmp_path / "legacy"))
    monkeypatch.undo()

    assert "--data-dir" in str(refused.value)
    assert str(tmp_path) not in str(refused.value)
    probe = sqlite3.connect(db_path)
    try:
        assert probe.execute("PRAGMA user_version").fetchone()[0] == 0
        assert probe.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        probe.close()


def test_an_existing_database_on_a_volume_without_wal_is_left_at_its_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _manager_with_data(tmp_path)
    probe = sqlite3.connect(manager.db_path)
    try:
        probe.execute("PRAGMA user_version = 3")
        probe.commit()
    finally:
        probe.close()
    volume_without_write_ahead_logging(monkeypatch)

    with pytest.raises(PersistenceError, match="write-ahead logging"):
        _reopen(manager)
    monkeypatch.undo()

    probe = sqlite3.connect(manager.db_path)
    try:
        assert probe.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        probe.close()
    assert _chat_ids(manager.db_path) == {"thread-1"}


def test_an_existing_install_on_a_volume_without_wal_is_told_to_copy_its_data_before_moving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing --data-dir at an empty folder starts with no chats, which reads as
    data loss. The refusal has to say the chats are still where they were and how
    to bring them along."""
    manager, _ = _manager_with_data(tmp_path)
    database_before = Path(manager.db_path).read_bytes()
    files_before = sorted(entry.name for entry in tmp_path.iterdir())
    volume_without_write_ahead_logging(monkeypatch)

    with pytest.raises(PersistenceError, match="write-ahead logging") as refused:
        _reopen(manager)
    monkeypatch.undo()

    message = " ".join(str(refused.value).split())
    assert "nothing was deleted or modified" in message.lower()
    assert "copy the existing data files" in message
    assert "-wal" in message and "-shm" in message
    assert message.index("copy the existing data files") < message.index("--data-dir")
    assert "empty" in message
    assert str(tmp_path) not in message
    # ... and that is true: the refusal changed nothing.
    assert Path(manager.db_path).read_bytes() == database_before
    assert sorted(entry.name for entry in tmp_path.iterdir()) == files_before
    assert _chat_ids(manager.db_path) == {"thread-1"}


def test_synchronous_normal_is_only_applied_once_write_ahead_logging_is_confirmed(
    tmp_path: Path,
) -> None:
    """NORMAL loses durability guarantees in the rollback-journal modes, so
    until WAL has been read back the connection keeps SQLite's default (FULL)."""
    manager = DatabaseManager(
        db_path=str(tmp_path / "chat.sqlite"), legacy_history_dir=str(tmp_path / "legacy")
    )
    NORMAL, FULL = 1, 2

    with manager.connect() as connection:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == NORMAL
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"

    manager._wal_confirmed = False
    with manager.connect() as connection:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == FULL


# -- BE-51: one full read of the primary, one snapshot, no second copy ------


def test_startup_backup_rotation_reads_the_primary_once_and_renames_the_old_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _manager_with_data(tmp_path)
    old_backup = Path(manager.backup_path).read_bytes()
    checks: list[tuple[str, bool]] = []
    snapshots: list[str] = []
    outgoing_checks: list[str] = []
    real_is_valid = DatabaseManager._database_is_valid
    real_snapshot = storage.snapshot_database
    real_outgoing_check = storage.quick_check_at_rest

    def spy_is_valid(path: str, *, quick: bool = False) -> bool:
        checks.append((os.path.basename(path), quick))
        return real_is_valid(path, quick=quick)

    def spy_snapshot(source, destination, **kwargs):
        snapshots.append(os.path.basename(str(source)))
        return real_snapshot(source, destination, **kwargs)

    def spy_outgoing_check(path, **kwargs):
        outgoing_checks.append(os.path.basename(str(path)))
        return real_outgoing_check(path, **kwargs)

    def no_byte_copies(*_args, **_kwargs):
        raise AssertionError("rotation must rename the old generation, not copy it")

    monkeypatch.setattr(DatabaseManager, "_database_is_valid", staticmethod(spy_is_valid))
    monkeypatch.setattr(storage, "snapshot_database", spy_snapshot)
    monkeypatch.setattr(storage, "quick_check_at_rest", spy_outgoing_check)
    monkeypatch.setattr(storage.shutil, "copy2", no_byte_copies)

    reopened = _reopen(manager)
    monkeypatch.undo()

    full = [name for name, quick in checks if not quick]
    quick_checks = [name for name, quick in checks if quick]
    assert full == ["chat.sqlite"], "the primary gets exactly one full integrity check"
    assert len(quick_checks) == 1 and quick_checks[0].endswith(".tmp"), "only the new snapshot is re-checked"
    assert outgoing_checks == ["chat.sqlite.bak"], "the backup being rotated is checked once, bounded"
    assert snapshots == ["chat.sqlite"], "one snapshot of the primary"
    assert reopened.backup_status == ("ok", None)
    # The old backup was renamed into the older slot, byte for byte.
    assert Path(manager.previous_backup_path).read_bytes() == old_backup
    assert DatabaseManager._database_is_valid(manager.backup_path)
    assert _chat_ids(manager.backup_path) == {"thread-1"}


def test_recovering_from_the_older_generation_keeps_it_instead_of_rotating_a_bad_backup_over_it(
    tmp_path: Path,
) -> None:
    manager, _ = _manager_with_data(tmp_path)
    older_generation = Path(manager.previous_backup_path).read_bytes()
    Path(manager.db_path).write_bytes(b"corrupt-primary")
    Path(manager.backup_path).write_bytes(b"corrupt-backup")

    recovered = _reopen(manager)

    assert recovered.recovery_report is not None
    assert recovered.recovery_report.recovered_from == manager.previous_backup_path
    assert Path(manager.previous_backup_path).read_bytes() == older_generation
    assert DatabaseManager._database_is_valid(manager.backup_path)
    assert recovered.backup_status == ("ok", None)


def test_a_backup_that_cannot_take_its_place_puts_the_old_one_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old backup is set aside only after the new one is written and
    verified. If the new one is then refused its name (an antivirus scan holding
    the temporary file, say), .bak must not be left missing."""
    manager, _ = _manager_with_data(tmp_path)
    manager.update_chat_title("thread-1", "Changed after the last backup")
    backup_before = Path(manager.backup_path).read_bytes()
    real_replace = os.replace
    refused: list[str] = []
    backup = os.path.normcase(os.path.abspath(manager.backup_path))

    def replace(source, destination, *args, **kwargs):
        if (
            os.path.normcase(os.path.abspath(destination)) == backup
            and str(source).endswith(".tmp")
        ):
            # Refused on every try: a lock that lifts is retried and succeeds
            # (see tests/test_persistence.py), so this one has to stay.
            refused.append(str(source))
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)

    reopened = _reopen(manager)
    monkeypatch.undo()

    assert refused
    assert reopened.backup_status[0] == "failed"
    assert Path(manager.backup_path).read_bytes() == backup_before
    assert DatabaseManager._database_is_valid(manager.backup_path)
    assert _leftover_temporaries(tmp_path) == []
    assert _reopen(manager).backup_status == ("ok", None)


def test_a_backup_held_briefly_by_another_program_is_still_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scanner holding the backup for a few milliseconds is not a failed backup."""
    manager, _ = _manager_with_data(tmp_path)
    manager.update_chat_title("thread-1", "Changed after the last backup")
    backup_before = Path(manager.backup_path).read_bytes()
    real_replace = os.replace
    backup = os.path.normcase(os.path.abspath(manager.backup_path))
    refused: list[str] = []
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    def replace(source, destination, *args, **kwargs):
        if (
            os.path.normcase(os.path.abspath(destination)) == backup
            and str(source).endswith(".tmp")
            and len(refused) < 2
        ):
            refused.append(str(source))
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)

    reopened = _reopen(manager)
    monkeypatch.undo()

    assert len(refused) == 2
    assert reopened.backup_status == ("ok", None)
    assert Path(manager.backup_path).read_bytes() != backup_before
    assert reopened.load_chat("thread-1")["title"] == "Changed after the last backup"
    assert DatabaseManager._database_is_valid(manager.backup_path)


def _stray_files(directory: Path) -> list[str]:
    """Anything a rotation may set aside while it works and must not leave behind."""
    return sorted(entry.name for entry in directory.iterdir() if entry.name.endswith((".old", ".tmp")))


def test_a_backup_that_cannot_take_its_place_keeps_both_generations_where_they_were(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting .bak aside means renaming it over .bak.1, which used to destroy the
    older generation for good: when the new snapshot was then refused its name,
    .bak came back but .bak.1 did not, and the older verified copy was gone."""
    manager, _ = _manager_with_data(tmp_path)
    manager.update_chat_title("thread-1", "Changed after the last backup")
    newest_before = Path(manager.backup_path).read_bytes()
    older_before = Path(manager.previous_backup_path).read_bytes()
    assert newest_before != older_before
    real_replace = os.replace
    refused: list[str] = []
    backup = os.path.normcase(os.path.abspath(manager.backup_path))

    def replace(source, destination, *args, **kwargs):
        if (
            os.path.normcase(os.path.abspath(destination)) == backup
            and str(source).endswith(".tmp")
        ):
            # Refused on every try: a lock that lifts is retried and succeeds
            # (see tests/test_persistence.py), so this one has to stay.
            refused.append(str(source))
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)

    reopened = _reopen(manager)
    monkeypatch.undo()

    assert refused
    assert reopened.backup_status[0] == "failed"
    assert Path(manager.backup_path).read_bytes() == newest_before
    assert Path(manager.previous_backup_path).read_bytes() == older_before
    assert _stray_files(tmp_path) == []

    # The next launch rotates as usual, and the older generation is then let go.
    assert _reopen(manager).backup_status == ("ok", None)
    assert Path(manager.previous_backup_path).read_bytes() == newest_before
    assert _stray_files(tmp_path) == []


def test_rotation_still_works_where_the_file_system_cannot_make_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _manager_with_data(tmp_path)
    newest_before = Path(manager.backup_path).read_bytes()

    def no_hard_links(*_args, **_kwargs):
        raise OSError(errno.EPERM, "This file system does not support hard links")

    monkeypatch.setattr(os, "link", no_hard_links)

    reopened = _reopen(manager)
    monkeypatch.undo()

    assert reopened.backup_status == ("ok", None)
    assert Path(manager.previous_backup_path).read_bytes() == newest_before
    assert DatabaseManager._database_is_valid(manager.backup_path)
    assert _stray_files(tmp_path) == []


def test_a_backup_that_cannot_be_set_aside_leaves_the_older_generation_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _manager_with_data(tmp_path)
    manager.update_chat_title("thread-1", "Changed after the last backup")
    newest_before = Path(manager.backup_path).read_bytes()
    older_before = Path(manager.previous_backup_path).read_bytes()
    _refuse_renames(monkeypatch, source=manager.backup_path)

    reopened = _reopen(manager)
    monkeypatch.undo()

    assert reopened.backup_status[0] == "failed"
    assert Path(manager.backup_path).read_bytes() == newest_before
    assert Path(manager.previous_backup_path).read_bytes() == older_before
    assert _stray_files(tmp_path) == []


def test_a_rotted_newest_backup_does_not_displace_the_older_verified_generation(
    tmp_path: Path,
) -> None:
    """The outgoing .bak used to be renamed over .bak.1 without being looked at,
    so a backup that had rotted between two launches pushed the last good copy out
    and left one good backup and one rotten one."""
    manager, _ = _manager_with_data(tmp_path)
    older_generation = Path(manager.previous_backup_path).read_bytes()
    Path(manager.backup_path).write_bytes(b"rotted since the last launch")

    reopened = _reopen(manager)

    assert reopened.backup_status == ("ok", None)
    assert Path(manager.previous_backup_path).read_bytes() == older_generation
    assert DatabaseManager._database_is_valid(manager.previous_backup_path)
    assert DatabaseManager._database_is_valid(manager.backup_path)
    assert _chat_ids(manager.backup_path) == {"thread-1"}
    assert _stray_files(tmp_path) == []

    # Once .bak is healthy again the rotation carries on as before.
    healthy_backup = Path(manager.backup_path).read_bytes()
    assert _reopen(manager).backup_status == ("ok", None)
    assert Path(manager.previous_backup_path).read_bytes() == healthy_backup


def test_a_backup_with_a_damaged_page_does_not_displace_the_older_generation(
    tmp_path: Path,
) -> None:
    manager, _ = _manager_with_data(tmp_path)
    older_generation = Path(manager.previous_backup_path).read_bytes()
    damaged = bytearray(Path(manager.backup_path).read_bytes())
    page_size = 4096
    damaged[page_size : 2 * page_size] = bytes(page_size)
    Path(manager.backup_path).write_bytes(bytes(damaged))
    assert not storage.quick_check_at_rest(manager.backup_path, time_limit=30.0)

    _reopen(manager)

    assert Path(manager.previous_backup_path).read_bytes() == older_generation
    assert DatabaseManager._database_is_valid(manager.backup_path)


def test_checking_the_outgoing_backup_leaves_nothing_beside_it_and_gives_up_on_time(
    tmp_path: Path,
) -> None:
    path = tmp_path / "big.sqlite"
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("CREATE TABLE filler (body TEXT)")
        writer.executemany("INSERT INTO filler VALUES (?)", [("x" * 500,)] * 2000)
        writer.commit()
    finally:
        writer.close()

    assert storage.quick_check_at_rest(str(path), time_limit=30.0)
    assert not storage.quick_check_at_rest(str(path), time_limit=0.0), "a check that overruns is not a pass"
    assert not storage.quick_check_at_rest(str(tmp_path / "missing.sqlite"), time_limit=30.0)
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["big.sqlite"]


# -- Recovery that was interrupted ------------------------------------------


def _crash_after_the_primary_moved_aside(
    manager: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Corrupt the primary, then die between quarantining it and publishing the
    restored copy. Returns the quarantined file."""
    Path(manager.db_path).write_bytes(b"corrupt-primary")

    def die_before_publishing(cls, source, destination):
        raise _PowerLoss

    monkeypatch.setattr(DatabaseManager, "_atomic_copy_database", classmethod(die_before_publishing))
    with pytest.raises(_PowerLoss):
        _reopen(manager)
    monkeypatch.undo()

    assert not Path(manager.db_path).exists()
    (quarantined,) = Path(manager.db_path).parent.glob("chat.sqlite.corrupt-*")
    assert quarantined.read_bytes() == b"corrupt-primary"
    return quarantined


def test_a_recovery_interrupted_after_the_primary_moved_aside_is_finished_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no primary the next launch used to create an empty database, back
    it up over the good backup, and on the launch after that push the good one
    out of the second generation as well."""
    manager, original = _manager_with_data(tmp_path)
    quarantined = _crash_after_the_primary_moved_aside(manager, monkeypatch)

    recovered = _reopen(manager)

    assert recovered.load_chat("thread-1") == original
    report = recovered.recovery_report
    assert report is not None
    assert Path(report.quarantined_path) == quarantined
    assert report.recovered_from == manager.backup_path
    assert recovered.last_corrupt_path == str(quarantined)
    assert quarantined.read_bytes() == b"corrupt-primary"
    # Neither generation was replaced by a backup of an empty database.
    assert _chat_ids(manager.backup_path) == {"thread-1"}
    assert _chat_ids(manager.previous_backup_path) == {"thread-1"}
    # And the launch after that one changes nothing about that.
    _reopen(manager)
    assert _chat_ids(manager.previous_backup_path) == {"thread-1"}
    assert len(list(tmp_path.glob("chat.sqlite.corrupt-*"))) == 1


def test_finishing_an_interrupted_recovery_that_cannot_publish_leaves_no_empty_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, original = _manager_with_data(tmp_path)
    _crash_after_the_primary_moved_aside(manager, monkeypatch)
    backups_before = (
        Path(manager.backup_path).read_bytes(),
        Path(manager.previous_backup_path).read_bytes(),
    )

    def full_disk(cls, source, destination):
        raise PersistenceError("injected publish failure", operation="backup")

    monkeypatch.setattr(DatabaseManager, "_atomic_copy_database", classmethod(full_disk))
    with pytest.raises(PersistenceError, match="injected publish failure"):
        _reopen(manager)
    monkeypatch.undo()

    assert not Path(manager.db_path).exists(), "an empty database must not stand in for the lost one"
    assert (
        Path(manager.backup_path).read_bytes(),
        Path(manager.previous_backup_path).read_bytes(),
    ) == backups_before

    assert _reopen(manager).load_chat("thread-1") == original


def test_an_interrupted_recovery_does_not_replay_a_stray_log_onto_the_restored_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, original = _manager_with_data(tmp_path)
    quarantined = _crash_after_the_primary_moved_aside(manager, monkeypatch)
    stray = Path(f"{manager.db_path}-wal")
    stray.write_bytes(b"frames that belong to the quarantined database")

    recovered = _reopen(manager)

    assert recovered.load_chat("thread-1") == original
    assert Path(f"{quarantined}-wal").read_bytes() == b"frames that belong to the quarantined database"
    assert not stray.exists()


def test_a_missing_primary_with_nothing_quarantined_starts_a_new_database(tmp_path: Path) -> None:
    manager, _ = _manager_with_data(tmp_path)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{manager.db_path}{suffix}").unlink(missing_ok=True)

    fresh = _reopen(manager)

    assert fresh.recovery_report is None
    assert fresh.get_all_chats_summary() == []


def test_a_missing_primary_beside_a_quarantined_file_but_no_valid_backup_starts_a_new_one(
    tmp_path: Path,
) -> None:
    """There is nothing to restore from, so this is not a recovery it can
    finish; the quarantined file is left exactly where it is."""
    manager, _ = _manager_with_data(tmp_path)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{manager.db_path}{suffix}").unlink(missing_ok=True)
    Path(manager.backup_path).write_bytes(b"corrupt-backup")
    Path(manager.previous_backup_path).write_bytes(b"corrupt-older-backup")
    quarantined = tmp_path / f"chat.sqlite.corrupt-{'b' * 32}"
    quarantined.write_bytes(b"corrupt-primary")

    fresh = _reopen(manager)

    assert fresh.recovery_report is None
    assert quarantined.read_bytes() == b"corrupt-primary"


# -- Logs stranded by an interrupted recovery -------------------------------


def test_recovery_adopts_logs_an_interrupted_attempt_left_under_another_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first attempt moved the log and died before moving the primary. The
    second one quarantines the primary under a new name, which left the log
    under the first attempt's name with no database beside it, where
    ``sqlite3 .recover`` would never look for it."""
    manager, original = _manager_with_data(tmp_path)
    Path(manager.db_path).write_bytes(b"corrupt-primary")
    Path(f"{manager.db_path}-wal").write_bytes(b"newest committed frames")
    Path(f"{manager.db_path}-shm").write_bytes(b"shared memory index")
    real_replace = os.replace

    def die_moving_the_primary(source, destination, *args, **kwargs):
        if os.path.abspath(source) == os.path.abspath(manager.db_path):
            raise _PowerLoss
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", die_moving_the_primary)
    with pytest.raises(_PowerLoss):
        _reopen(manager)
    monkeypatch.undo()
    stranded = sorted(tmp_path.glob("chat.sqlite.corrupt-*-*"))
    assert len(stranded) == 2

    recovered = _reopen(manager)

    report = recovered.recovery_report
    assert report is not None
    quarantined = report.quarantined_path
    assert Path(f"{quarantined}-wal").read_bytes() == b"newest committed frames"
    # The shared-memory index is rebuilt state that SQLite rewrites whenever it
    # opens the file, so only its presence is preserved, not its bytes.
    assert Path(f"{quarantined}-shm").exists()
    assert sorted(report.adopted_sidecars) == sorted([f"{quarantined}-wal", f"{quarantined}-shm"])
    assert not any(entry.exists() for entry in stranded)
    assert recovered.load_chat("thread-1") == original


def test_a_log_beside_its_own_quarantined_file_is_not_adopted(tmp_path: Path) -> None:
    manager, _ = _manager_with_data(tmp_path)
    earlier = tmp_path / f"chat.sqlite.corrupt-{'a' * 32}"
    earlier.write_bytes(b"an earlier corrupt primary")
    Path(f"{earlier}-wal").write_bytes(b"its own log")
    Path(manager.db_path).write_bytes(b"corrupt-primary")

    recovered = _reopen(manager)

    assert recovered.recovery_report is not None
    assert recovered.recovery_report.adopted_sidecars == ()
    assert Path(f"{earlier}-wal").read_bytes() == b"its own log"


def test_adoption_never_overwrites_a_log_already_beside_the_quarantined_file(tmp_path: Path) -> None:
    manager, _ = _manager_with_data(tmp_path)
    orphan = tmp_path / f"chat.sqlite.corrupt-{'c' * 32}-wal"
    orphan.write_bytes(b"stranded by an earlier attempt")
    Path(manager.db_path).write_bytes(b"corrupt-primary")
    Path(f"{manager.db_path}-wal").write_bytes(b"the log of the primary being quarantined now")

    recovered = _reopen(manager)

    report = recovered.recovery_report
    assert report is not None
    assert Path(f"{report.quarantined_path}-wal").read_bytes() == b"the log of the primary being quarantined now"
    assert report.adopted_sidecars == ()
    assert orphan.read_bytes() == b"stranded by an earlier attempt"
