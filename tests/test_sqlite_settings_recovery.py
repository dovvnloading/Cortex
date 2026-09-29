"""Recovery tests for the settings database and its verified backups."""

import errno
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from cortex_backend.core.settings import CortexSettings
from cortex_backend.repositories.sqlite_settings import SQLiteSettingsRepository
from cortex_backend.repositories.settings import SettingsRepositoryError
from cortex_backend.repositories.settings import SettingsRevisionConflict


def _repository_with_valid_backup(tmp_path: Path) -> tuple[SQLiteSettingsRepository, CortexSettings]:
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")
    original = repository.load().settings
    repository.save(original)
    updated = original.model_copy(
        update={"appearance": original.appearance.model_copy(update={"theme": "light"})}
    )
    repository.save(updated)
    return repository, original


def test_corrupt_primary_recovers_without_overwriting_valid_backup(tmp_path: Path):
    repository, original = _repository_with_valid_backup(tmp_path)
    backup_before = repository.backup_path.read_bytes()
    repository.db_path.write_bytes(b"corrupt-primary")

    recovered = SQLiteSettingsRepository(repository.db_path)

    assert recovered.load().settings == original
    assert repository.backup_path.read_bytes() == backup_before
    assert recovered.last_corrupt_path is not None
    assert recovered.last_corrupt_path.read_bytes() == b"corrupt-primary"


def test_corrupt_primary_without_valid_backup_fails_closed_and_preserves_files(tmp_path: Path):
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")
    repository.db_path.write_bytes(b"corrupt-primary")

    with pytest.raises(SettingsRepositoryError, match="corrupt"):
        SQLiteSettingsRepository(repository.db_path)

    assert repository.db_path.read_bytes() == b"corrupt-primary"
    assert not repository.backup_path.exists()


def test_failed_recovery_restores_corrupt_primary_from_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, _ = _repository_with_valid_backup(tmp_path)
    repository.db_path.write_bytes(b"corrupt-primary")

    def fail_recovery_copy(cls, source, destination):
        raise SettingsRepositoryError("injected recovery failure")

    monkeypatch.setattr(
        SQLiteSettingsRepository,
        "_atomic_copy_database",
        classmethod(fail_recovery_copy),
    )

    with pytest.raises(SettingsRepositoryError, match="injected recovery failure"):
        SQLiteSettingsRepository(repository.db_path)

    assert repository.db_path.read_bytes() == b"corrupt-primary"
    assert not list(tmp_path.glob("settings.sqlite.corrupt-*"))


def test_corrupt_primary_and_backup_fail_without_overwriting_either_file(tmp_path: Path):
    repository, _ = _repository_with_valid_backup(tmp_path)
    repository.db_path.write_bytes(b"corrupt-primary")
    repository.backup_path.write_bytes(b"corrupt-backup")
    repository.previous_backup_path.write_bytes(b"corrupt-older-backup")

    with pytest.raises(SettingsRepositoryError, match="corrupt"):
        SQLiteSettingsRepository(repository.db_path)

    assert repository.db_path.read_bytes() == b"corrupt-primary"
    assert repository.backup_path.read_bytes() == b"corrupt-backup"
    assert repository.previous_backup_path.read_bytes() == b"corrupt-older-backup"


def test_save_rejects_corrupt_primary_before_rotating_valid_backup(tmp_path: Path):
    repository, original = _repository_with_valid_backup(tmp_path)
    backup_before = repository.backup_path.read_bytes()
    repository.db_path.write_bytes(b"corrupt-primary")

    with pytest.raises(SettingsRepositoryError, match="invalid database"):
        repository.save(original)

    assert repository.backup_path.read_bytes() == backup_before


