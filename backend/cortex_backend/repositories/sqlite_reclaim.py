"""Give a database file back the space that deleted rows left in it.

SQLite never shrinks a file by itself: deleting a chat turns its pages into free
pages that later writes reuse, and the file stays as large as it ever was. A
history that was once big and has been cleared keeps its disk space for good.

Two mechanisms return it, and this module uses whichever the file supports:

* A file created with ``auto_vacuum = INCREMENTAL`` keeps a map of its free
  pages, and ``PRAGMA incremental_vacuum`` hands them back a few at a time.
  Each step is its own small transaction, so stopping part-way (time ran out,
  the process ended) loses nothing and the next start carries on.
* A file created without it can only shrink by ``VACUUM``, which rewrites the
  whole database into a temporary file and swaps it in. It is atomic -- an
  interruption, a full disk or a crash leaves the original untouched -- but it
  needs room for a second copy and holds the write lock throughout. Running it
  once with ``auto_vacuum = INCREMENTAL`` set also converts the file, so every
  later start can use the cheap path.

Nothing here runs unless more than a quarter of the file is free pages, so a
healthy database is never touched. Callers own the safety around it: the chat
store calls this once at startup, after a verified backup of the same content
has been written, and before the application serves anything. Every outcome
that is not a success is reported, never raised: reclaiming space is a courtesy
and must not stop Cortex from starting.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Literal

# Act only when more than this fraction of the file is free pages, and at least
# MIN_FREE_PAGES of them, so a small file is not rewritten over a few pages.
FREE_PAGE_FRACTION = 0.25
MIN_FREE_PAGES = 64

# Wall-clock budget for one call, and for waiting on another connection's lock.
TIME_LIMIT_SECONDS = 20.0
BUSY_TIMEOUT_SECONDS = 5.0

# A VACUUM rewrites everything that is kept. Past this much live content it could
# outlast the time limit on every start and never finish, so it is left alone.
MAX_VACUUM_LIVE_BYTES = 1024 * 1024 * 1024
# Room to spare beyond the copies themselves (journal, WAL frames, filesystem slack).
_SPACE_MARGIN_BYTES = 16 * 1024 * 1024

# Pages handed back per incremental step: small enough to check the clock often.
_INCREMENTAL_STEP_PAGES = 512
# The progress handler runs every this many SQLite virtual-machine steps.
_PROGRESS_INTERVAL = 1000

_AUTO_VACUUM_NONE = 0
_AUTO_VACUUM_INCREMENTAL = 2

Outcome = Literal[
    "nothing_to_do",
    "trimmed",
    "rewritten",
    "too_large",
    "not_enough_space",
    "gave_up",
]


def reclaim_free_space(
    path: str | os.PathLike[str],
    *,
    time_limit: float = TIME_LIMIT_SECONDS,
    busy_timeout: float = BUSY_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Outcome:
    """Return a database's free pages to the file system if there are many.

    ``"nothing_to_do"`` is the answer for a file that is not mostly empty (and
    for one that is missing); ``"trimmed"`` and ``"rewritten"`` say the
    incremental vacuum or a full ``VACUUM`` ran to the end; the rest say why
    nothing was done or finished. In every case the database holds exactly the
    rows it held before, and it is safe to call again.
    """
    if not os.path.exists(path):
        return "nothing_to_do"
    deadline = clock() + time_limit
    connection: sqlite3.Connection | None = None
    try:
        # mode=rw: never create a database where there is none.
        connection = sqlite3.connect(
            f"{Path(path).resolve().as_uri()}?mode=rw",
            timeout=busy_timeout,
            isolation_level=None,
            uri=True,
        )
        # Non-zero from the handler interrupts whatever is running, VACUUM
        # included, and SQLite rolls it back.
        connection.set_progress_handler(lambda: int(clock() >= deadline), _PROGRESS_INTERVAL)
        page_size = _pragma(connection, "page_size")
        page_count = _pragma(connection, "page_count")
        free_pages = _pragma(connection, "freelist_count")
        if free_pages < MIN_FREE_PAGES or free_pages <= page_count * FREE_PAGE_FRACTION:
            return "nothing_to_do"
        mode = _pragma(connection, "auto_vacuum")
        if mode == _AUTO_VACUUM_INCREMENTAL:
            return _trim(connection, deadline, clock)
        if mode != _AUTO_VACUUM_NONE:
            return "nothing_to_do"  # full auto-vacuum keeps the file trimmed by itself
        live_bytes = (page_count - free_pages) * page_size
        if live_bytes > MAX_VACUUM_LIVE_BYTES:
            return "too_large"
        if not _room_for_a_second_copy(Path(path).resolve().parent, live_bytes):
            return "not_enough_space"
        return _rewrite(connection)
    except (sqlite3.Error, OSError) as exc:
        # The type only: the message of an OS or SQLite error can carry the path.
        logging.warning("Could not reclaim unused space in a database (%s).", type(exc).__name__)
        return "gave_up"
    finally:
        if connection is not None:
            connection.close()


def _pragma(connection: sqlite3.Connection, name: str) -> int:
    return int(connection.execute(f"PRAGMA {name}").fetchone()[0])


def _trim(connection: sqlite3.Connection, deadline: float, clock: Callable[[], float]) -> Outcome:
    """Hand free pages back in small steps until none are left or time is up."""
    remaining = _pragma(connection, "freelist_count")
    while remaining > 0:
        if clock() >= deadline:
            return "gave_up"
        # The pragma only frees a page for each row it is asked to produce.
        connection.execute(f"PRAGMA incremental_vacuum({_INCREMENTAL_STEP_PAGES})").fetchall()
        freed = remaining - _pragma(connection, "freelist_count")
        if freed <= 0:
            # A step that frees nothing would otherwise spin until the clock
            # ran out, at every start.
            return "gave_up"
        remaining -= freed
    _shrink_the_file(connection)
    return "trimmed"


def _rewrite(connection: sqlite3.Connection) -> Outcome:
    """Convert to incremental auto-vacuum and rewrite the file, all or nothing."""
    connection.execute(f"PRAGMA auto_vacuum = {_AUTO_VACUUM_INCREMENTAL}")
    connection.execute("VACUUM")
    _shrink_the_file(connection)
    return "rewritten"


def _shrink_the_file(connection: sqlite3.Connection) -> None:
    """Move the freed pages out of the write-ahead log so the main file gets shorter.

    Best effort, and outside the time limit: the pages are already free, and a
    checkpoint that cannot finish (a reader still needs older frames, or the
    log is busy) is finished by a later write anyway.
    """
    connection.set_progress_handler(None, 0)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    except sqlite3.Error as exc:
        logging.info("The database log could not be truncated yet (%s).", type(exc).__name__)


def _room_for_a_second_copy(directory: Path, live_bytes: int) -> bool:
    """Whether the drives involved can hold what a VACUUM writes.

    It builds the new database in the temporary directory and then writes it
    back through the write-ahead log beside the file, so both are checked, the
    second twice over in case they are one drive. Free space that cannot be
    read is not room.
    """
    try:
        beside_the_file = shutil.disk_usage(directory).free
        for_the_copy = shutil.disk_usage(tempfile.gettempdir()).free
    except OSError:
        return False
    return (
        beside_the_file >= 2 * live_bytes + _SPACE_MARGIN_BYTES
        and for_the_copy >= live_bytes + _SPACE_MARGIN_BYTES
    )
