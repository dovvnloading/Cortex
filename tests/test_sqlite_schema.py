"""The versioned schema ladder both SQLite stores climb, and the WAL check before it.

The chat store's own steps are covered in test_chat_groups.py and its recovery
paths in test_chat_db_recovery.py; this file covers the shared runner and the
settings store, which has the same shape with one step.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import threading

import pytest

from sqlite_faults import volume_without_write_ahead_logging
from cortex_backend.repositories import sqlite_settings
from cortex_backend.repositories.settings import SettingsRepositoryError
from cortex_backend.repositories.sqlite_schema import (
    SchemaTooNewError,
    WriteAheadLogUnavailableError,
    add_column_if_missing,
    open_for_upgrade,
    prepare_database,
    require_wal,
    stored_version,
)
from cortex_backend.repositories.sqlite_settings import SQLiteSettingsRepository


def _version(path: Path) -> int:
    probe = sqlite3.connect(path)
    try:
        return int(probe.execute("PRAGMA user_version").fetchone()[0])
    finally:
        probe.close()


def _tables(path: Path) -> set[str]:
    probe = sqlite3.connect(path)
    try:
        return {row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        probe.close()


def _journal_mode(path: Path) -> str:
    probe = sqlite3.connect(path)
    try:
        return str(probe.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        probe.close()


# -- WAL is read back, not assumed (BE-50) ---------------------------------


@pytest.mark.parametrize("mode", ["wal", "WAL", "Wal"])
def test_require_wal_accepts_write_ahead_logging_in_any_case(mode: str) -> None:
    require_wal(mode)


@pytest.mark.parametrize("mode", ["delete", "truncate", "persist", "memory", "off", ""])
def test_require_wal_rejects_every_other_journal_mode(mode: str) -> None:
    with pytest.raises(WriteAheadLogUnavailableError) as refused:
        require_wal(mode)

    assert refused.value.mode == mode


def test_no_step_runs_on_a_volume_that_cannot_do_write_ahead_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "db.sqlite"
    ran: list[int] = []
    volume_without_write_ahead_logging(monkeypatch)
    connection = open_for_upgrade(path)
    try:
        with pytest.raises(WriteAheadLogUnavailableError):
            prepare_database(connection, {1: lambda c: ran.append(1)}, target=1)
    finally:
        connection.close()
    monkeypatch.undo()

    assert ran == []
    assert _version(path) == 0
    assert _tables(path) == set()


# -- the version gate comes first -------------------------------------------


def test_a_newer_database_is_refused_before_anything_about_it_changes(tmp_path: Path) -> None:
    """Not a journal-mode switch, not a CREATE INDEX: the file is left as found."""
    path = tmp_path / "newer.sqlite"
    seed = sqlite3.connect(path)
    try:
        seed.execute("CREATE TABLE kept (x INTEGER)")
        seed.execute("PRAGMA user_version = 9")
        seed.commit()
    finally:
        seed.close()
    assert _journal_mode(path) == "delete"
    before = path.read_bytes()
    ran: list[int] = []

    connection = open_for_upgrade(path)
    try:
        with pytest.raises(SchemaTooNewError) as refused:
            prepare_database(
                connection, {1: lambda c: ran.append(1), 2: lambda c: ran.append(2)}, target=2
            )
    finally:
        connection.close()

    assert (refused.value.version, refused.value.supported) == (9, 2)
    assert ran == []
    assert path.read_bytes() == before
    assert _journal_mode(path) == "delete"


# -- the ladder ---------------------------------------------------------------


def test_each_missing_step_runs_once_in_order_and_a_second_run_does_nothing(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    ran: list[int] = []

    def step(number: int):
        def run(connection: sqlite3.Connection) -> None:
            ran.append(number)
            connection.execute(f"CREATE TABLE step_{number} (x INTEGER)")

        return run

    migrations = {1: step(1), 2: step(2), 3: step(3)}
    connection = open_for_upgrade(path)
    try:
        prepare_database(connection, migrations, target=3)
        assert stored_version(connection) == 3
        prepare_database(connection, migrations, target=3)
    finally:
        connection.close()

    assert ran == [1, 2, 3]
    assert {"step_1", "step_2", "step_3"} <= _tables(path)
    assert _journal_mode(path) == "wal"


def test_a_failing_step_is_rolled_back_and_the_steps_before_it_stay_committed(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"

    def first(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE first (x INTEGER)")

    def second(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE second (x INTEGER)")
        connection.execute("PRAGMA user_version = 2")
        raise sqlite3.OperationalError("simulated failure")

    connection = open_for_upgrade(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="simulated"):
            prepare_database(connection, {1: first, 2: second}, target=2)
        assert not connection.in_transaction
    finally:
        connection.close()

    assert _version(path) == 1
    assert _tables(path) == {"first"}


def test_a_step_another_process_already_ran_while_we_waited_is_skipped(tmp_path: Path) -> None:
    """The version is only trustworthy inside the write lock, so it is read again there."""
    path = tmp_path / "db.sqlite"
    seed = open_for_upgrade(path)
    try:
        prepare_database(seed, {}, target=0)  # only puts the file in WAL mode
    finally:
        seed.close()
    ran: list[int] = []
    waiting = threading.Event()
    outcome: list[BaseException] = []

    def worker() -> None:
        connection = open_for_upgrade(path, timeout=10.0)
        connection.set_trace_callback(
            lambda statement: waiting.set() if statement.startswith("BEGIN IMMEDIATE") else None
        )
        try:
            prepare_database(connection, {1: lambda c: ran.append(1)}, target=1)
        except BaseException as exc:  # reported on the main thread
            outcome.append(exc)
        finally:
            connection.close()

    other_process = open_for_upgrade(path)
    try:
        other_process.execute("BEGIN IMMEDIATE")
        thread = threading.Thread(target=worker, name="test-schema-worker")
        thread.start()
        assert waiting.wait(timeout=10.0), "the worker never reached its write transaction"
        other_process.execute("CREATE TABLE done_elsewhere (x INTEGER)")
        other_process.execute("PRAGMA user_version = 1")
        other_process.execute("COMMIT")
        thread.join(timeout=15.0)
        assert not thread.is_alive()
    finally:
        other_process.close()

    assert outcome == []
    assert ran == [], "the step had already been applied by the other process"
    assert _version(path) == 1


def test_add_column_if_missing_tolerates_a_column_that_is_already_there(tmp_path: Path) -> None:
    connection = open_for_upgrade(tmp_path / "db.sqlite")
    try:
        connection.execute("CREATE TABLE t (a INTEGER)")
        add_column_if_missing(connection, "t", "b", "TEXT")
        add_column_if_missing(connection, "t", "b", "TEXT")
        columns = [row[1] for row in connection.execute("PRAGMA table_info(t)")]
    finally:
        connection.close()

    assert columns == ["a", "b"]


def test_prepare_database_needs_an_autocommit_connection(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "db.sqlite")
    try:
        with pytest.raises(ValueError, match="autocommit"):
            prepare_database(connection, {}, target=0)
    finally:
        connection.close()


# -- the settings store climbs the same ladder --------------------------------


def _settings_with_backups(tmp_path: Path):
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")
    original = repository.load().settings
    repository.save(original)
    repository.save(original)
    return repository, original


def test_a_new_settings_database_is_stamped_with_the_current_version(tmp_path: Path) -> None:
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")

    assert _version(repository.db_path) == sqlite_settings.SETTINGS_DATABASE_VERSION
    assert {"cortex_settings", "settings_migration_ledger"} <= _tables(repository.db_path)
    assert _journal_mode(repository.db_path) == "wal"


def test_a_settings_database_that_predates_versioning_is_stamped_and_keeps_its_settings(
    tmp_path: Path,
) -> None:
    repository, original = _settings_with_backups(tmp_path)
    probe = sqlite3.connect(repository.db_path)
    try:
        probe.execute("PRAGMA user_version = 0")
        probe.commit()
    finally:
        probe.close()

    reopened = SQLiteSettingsRepository(repository.db_path)

    assert _version(repository.db_path) == sqlite_settings.SETTINGS_DATABASE_VERSION
    assert reopened.load().settings == original


def test_a_newer_settings_database_is_refused_untouched_and_its_backups_are_not_rotated(
    tmp_path: Path,
) -> None:
    """An older release used to rotate the older-schema backups away on every
    launch before it got as far as refusing the file."""
    repository, _ = _settings_with_backups(tmp_path)
    probe = sqlite3.connect(repository.db_path)
    try:
        probe.execute("PRAGMA user_version = 9")
        probe.commit()
    finally:
        probe.close()
    before = {
        path: path.read_bytes()
        for path in (repository.db_path, repository.backup_path, repository.previous_backup_path)
    }

    with pytest.raises(SettingsRepositoryError, match="newer Cortex release") as refused:
        SQLiteSettingsRepository(repository.db_path)

    assert str(tmp_path) not in str(refused.value)
    assert {path: path.read_bytes() for path in before} == before
    assert not [entry.name for entry in tmp_path.iterdir() if ".tmp" in entry.name]


def test_a_failing_settings_step_leaves_the_version_and_tables_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.sqlite"

    def failing_step(connection: sqlite3.Connection) -> None:
        sqlite_settings._migrate_to_v1(connection)
        raise sqlite3.OperationalError("simulated failure")

    monkeypatch.setitem(sqlite_settings._MIGRATIONS, 1, failing_step)
    with pytest.raises(SettingsRepositoryError, match="initialize settings schema"):
        SQLiteSettingsRepository(path)
    monkeypatch.undo()

    assert _version(path) == 0
    assert _tables(path) == set()
    assert SQLiteSettingsRepository(path).load().settings is not None


def test_the_settings_ladder_has_a_step_for_every_version() -> None:
    assert sorted(sqlite_settings._MIGRATIONS) == list(
        range(1, sqlite_settings.SETTINGS_DATABASE_VERSION + 1)
    )


def test_a_settings_volume_without_write_ahead_logging_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.sqlite"
    volume_without_write_ahead_logging(monkeypatch)

    with pytest.raises(SettingsRepositoryError, match="write-ahead logging") as refused:
        SQLiteSettingsRepository(path)
    monkeypatch.undo()

    assert "--data-dir" in str(refused.value)
    assert str(tmp_path) not in str(refused.value)
    assert _version(path) == 0
    assert _tables(path) == set()


def test_settings_synchronous_normal_is_only_applied_once_write_ahead_logging_is_confirmed(
    tmp_path: Path,
) -> None:
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")
    normal, full = 1, 2

    with repository.connect() as connection:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == normal

    repository._wal_confirmed = False
    with repository.connect() as connection:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == full


# -- settings recovery that was interrupted -----------------------------------


class _PowerLoss(BaseException):
    """Stands in for the process dying: nothing after it gets to run."""


def test_a_settings_recovery_interrupted_after_the_primary_moved_aside_is_finished_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, original = _settings_with_backups(tmp_path)
    repository.db_path.write_bytes(b"corrupt-primary")

    def die_before_publishing(cls, source, destination):
        raise _PowerLoss

    monkeypatch.setattr(
        SQLiteSettingsRepository, "_atomic_copy_database", classmethod(die_before_publishing)
    )
    with pytest.raises(_PowerLoss):
        SQLiteSettingsRepository(repository.db_path)
    monkeypatch.undo()
    assert not repository.db_path.exists()
    (quarantined,) = tmp_path.glob("settings.sqlite.corrupt-*")

    recovered = SQLiteSettingsRepository(repository.db_path)

    assert recovered.load().settings == original
    report = recovered.recovery_report
    assert report is not None
    assert Path(report.quarantined_path) == quarantined
    assert report.recovered_from == str(repository.backup_path)
    assert quarantined.read_bytes() == b"corrupt-primary"
    assert len(list(tmp_path.glob("settings.sqlite.corrupt-*"))) == 1


def test_finishing_an_interrupted_settings_recovery_that_cannot_publish_leaves_no_empty_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, original = _settings_with_backups(tmp_path)
    repository.db_path.write_bytes(b"corrupt-primary")

    def die_before_publishing(cls, source, destination):
        raise _PowerLoss

    monkeypatch.setattr(
        SQLiteSettingsRepository, "_atomic_copy_database", classmethod(die_before_publishing)
    )
    with pytest.raises(_PowerLoss):
        SQLiteSettingsRepository(repository.db_path)
    monkeypatch.undo()
    backups_before = (repository.backup_path.read_bytes(), repository.previous_backup_path.read_bytes())

    def cannot_publish(cls, source, destination):
        raise SettingsRepositoryError("injected publish failure")

    monkeypatch.setattr(SQLiteSettingsRepository, "_atomic_copy_database", classmethod(cannot_publish))
    with pytest.raises(SettingsRepositoryError, match="injected publish failure"):
        SQLiteSettingsRepository(repository.db_path)
    monkeypatch.undo()

    assert not repository.db_path.exists()
    assert (
        repository.backup_path.read_bytes(),
        repository.previous_backup_path.read_bytes(),
    ) == backups_before
    assert SQLiteSettingsRepository(repository.db_path).load().settings == original


def test_a_missing_settings_primary_with_nothing_quarantined_starts_a_new_database(
    tmp_path: Path,
) -> None:
    repository, _ = _settings_with_backups(tmp_path)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{repository.db_path}{suffix}").unlink(missing_ok=True)

    fresh = SQLiteSettingsRepository(repository.db_path)

    assert fresh.recovery_report is None


def test_settings_recovery_adopts_a_log_an_interrupted_attempt_left_under_another_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, original = _settings_with_backups(tmp_path)
    repository.db_path.write_bytes(b"corrupt-primary")
    Path(f"{repository.db_path}-wal").write_bytes(b"newest committed frames")
    real_replace = os.replace

    def die_moving_the_primary(source, destination, *args, **kwargs):
        if os.path.abspath(source) == os.path.abspath(repository.db_path):
            raise _PowerLoss
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", die_moving_the_primary)
    with pytest.raises(_PowerLoss):
        SQLiteSettingsRepository(repository.db_path)
    monkeypatch.undo()
    stranded = list(tmp_path.glob("settings.sqlite.corrupt-*-*"))
    assert any(entry.name.endswith("-wal") for entry in stranded)

    recovered = SQLiteSettingsRepository(repository.db_path)

    report = recovered.recovery_report
    assert report is not None
    adopted = Path(f"{report.quarantined_path}-wal")
    assert adopted.read_bytes() == b"newest committed frames"
    assert str(adopted) in report.adopted_sidecars
    assert all(item.startswith(report.quarantined_path) for item in report.adopted_sidecars)
    assert not any(entry.exists() for entry in stranded)
    assert recovered.load().settings == original
