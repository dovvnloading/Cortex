"""The backup primitives the chat and settings stores share."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import time

import pytest

from cortex_backend.repositories.sqlite_backup import failure_detail, snapshot_database


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
