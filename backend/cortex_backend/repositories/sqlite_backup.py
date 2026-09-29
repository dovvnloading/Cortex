"""Backup primitives shared by the chat and settings databases.

Both stores keep verified backups of a SQLite file that other connections may
be writing to. The part of that which is easy to get subtly wrong lives here
once:

* ``snapshot_database`` copies a live database through SQLite's online backup
  API, so the copy includes commits that are still only in the ``-wal``
  sidecar.
* ``BackupStatus`` is what a store reports about the backup it took.
"""

from __future__ import annotations

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
