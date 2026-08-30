"""Recovery tests for the settings database and its verified backups."""

from pathlib import Path
import shutil

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
