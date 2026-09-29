"""Returning the disk space of deleted history: what may run, and that it never costs a row."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest

from cortex_backend.repositories import sqlite_reclaim
from cortex_backend.repositories.sqlite_reclaim import BACKOFF_SECONDS, reclaim_free_space

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
        # An interrupted rewrite is not retried at once (see the back-off tests
        # below); a later launch, past the back-off, finishes it.
        assert reclaim_free_space(path, wall_clock=lambda: time.time() + BACKOFF_SECONDS + 1) in (
            "trimmed",
            "rewritten",
        )
        assert _facts(path).rows == before.rows


def test_a_step_that_frees_nothing_ends_the_pass_instead_of_spinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=True)
    real_pragma = sqlite_reclaim._pragma
    steps: list[str] = []

    def stuck(connection: sqlite3.Connection, name: str) -> int:
        if name == "freelist_count":
            steps.append(name)
            return 500  # the free list never shrinks
        return real_pragma(connection, name)

    monkeypatch.setattr(sqlite_reclaim, "_pragma", stuck)

    assert reclaim_free_space(path, time_limit=5.0) == "gave_up"
    assert len(steps) <= 3  # the check, one step, the check that saw no progress


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


# -- a rewrite that fails is not retried at every launch -------------------------------


def _marker(path: Path) -> Path:
    return Path(f"{path}.reclaim-backoff")


class _Rewrites:
    """Counts the full rewrites a test lets through, without changing what they do."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real = sqlite_reclaim._rewrite

        def counted(connection: sqlite3.Connection):
            self.count += 1
            return real(connection)

        monkeypatch.setattr(sqlite_reclaim, "_rewrite", counted)


def test_a_rewrite_that_ran_out_of_time_is_not_tried_again_until_the_backoff_has_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this, a file whose rewrite needs more than the limit adds the limit to every launch."""
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=False, keep=700)
    before = _facts(path)
    rewrites = _Rewrites(monkeypatch)
    failed_at = 1_000_000.0

    assert reclaim_free_space(path, time_limit=0.0, wall_clock=lambda: failed_at) == "gave_up"
    assert rewrites.count == 1
    assert _marker(path).read_text(encoding="ascii").strip() == str(int(failed_at))

    # Every launch for the next week costs nothing and touches nothing.
    for launch in (1, 3600, BACKOFF_SECONDS - 1):
        now = failed_at + launch
        assert reclaim_free_space(path, wall_clock=lambda now=now: now) == "backed_off"
    assert rewrites.count == 1
    assert (_facts(path).rows, _facts(path).free_pages, _facts(path).auto_vacuum) == (
        before.rows,
        before.free_pages,
        0,
    )

    # After it, the rewrite is tried again, finishes, and the note of the failure goes.
    assert reclaim_free_space(path, wall_clock=lambda: failed_at + BACKOFF_SECONDS) == "rewritten"
    assert rewrites.count == 2
    after = _facts(path)
    assert (after.free_pages, after.auto_vacuum, after.rows, after.intact) == (0, 2, before.rows, True)
    assert not _marker(path).exists()


def test_a_rewrite_that_fails_for_any_other_reason_is_backed_off_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=False)

    def full_disk(_connection: sqlite3.Connection):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(sqlite_reclaim, "_rewrite", full_disk)
    assert reclaim_free_space(path, wall_clock=lambda: 5_000.0) == "gave_up"
    assert _marker(path).exists()
    monkeypatch.undo()

    assert reclaim_free_space(path, wall_clock=lambda: 5_001.0) == "backed_off"
    assert _facts(path).auto_vacuum == 0


def test_a_rewrite_held_off_by_another_writer_is_not_backed_off(tmp_path: Path) -> None:
    """A lock says nothing about the rewrite: the next launch may well succeed."""
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=False)
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        assert reclaim_free_space(path, busy_timeout=0.05) == "gave_up"
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    assert not _marker(path).exists()
    assert reclaim_free_space(path) == "rewritten"


@pytest.mark.parametrize(
    "content",
    ["not a time", "", "\x00\x01", "1e999999", "-1", "99999999999"],
    ids=["text", "empty", "binary", "overflow", "ancient", "in-the-future"],
)
def test_a_marker_that_says_nothing_usable_does_not_hold_the_rewrite_off(tmp_path: Path, content: str) -> None:
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=False)
    _marker(path).write_bytes(content.encode("ascii"))

    assert reclaim_free_space(path, wall_clock=lambda: 1_000_000.0) == "rewritten"

    assert not _marker(path).exists()


def test_the_backoff_does_not_slow_the_incremental_path(tmp_path: Path) -> None:
    """Only the whole-file rewrite is repeated cost; incremental steps keep what they freed."""
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=True)
    _marker(path).write_text(f"{int(time.time())}\n", encoding="ascii")

    assert reclaim_free_space(path) == "trimmed"


def test_a_marker_that_cannot_be_written_or_read_is_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=False, keep=700)
    _marker(path).mkdir()  # nothing can be written to, or read from, this name

    assert reclaim_free_space(path, time_limit=0.0) == "gave_up"
    assert reclaim_free_space(path, wall_clock=lambda: time.time() + BACKOFF_SECONDS + 1) == "rewritten"


def test_a_database_with_nothing_to_reclaim_leaves_no_marker(tmp_path: Path) -> None:
    path = tmp_path / "chats.sqlite"
    _history(path, incremental=False, deleted=False)

    assert reclaim_free_space(path, time_limit=0.0) == "nothing_to_do"

    assert not _marker(path).exists()
