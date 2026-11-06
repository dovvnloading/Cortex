"""The backup and recovery primitives the chat and settings stores share."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import time

import pytest

from cortex_backend.repositories import sqlite_backup
from cortex_backend.repositories.sqlite_backup import (
    REPLACE_ATTEMPTS,
    failure_detail,
    move_sidecars,
    put_sidecars_back,
    replace_with_retry,
    snapshot_database,
)


def _rows(path: Path) -> list[int]:
    connection = sqlite3.connect(path)
    try:
        return [row[0] for row in connection.execute("SELECT x FROM t ORDER BY x")]
    finally:
        connection.close()


def _wal_database(path: Path) -> sqlite3.Connection:
    # A short busy timeout: the checkpoint below is meant to lose to the reader.
    connection = sqlite3.connect(path, timeout=0.1)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t (x INTEGER)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.commit()
    return connection


def test_a_snapshot_includes_commits_a_pinned_reader_keeps_in_the_wal(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    writer = _wal_database(source)
    reader = sqlite3.connect(source)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM t").fetchall()
        writer.execute("INSERT INTO t VALUES (2)")
        writer.commit()
        # The failure mode this exists to avoid: the checkpoint reports busy
        # through its return value, leaves the frame in the log, and raises
        # nothing.
        busy, _log, _checkpointed = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert busy == 1

        destination = tmp_path / "copy.sqlite"
        snapshot_database(source, destination)
    finally:
        reader.close()
        writer.close()

    assert _rows(destination) == [1, 2]


def test_a_snapshot_gives_up_on_a_source_that_stays_locked(tmp_path: Path) -> None:
    """Python's backup() retries for as long as SQLite says busy, which on a
    locked source would hang startup forever."""
    source = tmp_path / "source.sqlite"
    holder = sqlite3.connect(source)
    holder.execute("CREATE TABLE t (x INTEGER)")
    holder.commit()
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            snapshot_database(source, tmp_path / "copy.sqlite", wait_seconds=0.3)
        assert time.monotonic() - started < 10
    finally:
        holder.rollback()
        holder.close()


def test_a_snapshot_never_creates_a_missing_source(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"

    with pytest.raises(sqlite3.OperationalError):
        snapshot_database(missing, tmp_path / "copy.sqlite")

    assert not missing.exists()


def test_failure_detail_keeps_the_error_type_but_never_its_message() -> None:
    error = PermissionError(13, "Access is denied: 'C:\\Users\\someone\\private\\chat.sqlite'")

    detail = failure_detail("Could not copy the database.", error)

    assert detail == "Could not copy the database. (PermissionError)"
    assert "someone" not in detail
    assert failure_detail("Could not copy the database.", None) == "Could not copy the database."


def _database_with_sidecars(directory: Path, name: str) -> Path:
    database = directory / name
    database.write_bytes(b"main file")
    Path(f"{database}-wal").write_bytes(b"write-ahead log")
    Path(f"{database}-shm").write_bytes(b"shared memory")
    return database


def test_move_sidecars_renames_both_beside_the_destination_and_reports_the_moves(
    tmp_path: Path,
) -> None:
    database = _database_with_sidecars(tmp_path, "live.sqlite")
    destination = tmp_path / "live.sqlite.corrupt-1"

    moved = move_sidecars(database, destination)

    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    assert Path(f"{destination}-wal").read_bytes() == b"write-ahead log"
    assert Path(f"{destination}-shm").read_bytes() == b"shared memory"
    assert len(moved) == 2

    put_sidecars_back(moved)

    assert Path(f"{database}-wal").read_bytes() == b"write-ahead log"
    assert Path(f"{database}-shm").read_bytes() == b"shared memory"
    assert not Path(f"{destination}-wal").exists()


def test_move_sidecars_tolerates_a_database_with_no_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "live.sqlite"
    database.write_bytes(b"main file")

    assert move_sidecars(database, tmp_path / "elsewhere") == []


def test_move_sidecars_never_overwrites_a_preserved_sidecar(tmp_path: Path) -> None:
    database = _database_with_sidecars(tmp_path, "live.sqlite")
    destination = tmp_path / "quarantine"
    Path(f"{destination}-wal").write_bytes(b"an earlier preserved log")

    with pytest.raises(FileExistsError):
        move_sidecars(database, destination)

    assert Path(f"{destination}-wal").read_bytes() == b"an earlier preserved log"
    assert Path(f"{database}-wal").read_bytes() == b"write-ahead log"
    assert Path(f"{database}-shm").read_bytes() == b"shared memory"


def test_move_sidecars_undoes_a_partial_move_when_a_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database_with_sidecars(tmp_path, "live.sqlite")
    real_replace = os.replace

    def replace(source, destination, *args, **kwargs):
        if str(source).endswith("-shm") and "quarantine" in str(destination):
            raise PermissionError(13, "in use")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(sqlite_backup.os, "replace", replace)

    with pytest.raises(PermissionError):
        move_sidecars(database, tmp_path / "quarantine")
    monkeypatch.undo()

    assert Path(f"{database}-wal").read_bytes() == b"write-ahead log"
    assert Path(f"{database}-shm").read_bytes() == b"shared memory"
    assert not list(tmp_path.glob("quarantine*"))


def test_put_sidecars_back_never_overwrites_a_file_at_the_original_name(tmp_path: Path) -> None:
    database = _database_with_sidecars(tmp_path, "live.sqlite")
    destination = tmp_path / "quarantine"
    moved = move_sidecars(database, destination)
    # Something (another database) has since claimed the original log name.
    Path(f"{database}-wal").write_bytes(b"belongs to a different database")

    with pytest.raises(FileExistsError):
        put_sidecars_back(moved)

    assert Path(f"{database}-wal").read_bytes() == b"belongs to a different database"
    assert Path(f"{destination}-wal").read_bytes() == b"write-ahead log"
    # The other sidecar had no such conflict and still went back.
    assert Path(f"{database}-shm").read_bytes() == b"shared memory"


# -- replace_with_retry --------------------------------------------------------


def _refuse_replacing_until(monkeypatch: pytest.MonkeyPatch, *, refusals: int, error=PermissionError):
    """Make os.replace fail ``refusals`` times, then work; record the waits between tries."""
    real_replace = os.replace
    calls: list[str] = []
    waits: list[float] = []

    def replace(source, destination, *args, **kwargs):
        calls.append(str(destination))
        if len(calls) <= refusals:
            raise error(13, "The process cannot access the file because it is being used")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(time, "sleep", waits.append)
    return calls, waits


def test_a_sharing_violation_that_lifts_is_waited_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, destination = tmp_path / "new", tmp_path / "current"
    source.write_bytes(b"new")
    destination.write_bytes(b"current")
    calls, waits = _refuse_replacing_until(monkeypatch, refusals=2)

    replace_with_retry(source, destination)

    assert destination.read_bytes() == b"new"
    assert not source.exists()
    assert len(calls) == 3
    assert waits == [0.05, 0.1]  # doubling, and only between tries


def test_a_sharing_violation_that_does_not_lift_is_raised_after_a_bounded_number_of_tries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = tmp_path / "new", tmp_path / "current"
    source.write_bytes(b"new")
    destination.write_bytes(b"current")
    calls, waits = _refuse_replacing_until(monkeypatch, refusals=10_000)

    with pytest.raises(PermissionError):
        replace_with_retry(source, destination)

    assert len(calls) == REPLACE_ATTEMPTS
    assert len(waits) == REPLACE_ATTEMPTS - 1
    assert sum(waits) < 1.0  # a lock that stays is reported promptly
    # Nothing was moved: the file that was there is intact and so is the new one.
    assert destination.read_bytes() == b"current"
    assert source.read_bytes() == b"new"


def test_an_error_other_than_a_sharing_violation_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, waits = _refuse_replacing_until(monkeypatch, refusals=10_000, error=FileNotFoundError)

    with pytest.raises(FileNotFoundError):
        replace_with_retry(tmp_path / "missing", tmp_path / "current")

    assert len(calls) == 1
    assert waits == []
