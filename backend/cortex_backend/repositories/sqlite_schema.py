"""Versioned schema evolution shared by the chat and settings databases.

A database file is brought to the shape this release expects in one fixed
order, and the order is the point:

1. The stored ``PRAGMA user_version`` is read first. A file written by a newer
   release is refused before anything has changed it -- not a journal-mode
   switch, not a ``CREATE INDEX`` -- so an older build never half-modifies a
   file it cannot read.
2. Write-ahead logging is switched on and the mode SQLite *reports back* is
   checked. SQLite falls back to the rollback journal without raising when the
   volume cannot provide shared memory, and the stores' ``synchronous = NORMAL``
   is only crash-safe under WAL, so carrying on would be a silent downgrade.
3. Each step of the ladder that the file has not seen runs in its own
   ``BEGIN IMMEDIATE`` transaction together with the ``user_version`` bump. SQLite
   DDL is transactional, so a step that fails leaves the tables and the version
   exactly as they were, and the next launch simply runs it again.

Steps must be idempotent: a database that predates versioning reports
``user_version = 0`` although it may already hold some of the shape.

Nothing here deletes or rewrites user rows.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import contextlib
import sqlite3

Migration = Callable[[sqlite3.Connection], None]


class SchemaTooNewError(Exception):
    """The database was written by a release newer than this one."""

    def __init__(self, version: int, supported: int) -> None:
        self.version = version
        self.supported = supported
        super().__init__(f"database schema version {version} is newer than {supported}")


class WriteAheadLogUnavailableError(Exception):
    """SQLite would not put the database into write-ahead-log mode."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        super().__init__(f"SQLite reported journal mode {mode!r} instead of 'wal'")


def require_wal(mode: object) -> None:
    """Raise unless ``mode`` is what ``PRAGMA journal_mode = WAL`` must report."""
    reported = str(mode).lower()
    if reported != "wal":
        raise WriteAheadLogUnavailableError(reported)


def open_for_upgrade(path: object, *, timeout: float = 10.0) -> sqlite3.Connection:
    """Open ``path`` in autocommit mode, so the upgrade owns every BEGIN and COMMIT."""
    return sqlite3.connect(str(path), timeout=timeout, isolation_level=None)


def stored_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def add_column_if_missing(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    """``ALTER TABLE ... ADD COLUMN`` that tolerates a file which already has it.

    ``table`` and ``column`` are the migration's own literals, never user input.
    """
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def prepare_database(
    connection: sqlite3.Connection,
    migrations: Mapping[int, Migration],
    *,
    target: int,
) -> None:
    """Gate on the stored version, enable WAL, then run every step up to ``target``.

    ``connection`` must be in autocommit mode (see :func:`open_for_upgrade`).
    Raises :class:`SchemaTooNewError` before touching a newer file and
    :class:`WriteAheadLogUnavailableError` before running any step; a step that
    raises is rolled back and its exception propagates.
    """
    if connection.isolation_level is not None:
        raise ValueError("prepare_database needs an autocommit connection")
    version = stored_version(connection)
    if version > target:
        raise SchemaTooNewError(version, target)
    # WAL cannot be switched inside a transaction, so it sits outside the steps.
    # It is persisted in the file, which makes this a no-op once it has taken.
    require_wal(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
    for step in range(version + 1, target + 1):
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have run this step while we waited for the
            # write lock; the version is only trustworthy inside the lock.
            current = stored_version(connection)
            if current > target:
                raise SchemaTooNewError(current, target)
            if current >= step:
                connection.execute("ROLLBACK")
                continue
            migrations[step](connection)
            connection.execute(f"PRAGMA user_version = {step}")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
            raise