def test_failed_backup_copy_preserves_current_recovery_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, original = _repository_with_valid_backup(tmp_path)
    primary_before = repository.db_path.read_bytes()
    backup_before = repository.backup_path.read_bytes()

    def fail_primary_snapshot(source, destination, **kwargs):
        raise OSError("injected primary snapshot failure")

    monkeypatch.setattr(
        "cortex_backend.repositories.sqlite_settings.snapshot_database",
        fail_primary_snapshot,
    )

    with pytest.raises(SettingsRepositoryError, match="safely"):
        repository.save(original)

    assert repository.db_path.read_bytes() == primary_before
    assert repository.backup_path.read_bytes() == backup_before


def test_recovery_falls_back_to_older_verified_backup_generation(tmp_path: Path):
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")
    original = repository.load().settings
    repository.save(original)
    updated = original.model_copy(
        update={"appearance": original.appearance.model_copy(update={"theme": "light"})}
    )
    repository.save(updated)
    newest = updated.model_copy(
        update={"appearance": updated.appearance.model_copy(update={"theme": "system"})}
    )
    repository.save(newest)

    repository.db_path.write_bytes(b"corrupt-primary")
    repository.backup_path.write_bytes(b"corrupt-current-backup")

    recovered = SQLiteSettingsRepository(repository.db_path)

    assert recovered.load().settings == original


def test_save_compare_and_swap_rejects_stale_revision_without_overwrite(tmp_path: Path):
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")
    original = repository.load().settings
    first = original.model_copy(
        update={
            "revision": 1,
            "appearance": original.appearance.model_copy(update={"theme": "light"}),
        }
    )
    stale = original.model_copy(
        update={
            "revision": 1,
            "appearance": original.appearance.model_copy(update={"theme": "system"}),
        }
    )

    repository.save(first, expected_revision=0)
    with pytest.raises(SettingsRevisionConflict):
        repository.save(stale, expected_revision=0)

    assert repository.load().settings == first


def _payload_revision(database: Path) -> int:
    """Read the stored revision straight from a settings file on disk."""
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("SELECT revision FROM cortex_settings WHERE id = 1").fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def test_settings_database_uses_wal_with_normal_synchronous(tmp_path: Path):
    """synchronous = NORMAL is only crash-safe under WAL; assert both together."""
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")

    with repository.connect() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert journal_mode.lower() == "wal"
    assert int(synchronous) == 1


def test_backup_captures_writes_that_are_still_only_in_the_write_ahead_log(tmp_path: Path):
    """A backup is a file copy, so it must checkpoint the WAL before copying.

    While any other connection holds the database open -- routine for an API
    serving concurrent requests -- SQLite keeps committed pages in the -wal
    sidecar. Copying the primary file alone at that moment produces a backup
    with no settings row in it at all.
    """
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")
    original = repository.load().settings

    concurrent_reader = sqlite3.connect(repository.db_path)
    try:
        # sqlite3 opens the file lazily; read once so the connection is really
        # attached and SQLite has to keep the sidecar alive.
        concurrent_reader.execute("SELECT id FROM cortex_settings").fetchall()

        repository.save(original.model_copy(update={"revision": 1}), expected_revision=0)
        assert Path(f"{repository.db_path}-wal").exists(), "expected a live write-ahead log"
        # save() rotates the backup before writing, so this call is what must
        # capture revision 1.
        repository.save(original.model_copy(update={"revision": 2}), expected_revision=1)

        assert _payload_revision(repository.backup_path) == 1
    finally:
        concurrent_reader.close()


def test_restore_discards_the_replaced_databases_write_ahead_log(tmp_path: Path):
    """A restored file must not inherit sidecars describing the old database.

    An unclean shutdown can leave a -wal and -shm behind. Once the primary is
    replaced from a backup they describe a database that no longer exists, and
    the next connection would replay them onto the replacement.
    """
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")
    original = repository.load().settings
    repository.save(original.model_copy(update={"revision": 1}), expected_revision=0)
    repository.save(original.model_copy(update={"revision": 2}), expected_revision=1)

    stale_wal = Path(f"{repository.db_path}-wal")
    stale_shm = Path(f"{repository.db_path}-shm")
    stale_wal.write_bytes(b"leftover write-ahead log")
    stale_shm.write_bytes(b"leftover shared memory index")

    repository.restore_backup()

    assert not stale_wal.exists()
    assert not stale_shm.exists()
    assert repository.load().settings.revision == 1


