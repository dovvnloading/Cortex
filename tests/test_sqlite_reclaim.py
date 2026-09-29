"""Returning the disk space of deleted history: what may run, and that it never costs a row."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from cortex_backend.repositories import sqlite_reclaim
from cortex_backend.repositories.sqlite_reclaim import reclaim_free_space

ROWS_KEPT = 100


def _history(path: Path, *, incremental: bool, deleted: bool = True, keep: int = ROWS_KEPT) -> None:
    """A database that held 1500 rows of about 2 KB and now holds ``keep`` of them."""
    connection = sqlite3.connect(path, isolation_level=None)
    if incremental:
        connection.execute("PRAGMA auto_vacuum = INCREMENTAL")  # only possible before WAL is switched on
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
    connection.execute("BEGIN")
    connection.executemany("INSERT INTO t (body) VALUES (?)", ((f"{index:05d}" + "x" * 2000,) for index in range(1500)))
    connection.execute("COMMIT")
    if deleted:
        connection.execute("DELETE FROM t WHERE id > ?", (keep,))
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()


class _Facts(SimpleNamespace):
    free_pages: int
    auto_vacuum: int
    file_bytes: int
    rows: list[tuple[int, str]]
    intact: bool


def _facts(path: Path) -> _Facts:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        return _Facts(
            free_pages=connection.execute("PRAGMA freelist_count").fetchone()[0],
            auto_vacuum=connection.execute("PRAGMA auto_vacuum").fetchone()[0],
            file_bytes=os.path.getsize(path),
            rows=connection.execute("SELECT id, body FROM t ORDER BY id").fetchall(),
            intact=connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
        )
    finally:
        connection.close()


def test_an_incremental_database_gives_its_free_pages_back(tmp_path: Path) -> None:
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=True)
    before = _facts(path)
    assert before.free_pages > 300 and before.auto_vacuum == 2

    assert reclaim_free_space(path) == "trimmed"

    after = _facts(path)
    assert after.free_pages == 0
    assert after.file_bytes < before.file_bytes / 5
    assert after.rows == before.rows and len(after.rows) == ROWS_KEPT
    assert after.intact


def test_a_database_made_without_auto_vacuum_is_rewritten_once_and_converted(tmp_path: Path) -> None:
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=False)
    before = _facts(path)
    assert before.free_pages > 300 and before.auto_vacuum == 0

    assert reclaim_free_space(path) == "rewritten"

    after = _facts(path)
    assert after.free_pages == 0
    assert after.file_bytes < before.file_bytes / 5
    assert after.rows == before.rows and len(after.rows) == ROWS_KEPT
    assert after.intact
    assert after.auto_vacuum == 2  # so every later start can use the cheap path


def test_a_database_that_is_mostly_full_is_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=False, deleted=False)
    before = path.read_bytes()

    assert reclaim_free_space(path) == "nothing_to_do"

    assert path.read_bytes() == before


def test_a_missing_database_is_not_created(tmp_path: Path) -> None:
    assert reclaim_free_space(tmp_path / "missing.sqlite") == "nothing_to_do"
    assert list(tmp_path.iterdir()) == []


def test_running_out_of_time_rewrites_nothing_and_a_later_run_finishes(tmp_path: Path) -> None:
    """A VACUUM that is interrupted rolls back; an incremental one keeps what it freed."""
    for incremental in (False, True):
        path = tmp_path / f"chats-{incremental}.sqlite"
        # Enough rows kept that a rewrite has real work for the clock to interrupt.
        _history(path, incremental=incremental, keep=700)
        before = _facts(path)

        assert reclaim_free_space(path, time_limit=0.0) == "gave_up"

        untouched = _facts(path)
        assert (untouched.rows, untouched.free_pages, untouched.auto_vacuum) == (
            before.rows,
            before.free_pages,
            before.auto_vacuum,
        )
        assert untouched.intact
        assert reclaim_free_space(path) in ("trimmed", "rewritten")
        assert _facts(path).rows == before.rows


def test_a_database_another_connection_is_writing_is_left_alone(tmp_path: Path) -> None:
    for incremental in (False, True):
        path = tmp_path / f"chats-{incremental}.sqlite"
        _history(path, incremental=incremental)
        before = _facts(path)
        writer = sqlite3.connect(path, isolation_level=None)
        try:
            writer.execute("BEGIN IMMEDIATE")
            outcome = reclaim_free_space(path, busy_timeout=0.05)
        finally:
            writer.execute("ROLLBACK")
            writer.close()

        assert outcome == "gave_up"
        after = _facts(path)
        assert (after.rows, after.free_pages, after.auto_vacuum) == (
            before.rows,
            before.free_pages,
            before.auto_vacuum,
        )


def test_a_rewrite_is_not_attempted_without_room_for_a_second_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=False)
    before = _facts(path)
    monkeypatch.setattr(sqlite_reclaim.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1024))

    assert reclaim_free_space(path) == "not_enough_space"

    after = _facts(path)
    assert (after.rows, after.free_pages, after.auto_vacuum) == (before.rows, before.free_pages, 0)


def test_free_space_that_cannot_be_read_is_not_room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=False)

    def unreadable(_path):
        raise OSError("no such volume")

    monkeypatch.setattr(sqlite_reclaim.shutil, "disk_usage", unreadable)

    assert reclaim_free_space(path) == "not_enough_space"
    assert _facts(path).auto_vacuum == 0


def test_a_database_too_big_to_rewrite_within_the_time_limit_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=False)
    monkeypatch.setattr(sqlite_reclaim, "MAX_VACUUM_LIVE_BYTES", 1024)

    assert reclaim_free_space(path) == "too_large"
    assert _facts(path).auto_vacuum == 0


def test_a_file_that_is_not_a_database_is_reported_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "chats.sqlite"
    path.write_bytes(b"this is not a database" * 100)
    before = path.read_bytes()

    assert reclaim_free_space(path) == "gave_up"
    assert path.read_bytes() == before
