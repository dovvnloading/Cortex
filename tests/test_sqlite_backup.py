"""The backup and recovery primitives the chat and settings stores share."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import time

import pytest

from cortex_backend.repositories import sqlite_backup
from cortex_backend.repositories.sqlite_backup import (
    failure_detail,
    move_sidecars,
    put_sidecars_back,
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