def test_a_workspace_written_before_suggestions_was_removed_still_loads(tmp_path: Path):
    """CortexSettings is extra="forbid", so a retired key is a hard failure.

    Every existing install has "suggestions" in its stored payload. Without
    dropping retired keys on read, upgrading would surface as "Stored Cortex
    settings are invalid" and lose a real workspace's settings over a field
    nothing has used for a long time.
    """
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")
    original = repository.load().settings
    repository.save(original.model_copy(update={"revision": 1}), expected_revision=0)

    # Write back exactly what an older build would have stored.
    stored = json.loads(json.dumps(original.model_dump(mode="json")))
    stored["revision"] = 1
    stored["suggestions"] = {"enabled": False, "model": "qwen3:4b"}
    with repository.connect() as connection:
        connection.execute(
            "UPDATE cortex_settings SET payload = ? WHERE id = 1",
            (json.dumps(stored),),
        )

    reopened = SQLiteSettingsRepository(repository.db_path)
    loaded = reopened.load().settings

    assert loaded.revision == 1
    assert not hasattr(loaded, "suggestions")
    # The next save writes the current shape, so the key does not come back.
    reopened.save(loaded.model_copy(update={"revision": 2}), expected_revision=1)
    with reopened.connect() as connection:
        payload = connection.execute(
            "SELECT payload FROM cortex_settings WHERE id = 1"
        ).fetchone()[0]
    assert "suggestions" not in json.loads(payload)


# -- BE-46: a failed startup backup must not make Cortex unlaunchable -------

SETTINGS_MODULE = "cortex_backend.repositories.sqlite_settings"


def _leftover_temporaries(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir() if ".tmp" in entry.name)


def test_a_failed_startup_backup_does_not_block_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repository, _ = _repository_with_valid_backup(tmp_path)
    backup_before = repository.backup_path.read_bytes()

    def fail_copy(cls, source, destination):
        raise SettingsRepositoryError("injected copy failure")

    monkeypatch.setattr(SQLiteSettingsRepository, "_atomic_copy_database", classmethod(fail_copy))

    reopened = SQLiteSettingsRepository(repository.db_path)
    monkeypatch.undo()

    assert reopened.backup_status[0] == "failed"
    assert reopened.load().settings.appearance.theme == "light"
    assert repository.backup_path.read_bytes() == backup_before
    # Without the fault, the very next save works and reports a good backup.
    reopened.save(reopened.load().settings)
    assert reopened.backup_status == ("ok", None)


