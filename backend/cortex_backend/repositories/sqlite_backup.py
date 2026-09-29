"""Backup and recovery primitives shared by the chat and settings databases.

Both stores keep verified backups of a SQLite file that other connections may
be writing to, and both can replace a corrupt primary from one of those
backups. The parts of that which are easy to get subtly wrong live here once:

* ``snapshot_database`` copies a live database through SQLite's online backup
  API, so the copy includes commits that are still only in the ``-wal``
  sidecar.
* ``move_sidecars`` / ``put_sidecars_back`` set a crashed database's write-ahead
  log aside instead of deleting it, and undo that when recovery is abandoned.
* ``BackupStatus`` and ``RecoveryReport`` are what a store reports about the
  backup it took and the recovery it performed.

Nothing here deletes or overwrites a database or a write-ahead log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Literal, NamedTuple

# How long a snapshot waits for a source that another connection holds locked
# before giving up. It matches the busy timeout the stores use for ordinary
# queries; a backup is not worth stalling startup for longer than a write is.
SNAPSHOT_WAIT_SECONDS = 10.0

SIDECAR_SUFFIXES = ("-wal", "-shm")

# SQLite result codes (sqlite3.SQLITE_BUSY / SQLITE_LOCKED only exist from
# Python 3.11, and this project supports 3.10).
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6

BackupState = Literal["ok", "failed", "skipped"]


class BackupStatus(NamedTuple):
    """What became of the most recent attempt to refresh a verified backup.

    ``state`` is ``"ok"`` when a verified backup was written, ``"failed"``
    when it could not be (the primary database is still healthy and in use),
    and ``"skipped"`` when there was nothing to back up yet. ``detail`` is a
    short, path-free explanation for a failure.
    """

    state: BackupState
    detail: str | None = None


@dataclass(frozen=True)
class RecoveryReport:
    """A corrupt primary database was replaced from a verified backup.

    ``recovered_from`` is the backup that was restored and
    ``quarantined_path`` the file the corrupt primary was moved to; its
    write-ahead log, if there was one, sits beside it as
    ``<quarantined_path>-wal``. ``at`` is a UTC ISO timestamp.

    The restored state is the one the backup captured, which is the state at
    the previous launch: anything written since then is only in the
    quarantined files.
    """

    recovered_from: str
    quarantined_path: str
    at: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def failure_detail(message: str, cause: BaseException | None) -> str:
    """Describe a failed backup without repeating anything path-shaped.

    An OS or SQLite error message can carry the private data-directory path,
    so only the exception's type is kept alongside the store's own message.
    """
    return f"{message} ({type(cause).__name__})" if cause is not None else message


def snapshot_database(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    wait_seconds: float = SNAPSHOT_WAIT_SECONDS,
) -> None:
    """Write a consistent copy of a live SQLite database to ``destination``.

    This is SQLite's online backup API rather than a checkpoint followed by a
    file copy. ``wal_checkpoint(TRUNCATE)`` reports failure through its return
    value, not by raising: while any reader pins the log it returns ``busy``
    and leaves frames in the ``-wal``, so copying only the main file then
    yields an older, perfectly valid database that passes an integrity check.
    The backup API reads through the connection, so the copy always includes
    every committed frame.

    ``destination`` should be a fresh, empty file; the caller validates and
    publishes it. The source is opened without creating it, and a source that
    stays locked past ``wait_seconds`` raises ``sqlite3.OperationalError``
    instead of retrying forever (Python's ``backup()`` loops for as long as
    SQLite reports busy).
    """
    deadline = time.monotonic() + wait_seconds

    def give_up_if_still_locked(status: int, remaining: int, total: int) -> None:
        if status in (_SQLITE_BUSY, _SQLITE_LOCKED) and time.monotonic() >= deadline:
            raise sqlite3.OperationalError("database is locked")

    source_uri = f"{Path(source).resolve().as_uri()}?mode=rw"
    source_connection = sqlite3.connect(source_uri, timeout=wait_seconds, uri=True)
    try:
        target_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(
                target_connection, progress=give_up_if_still_locked, sleep=0.05
            )
        finally:
            target_connection.close()
    finally:
        source_connection.close()


def move_sidecars(
    database: str | os.PathLike[str], destination: str | os.PathLike[str]
) -> list[tuple[Path, Path]]:
    """Move ``<database>-wal`` and ``-shm`` beside ``destination``.

    A crashed database's write-ahead log may hold its newest committed rows,
    so it is renamed to ``<destination>-wal`` -- the name SQLite and
    ``.recover`` look for next to that file -- rather than deleted. Returns
    the ``(original, moved)`` pairs so the caller can undo the move. If any
    rename fails, the ones already done are undone and ``OSError`` is raised;
    a destination that already exists is never overwritten.
    """
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in SIDECAR_SUFFIXES:
            original = Path(f"{database}{suffix}")
            if not original.exists():
                continue
            target = Path(f"{destination}{suffix}")
            if target.exists():
                raise FileExistsError("a preserved sidecar already exists at the destination")
            os.replace(original, target)
            moved.append((original, target))
    except OSError:
        try:
            put_sidecars_back(moved)
        except OSError:
            logging.warning("Could not return a database sidecar after a failed quarantine.")
        raise
    return moved


def put_sidecars_back(moved: list[tuple[Path, Path]]) -> None:
    """Undo ``move_sidecars``. Tries every rename, then raises the first failure.

    A file that has since appeared at the original name is left alone and the
    preserved sidecar stays where it is: it could belong to a different
    database, and overwriting a write-ahead log is exactly what this avoids.
    """
    first_error: OSError | None = None
    for original, target in reversed(moved):
        try:
            if original.exists():
                raise FileExistsError("a sidecar already exists at the original name")
            os.replace(target, original)
        except OSError as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise first_error
