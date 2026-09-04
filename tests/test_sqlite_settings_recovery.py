"""Recovery tests for the settings database and its verified backups."""

import json
from pathlib import Path
import shutil
import sqlite3

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
    real_copy = shutil.copy2

    def fail_primary_copy(source, destination):
        if Path(source) == repository.db_path:
            raise OSError("injected primary snapshot failure")
        return real_copy(source, destination)

    monkeypatch.setattr(
        "cortex_backend.repositories.sqlite_settings.shutil.copy2",
        fail_primary_copy,
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
