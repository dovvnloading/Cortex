"""A damaged execution store must not take the whole application with it.

execution.sqlite holds only transient bookkeeping -- jobs, events, leases,
artifact rows -- and is written on every job, every event and every lease
renewal, so it is the store most exposed to an unclean shutdown. It is also
the first dependency build_app constructs, and unlike the chat and settings
stores it has no backup and no recovery path. A torn page therefore stopped
the app launching at all: no chat, no settings, nothing.

Nothing in it is authored by the user, so it is rebuilt rather than restored --
but only when the file is *known* to be unusable: corrupt, or written by a newer
schema. A file that merely could not be read this time (locked by a scanner or a
backup agent, disk full, I/O error) is left exactly as it is, and the set-aside
copies are reclaimed after a retention window.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sqlite3
import time

import pytest

from cortex_backend.execution import repository as repository_module
from cortex_backend.execution.repository import (
    ASIDE_COPY_RETENTION_SECONDS,
    SCHEMA_VERSION,
    ExecutionRepository,
    ExecutionRepositoryError,
    ExecutionStoreUnavailable,
)


def _tear_a_page(db_path: Path) -> None:
    """The shape an unclean shutdown leaves: a run of zeroed bytes."""
    raw = bytearray(db_path.read_bytes())
    for index in range(100, min(4096, len(raw))):
        raw[index] = 0
    db_path.write_bytes(bytes(raw))


def test_a_damaged_store_is_rebuilt_instead_of_refusing_to_open(tmp_path: Path) -> None:
    db_path = tmp_path / "execution.sqlite"
    first = ExecutionRepository(db_path, tmp_path / "artifacts")
    owner = first.installation_principal_id
    first.create_job(
        job_id="job-1", owner=owner, request_id="r1", profile="scratch.auto.v1", payload={}
    )
    _tear_a_page(db_path)

    rebuilt = ExecutionRepository(db_path, tmp_path / "artifacts")

    # Usable again, and the transient job state is simply gone.
    assert rebuilt.get_job("job-1") is None
    job, created = rebuilt.create_job(
        job_id="job-2", owner=owner, request_id="r2", profile="scratch.auto.v1", payload={}
    )
    assert created and job.job_id == "job-2"


def test_the_damaged_file_is_kept_for_inspection(tmp_path: Path) -> None:
    """Rebuilding is not the same as deleting someone's data without a trace."""
    db_path = tmp_path / "execution.sqlite"
    ExecutionRepository(db_path, tmp_path / "artifacts")
    _tear_a_page(db_path)

    ExecutionRepository(db_path, tmp_path / "artifacts")

    preserved = list(tmp_path.glob("execution.sqlite.damaged-*"))
    assert len(preserved) == 1


def test_a_healthy_store_is_left_completely_alone(tmp_path: Path) -> None:
    """The check must never discard a database that was fine."""
    db_path = tmp_path / "execution.sqlite"
    first = ExecutionRepository(db_path, tmp_path / "artifacts")
    owner = first.installation_principal_id
    first.create_job(
        job_id="job-1", owner=owner, request_id="r1", profile="scratch.auto.v1", payload={}
    )

    reopened = ExecutionRepository(db_path, tmp_path / "artifacts")

    assert reopened.get_job("job-1") is not None
    assert list(tmp_path.glob("execution.sqlite.damaged-*")) == []


def test_the_replaced_stores_write_ahead_log_is_discarded(tmp_path: Path) -> None:
    """A leftover -wal describes the file moved aside, not the new one.

    Replaying it onto the empty replacement is the same corruption the chat
    store had to be fixed for.
    """
    db_path = tmp_path / "execution.sqlite"
    ExecutionRepository(db_path, tmp_path / "artifacts")
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute(
            "INSERT INTO execution_jobs (job_id, owner, request_id, profile,"
            " status, sequence, payload_json, created_at, updated_at)"
            " VALUES ('ghost','o','r','p','queued',0,'{}','2026-01-01','2026-01-01')"
        )
        raw.commit()
    finally:
        raw.close()
    _tear_a_page(db_path)

    ExecutionRepository(db_path, tmp_path / "artifacts")

    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


def _seed_store(tmp_path: Path) -> Path:
    """A healthy store holding one job, closed again."""
    db_path = tmp_path / "execution.sqlite"
    first = ExecutionRepository(db_path, tmp_path / "artifacts")
    first.create_job(
        job_id="job-1",
        owner=first.installation_principal_id,
        request_id="r1",
        profile="scratch.auto.v1",
        payload={},
    )
    return db_path


def _set_aside(tmp_path: Path, kind: str) -> list[Path]:
    return sorted(tmp_path.glob(f"execution.sqlite.{kind}-*"))


def _first_probe_fails(monkeypatch: pytest.MonkeyPatch, error: sqlite3.Error) -> None:
    """Make the startup integrity probe -- and only it -- raise ``error``."""
    real_connect = sqlite3.connect
    calls: list[int] = []

    class _FailingProbe(sqlite3.Connection):
        def execute(self, sql, *args):  # type: ignore[no-untyped-def]
            if "integrity_check" in sql:
                raise error
            return super().execute(sql, *args)

    def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) == 1:
            kwargs["factory"] = _FailingProbe
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)


