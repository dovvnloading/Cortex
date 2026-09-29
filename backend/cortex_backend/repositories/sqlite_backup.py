"""Backup and recovery primitives shared by the chat and settings databases.

Both stores keep verified backups of a SQLite file that other connections may
be writing to, and both can replace a corrupt primary from one of those
backups. The parts of that which are easy to get subtly wrong live here once:

* ``snapshot_database`` copies a live database through SQLite's online backup
  API, so the copy includes commits that are still only in the ``-wal``
  sidecar.
* ``open_at_rest`` / ``quick_check_at_rest`` read a backup that nothing is
  writing without leaving ``-wal`` and ``-shm`` files beside it, and bound the
  time a check may take.
* ``move_sidecars`` / ``put_sidecars_back`` set a crashed database's write-ahead
  log aside instead of deleting it, and undo that when recovery is abandoned.
* ``find_interrupted_recovery`` / ``adopt_orphaned_sidecars`` finish what a
  recovery that died half-way left behind: a primary already moved aside with
  nothing put in its place, and write-ahead logs stranded under the name of an
  earlier attempt.
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
import re
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

    ``adopted_sidecars`` lists write-ahead logs (or shared-memory files) that
    an earlier, interrupted recovery had set aside under a different
    quarantine name. They were moved beside ``quarantined_path`` so that
    ``sqlite3 .recover`` finds them next to the file they belong to.
    """

    recovered_from: str
    quarantined_path: str
    at: str
    adopted_sidecars: tuple[str, ...] = ()


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


def open_at_rest(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open a backup or snapshot nobody is writing, touching nothing but its bytes.

    A plain read-only open of a file whose header says write-ahead logging still
    makes SQLite create ``<file>-wal`` and ``<file>-shm`` beside it, and a file
    that is later renamed or superseded leaves them behind next to whatever
    takes its name. ``immutable=1`` tells SQLite the file cannot change, so it
    creates and locks nothing. It also means the write-ahead log is ignored, so
    this is only for files that have none: never for a live database.
    """
    uri = f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, timeout=SNAPSHOT_WAIT_SECONDS, uri=True)


def quick_check_at_rest(path: str | os.PathLike[str], *, time_limit: float) -> bool:
    """Whether a backup passes ``PRAGMA quick_check``, giving up after ``time_limit`` seconds.

    A check that cannot finish in time, or a file that cannot be opened at all,
    is not a pass: the callers use this to decide whether a backup may push an
    older, verified generation out of its slot, and not knowing is a reason to
    keep the older one. The file is opened with :func:`open_at_rest`.
    """
    deadline = time.monotonic() + time_limit
    connection: sqlite3.Connection | None = None
    try:
        connection = open_at_rest(path)
        # SQLite calls this every few dozen internal steps, including from
        # inside the integrity check, and a non-zero return interrupts it.
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        result = connection.execute("PRAGMA quick_check").fetchone()
        return result is not None and str(result[0]).lower() == "ok"
    except (OSError, sqlite3.Error, ValueError):
        return False
    finally:
        if connection is not None:
            connection.close()


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


def _quarantine_pattern(database: Path) -> str:
    """The name recovery gives a corrupt primary: ``<database>.corrupt-<uuid4 hex>``."""
    return rf"{re.escape(database.name)}\.corrupt-[0-9a-f]{{32}}"


def _modified_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def find_interrupted_recovery(database: str | os.PathLike[str]) -> Path | None:
    """Return the quarantined primary of a recovery that never finished, if any.

    Recovery moves the corrupt primary aside and only then publishes the
    restored copy. A crash in between leaves no primary at all, and a launch
    that simply created an empty one would then refresh the backups from that
    empty database and rotate the good backup away. A missing primary next to a
    ``<database>.corrupt-<id>`` file is therefore treated as that unfinished
    recovery; the most recently modified such file is returned. ``None`` means
    the primary exists or nothing was ever quarantined.

    Quarantined files are kept for good, so an old one can sit beside a database
    that a person deliberately removed. The caller only acts on this when a
    valid backup exists to restore from.
    """
    path = Path(database)
    if path.exists():
        return None
    pattern = re.compile(_quarantine_pattern(path))
    try:
        quarantined = [
            entry for entry in path.parent.iterdir() if pattern.fullmatch(entry.name) and entry.is_file()
        ]
    except OSError:
        return None
    if not quarantined:
        return None
    return max(quarantined, key=lambda entry: (_modified_ns(entry), entry.name))


def find_orphaned_sidecars(database: str | os.PathLike[str]) -> list[Path]:
    """Preserved ``-wal``/``-shm`` files whose quarantined database is not there.

    ``move_sidecars`` renames the logs before the primary, so a crash between
    the two renames strands them under the first attempt's quarantine name while
    the next attempt quarantines the primary under a new one. Newest first.
    """
    path = Path(database)
    pattern = re.compile(rf"({_quarantine_pattern(path)})-(?:wal|shm)")
    try:
        entries = list(path.parent.iterdir())
    except OSError:
        return []
    orphans = []
    for entry in entries:
        match = pattern.fullmatch(entry.name)
        if match is not None and entry.is_file() and not (path.parent / match.group(1)).exists():
            orphans.append(entry)
    orphans.sort(key=lambda entry: (-_modified_ns(entry), entry.name))
    return orphans


def adopt_orphaned_sidecars(
    database: str | os.PathLike[str], quarantined: str | os.PathLike[str]
) -> tuple[str, ...]:
    """Move logs stranded by an interrupted recovery beside ``quarantined``.

    ``sqlite3 .recover`` looks for ``<file>-wal`` next to the file it is given,
    so a log left under an earlier attempt's name is as good as lost. This is
    tidying after a recovery that already succeeded, so it never fails one: a
    rename that cannot be done, or a name that is taken, leaves that file where
    it is (nothing is overwritten) and is logged. Returns where the adopted
    files now are.
    """
    adopted: list[str] = []
    left_behind = 0
    for orphan in find_orphaned_sidecars(database):
        target = Path(f"{quarantined}{orphan.name[-4:]}")
        try:
            if target.exists():
                raise FileExistsError("a preserved sidecar already exists at the destination")
            os.replace(orphan, target)
        except OSError:
            left_behind += 1
            continue
        adopted.append(str(target))
    if left_behind:
        logging.warning(
            "%d earlier database sidecar file(s) could not be moved beside the quarantined "
            "database and were left where they are.",
            left_behind,
        )
    return tuple(adopted)
