"""A damaged execution store must not take the whole application with it.

execution.sqlite holds only transient bookkeeping -- jobs, events, leases,
artifact rows -- and is written on every job, every event and every lease
renewal, so it is the store most exposed to an unclean shutdown. It is also
the first dependency build_app constructs, and unlike the chat and settings
stores it has no backup and no recovery path. A torn page therefore stopped
the app launching at all: no chat, no settings, nothing.

Nothing in it is authored by the user, so it is rebuilt rather than restored.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

from cortex_backend.execution.repository import ExecutionRepository


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