def _coded(exc_type: type[sqlite3.Error], message: str, code: int) -> sqlite3.Error:
    error = exc_type(message)
    error.sqlite_errorcode = code  # type: ignore[attr-defined]
    return error


# SQLITE_BUSY, LOCKED, IOERR, CANTOPEN, FULL, READONLY, PERM, plus extended
# codes whose primary code is one of them.
_TRANSIENT_CODES = [5, 6, 10, 14, 13, 8, 3, 261, 522, 1038]


@pytest.mark.parametrize("code", _TRANSIENT_CODES)
def test_a_store_that_cannot_be_read_right_now_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Locked, busy, cannot-open and I/O errors are not evidence of damage.

    Every ``sqlite3.Error`` used to count as damage, so an antivirus scan or a
    backup agent holding the file turned a healthy store into an empty one and
    lost every in-flight job, approval and artifact row.
    """
    db_path = _seed_store(tmp_path)
    size_before = db_path.stat().st_size
    _first_probe_fails(monkeypatch, _coded(sqlite3.OperationalError, "synthetic", code))

    with pytest.raises(ExecutionStoreUnavailable):
        ExecutionRepository(db_path, tmp_path / "artifacts")

    monkeypatch.undo()
    assert db_path.stat().st_size == size_before
    assert _set_aside(tmp_path, "damaged") == []
    assert ExecutionRepository(db_path, tmp_path / "artifacts").get_job("job-1") is not None


@pytest.mark.parametrize("code", [11, 26, 267])
def test_a_store_that_reports_corruption_is_still_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    db_path = _seed_store(tmp_path)
    _first_probe_fails(monkeypatch, _coded(sqlite3.DatabaseError, "synthetic", code))

    rebuilt = ExecutionRepository(db_path, tmp_path / "artifacts")

    monkeypatch.undo()
    assert rebuilt.get_job("job-1") is None
    assert len(_set_aside(tmp_path, "damaged")) == 1


def test_error_messages_decide_when_the_driver_reports_no_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python 3.10 has no sqlite_errorcode; the message is all there is."""
    db_path = _seed_store(tmp_path)
    _first_probe_fails(monkeypatch, sqlite3.OperationalError("database is locked"))
    with pytest.raises(ExecutionStoreUnavailable):
        ExecutionRepository(db_path, tmp_path / "artifacts")
    monkeypatch.undo()
    assert _set_aside(tmp_path, "damaged") == []

    _first_probe_fails(monkeypatch, sqlite3.DatabaseError("file is not a database"))
    ExecutionRepository(db_path, tmp_path / "artifacts")
    monkeypatch.undo()
    assert len(_set_aside(tmp_path, "damaged")) == 1


def test_an_integrity_check_that_reports_a_problem_is_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other way corruption shows up: no exception, a result that is not ok."""
    db_path = _seed_store(tmp_path)
    real_connect = sqlite3.connect
    calls: list[int] = []

    class _ReportsAProblem(sqlite3.Connection):
        def execute(self, sql, *args):  # type: ignore[no-untyped-def]
            if "integrity_check" in sql:
                return super().execute("SELECT 'row 3 missing from index synthetic'")
            return super().execute(sql, *args)

    def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) == 1:
            kwargs["factory"] = _ReportsAProblem
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    rebuilt = ExecutionRepository(db_path, tmp_path / "artifacts")

    monkeypatch.undo()
    assert rebuilt.get_job("job-1") is None
    assert len(_set_aside(tmp_path, "damaged")) == 1


def test_a_store_really_locked_by_another_connection_is_not_renamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end case behind the mocked ones: a genuine exclusive lock."""
    db_path = _seed_store(tmp_path)
    blocker = sqlite3.connect(db_path, isolation_level=None)
    try:
        # A rollback-journal writer blocks readers; WAL would not.
        blocker.execute("PRAGMA journal_mode=DELETE")
        blocker.execute("BEGIN EXCLUSIVE")
        with monkeypatch.context() as short:
            short.setattr(repository_module, "_CONNECT_TIMEOUT_SECONDS", 0.05)
            with pytest.raises(ExecutionStoreUnavailable):
                ExecutionRepository(db_path, tmp_path / "artifacts")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert db_path.exists()
    assert _set_aside(tmp_path, "damaged") == []
    assert ExecutionRepository(db_path, tmp_path / "artifacts").get_job("job-1") is not None


def test_a_file_that_is_not_a_database_is_rebuilt(tmp_path: Path) -> None:
    db_path = tmp_path / "execution.sqlite"
    db_path.write_bytes(b"synthetic bytes that are not a sqlite database" * 20)

    rebuilt = ExecutionRepository(db_path, tmp_path / "artifacts")

    assert rebuilt.get_job("anything") is None
    assert len(_set_aside(tmp_path, "damaged")) == 1