def _full_disk_while_snapshotting(monkeypatch: pytest.MonkeyPatch, repository) -> None:
    def full(*_args, **_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(f"{SETTINGS_MODULE}.snapshot_database", full)


def _full_disk_while_rotating(monkeypatch: pytest.MonkeyPatch, repository) -> None:
    def full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(f"{SETTINGS_MODULE}.shutil.copy2", full)


def _backup_file_locked_by_another_program(monkeypatch: pytest.MonkeyPatch, repository) -> None:
    real_replace = os.replace
    locked = os.path.normcase(os.path.abspath(repository.backup_path))

    def replace(source, destination, *args, **kwargs):
        if os.path.normcase(os.path.abspath(destination)) == locked:
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def _snapshot_that_fails_verification(monkeypatch: pytest.MonkeyPatch, repository) -> None:
    def torn(_source, destination, **_kwargs):
        Path(destination).write_bytes(b"torn snapshot")

    monkeypatch.setattr(f"{SETTINGS_MODULE}.snapshot_database", torn)


_BACKUP_FAILURES = [
    _full_disk_while_snapshotting,
    _full_disk_while_rotating,
    _backup_file_locked_by_another_program,
    _snapshot_that_fails_verification,
]
_BACKUP_FAILURE_IDS = ["disk-full-snapshot", "disk-full-rotation", "backup-locked", "snapshot-corrupt"]


@pytest.mark.parametrize("inject", _BACKUP_FAILURES, ids=_BACKUP_FAILURE_IDS)
def test_startup_survives_every_way_the_backup_can_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inject
):
    """The primary is healthy, so Cortex must start and the existing backups
    must come through untouched -- then the next launch backs up normally."""
    repository, _ = _repository_with_valid_backup(tmp_path)
    backup_before = repository.backup_path.read_bytes()
    inject(monkeypatch, repository)

    reopened = SQLiteSettingsRepository(repository.db_path)
    monkeypatch.undo()

    state, detail = reopened.backup_status
    assert state == "failed"
    assert detail and str(tmp_path) not in detail
    assert reopened.load().settings.appearance.theme == "light"
    assert repository.backup_path.read_bytes() == backup_before
    assert SQLiteSettingsRepository._database_is_valid(repository.backup_path)
    assert SQLiteSettingsRepository._database_is_valid(repository.previous_backup_path)
    assert _leftover_temporaries(tmp_path) == []

    healthy = SQLiteSettingsRepository(repository.db_path)
    assert healthy.backup_status == ("ok", None)
    assert repository.backup_path.read_bytes() != backup_before


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to replace an open file")
def test_a_backup_file_held_open_by_another_program_does_not_block_startup(tmp_path: Path):
    repository, _ = _repository_with_valid_backup(tmp_path)
    backup_before = repository.backup_path.read_bytes()

    with open(repository.backup_path, "rb"):
        reopened = SQLiteSettingsRepository(repository.db_path)

    assert reopened.backup_status[0] == "failed"
    assert reopened.load().settings.appearance.theme == "light"
    assert repository.backup_path.read_bytes() == backup_before
    assert _leftover_temporaries(tmp_path) == []


def test_a_failed_save_time_backup_still_refuses_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Only the startup backup is optional. A save is an explicit user action
    with an error path, and it keeps requiring its rollback copy."""
    repository, original = _repository_with_valid_backup(tmp_path)
    stored_revision = _payload_revision(repository.db_path)
    _full_disk_while_snapshotting(monkeypatch, repository)

    with pytest.raises(SettingsRepositoryError, match="safely"):
        repository.save(original.model_copy(update={"revision": 9}))
    monkeypatch.undo()

    assert _payload_revision(repository.db_path) == stored_revision
    assert repository.backup_status[0] == "failed"
    repository.save(original.model_copy(update={"revision": 9}))
    assert _payload_revision(repository.db_path) == 9
    assert repository.backup_status == ("ok", None)


def test_a_corrupt_primary_still_refuses_to_start_without_a_usable_backup(tmp_path: Path):
    """Non-fatal backups must not loosen the recovery path: that stays closed."""
    repository, _ = _repository_with_valid_backup(tmp_path)
    repository.db_path.write_bytes(b"corrupt-primary")
    repository.backup_path.write_bytes(b"corrupt-backup")
    repository.previous_backup_path.write_bytes(b"corrupt-older-backup")

    with pytest.raises(SettingsRepositoryError, match="no valid backup"):
        SQLiteSettingsRepository(repository.db_path)

    assert repository.db_path.read_bytes() == b"corrupt-primary"


# -- BE-48: the backup must include commits still in the write-ahead log ----


def test_backup_includes_commits_a_concurrent_reader_keeps_in_the_wal(tmp_path: Path):
    """While a reader pins the log, wal_checkpoint(TRUNCATE) cannot finish and
    reports it only through its return value, so a file copy of the main
    database would be an older -- yet perfectly valid -- state."""
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")
    original = repository.load().settings
    reader = sqlite3.connect(repository.db_path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT id FROM cortex_settings").fetchall()

        repository.save(original.model_copy(update={"revision": 1}), expected_revision=0)
        assert Path(f"{repository.db_path}-wal").stat().st_size > 0

        repository._create_backup()
    finally:
        reader.close()

    assert _payload_revision(repository.backup_path) == 1