def test_a_damaged_store_that_cannot_be_moved_aside_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _seed_store(tmp_path)
    _tear_a_page(db_path)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("synthetic sharing violation")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(ExecutionRepositoryError, match="could not be replaced"):
        ExecutionRepository(db_path, tmp_path / "artifacts")

    monkeypatch.undo()
    assert db_path.exists()
    assert _set_aside(tmp_path, "damaged") == []


def _age(path: Path, days: float) -> None:
    moment = time.time() - days * 86_400
    os.utime(path, (moment, moment))


def test_startup_sweep_reclaims_only_old_set_aside_copies(tmp_path: Path) -> None:
    db_path = _seed_store(tmp_path)
    keep_days = ASIDE_COPY_RETENTION_SECONDS / 86_400
    old_damaged = tmp_path / f"execution.sqlite.damaged-{'a' * 32}"
    old_newer = tmp_path / f"execution.sqlite.newer-{'b' * 32}"
    recent = tmp_path / f"execution.sqlite.damaged-{'c' * 32}"
    wrong_shape = tmp_path / "execution.sqlite.damaged-keep"
    other_store = tmp_path / f"other.sqlite.damaged-{'d' * 32}"
    for path in (old_damaged, old_newer, recent, wrong_shape, other_store):
        path.write_bytes(b"synthetic")
    _age(old_damaged, keep_days + 1)
    _age(old_newer, keep_days + 1)
    _age(recent, keep_days - 1)
    _age(wrong_shape, 400)
    _age(other_store, 400)

    ExecutionRepository(db_path, tmp_path / "artifacts")

    assert not old_damaged.exists()
    assert not old_newer.exists()
    assert recent.exists()
    assert wrong_shape.exists()
    assert other_store.exists()
    assert db_path.exists()


def test_a_store_set_aside_now_is_not_swept_for_its_old_last_write(tmp_path: Path) -> None:
    """The retention window runs from the set-aside, not from the last write."""
    db_path = _seed_store(tmp_path)
    _tear_a_page(db_path)
    _age(db_path, ASIDE_COPY_RETENTION_SECONDS / 86_400 + 30)

    ExecutionRepository(db_path, tmp_path / "artifacts")

    assert len(_set_aside(tmp_path, "damaged")) == 1


def _write_newer_store(db_path: Path) -> None:
    """A store as a newer build leaves it: a schema version and a table we never heard of."""
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(
            f"""
            CREATE TABLE execution_schema (
                id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL
            );
            INSERT INTO execution_schema (id, version) VALUES (1, {SCHEMA_VERSION + 1});
            CREATE TABLE future_only_table (id INTEGER PRIMARY KEY, note TEXT);
            INSERT INTO future_only_table (id, note) VALUES (1, 'synthetic');
            """
        )
        raw.commit()
    finally:
        raw.close()


def _tables_and_version(db_path: Path) -> tuple[set[str], int]:
    raw = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        version = raw.execute("SELECT version FROM execution_schema WHERE id = 1").fetchone()[0]
    finally:
        raw.close()
    return tables, int(version)


def test_a_newer_store_is_set_aside_untouched_instead_of_bricking_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Downgrading a build, or copying a profile back from a newer one.

    The version used to be read only after this build's DDL had run, and the
    refusal propagated out of the constructor -- so Cortex could not start at
    all, over a store that holds only disposable bookkeeping, and the newer
    database had already been written to.
    """
    db_path = tmp_path / "execution.sqlite"
    _write_newer_store(db_path)

    with caplog.at_level(logging.WARNING, logger="cortex.execution.repository"):
        repository = ExecutionRepository(db_path, tmp_path / "artifacts")

    (aside,) = _set_aside(tmp_path, "newer")
    # The newer file is exactly as the newer build left it: no DDL ran on it.
    assert _tables_and_version(aside) == ({"execution_schema", "future_only_table"}, SCHEMA_VERSION + 1)
    # The fresh store is at this build's version and fully usable.
    tables, version = _tables_and_version(db_path)
    assert version == SCHEMA_VERSION
    assert "future_only_table" not in tables
    job, created = repository.create_job(
        job_id="job-new", owner=repository.installation_principal_id, request_id="r", profile="scratch.auto.v1", payload={}
    )
    assert created and job.job_id == "job-new"
    assert "newer version" in caplog.text
    assert str(tmp_path) not in caplog.text


def test_a_newer_store_that_cannot_be_set_aside_is_refused_with_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "execution.sqlite"
    _write_newer_store(db_path)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("synthetic sharing violation")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(ExecutionRepositoryError, match="newer version of Cortex"):
        ExecutionRepository(db_path, tmp_path / "artifacts")

    monkeypatch.undo()
    # Refused before any DDL: the newer database is untouched.
    assert _tables_and_version(db_path) == ({"execution_schema", "future_only_table"}, SCHEMA_VERSION + 1)
    assert _set_aside(tmp_path, "newer") == []


def test_a_store_at_this_builds_version_is_not_set_aside(tmp_path: Path) -> None:
    db_path = _seed_store(tmp_path)

    reopened = ExecutionRepository(db_path, tmp_path / "artifacts")

    assert reopened.get_job("job-1") is not None
    assert _set_aside(tmp_path, "newer") == []
    assert _set_aside(tmp_path, "damaged") == []
