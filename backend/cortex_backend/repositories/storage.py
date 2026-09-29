"""Durable chat and permanent-memory persistence.

Two stores live here, and both are the live ones the application writes to:

1.  DatabaseManager: chat threads, messages, and groups in SQLite.
2.  PermanentMemoryManager: the user's explicit memos in a JSON file.

They share this module because they share a layout -- both are rooted at
AppPaths and both carry verified backups. Reading legacy JSON chat files is a
migration path DatabaseManager offers, not what this module is.
"""

import logging
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
import json
import re
import sqlite3
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, RLock
from uuid import uuid4

from cortex_backend.core.paths import AppPaths
from cortex_backend.repositories.sqlite_backup import (
    BackupStatus,
    RecoveryReport,
    adopt_orphaned_sidecars,
    failure_detail,
    find_interrupted_recovery,
    move_sidecars,
    open_at_rest,
    put_sidecars_back,
    quick_check_at_rest,
    snapshot_database,
    utc_now_iso,
)
from cortex_backend.repositories.sqlite_schema import (
    Migration,
    SchemaTooNewError,
    WriteAheadLogUnavailableError,
    add_column_if_missing,
    open_for_upgrade,
    prepare_database,
    stored_version,
)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO string that says it is UTC.

    Timestamps used to be written naive (no offset). ECMAScript parses a
    zone-less date-time as *local* time, so the WebView rendered every
    message footer shifted by the viewer's UTC offset -- and because the
    optimistic message the UI creates does carry a "Z", the time visibly
    jumped once the chat reloaded.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _as_utc_iso(value: object) -> object:
    """Tag a stored timestamp as UTC when it does not say so already.

    Rows written by earlier builds are naive. Normalising on read fixes those
    without a migration: a naive string is a strict prefix of the same instant
    with "+00:00" appended, so ORDER BY keeps old and new rows in the right
    order either way.
    """
    if not isinstance(value, str) or not value:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        return value
    return parsed.replace(tzinfo=timezone.utc).isoformat()


class PersistenceError(RuntimeError):
    """Raised when local chat or permanent-memory persistence fails."""

    def __init__(self, message: str, *, operation: str | None = None, cause=None):
        self.operation = operation
        self.cause = cause
        super().__init__(message)


# One lock per resolved chat-database path, shared by every DatabaseManager
# instance that opens it. Backup rotation and corrupt-primary recovery are
# file-copy operations, not SQLite transactions, so SQLite's own locking
# cannot serialize them against a concurrent instance in this process.
_CHAT_DB_LOCKS_GUARD = Lock()
_CHAT_DB_LOCKS: dict[str, RLock] = {}


def _chat_db_lock_for(path: str) -> RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _CHAT_DB_LOCKS_GUARD:
        return _CHAT_DB_LOCKS.setdefault(key, RLock())


@dataclass(frozen=True)
class MigrationResult:
    """Counts from one legacy JSON migration pass."""

    migrated: int = 0
    skipped: int = 0
    quarantined: int = 0


# Keep legacy imports within the limits enforced by the current chat API. The
# file cap also bounds the amount of JSON Python can materialize before the
# per-message checks below run.
MAX_LEGACY_CHAT_FILE_BYTES = 10 * 1024 * 1024
MAX_LEGACY_CHAT_MESSAGES = 16_384
MAX_LEGACY_MESSAGE_CONTENT_CHARS = 100_000
MAX_LEGACY_CHAT_ATTACHMENTS = 8
MAX_LEGACY_ATTACHMENT_BYTES = 10 * 1024 * 1024
_LEGACY_ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_LEGACY_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# How long the backup about to be rotated into the older slot may take to prove
# it is still whole. A check that overruns counts as a failure, which keeps the
# older generation, so this only bounds startup; it does not decide anything a
# healthy backup would not pass in a few seconds.
_OUTGOING_BACKUP_CHECK_SECONDS = 30.0

def _discard_sidecars_for(database_path: str | Path) -> None:
    """Remove the -wal/-shm SQLite leaves beside a database file.

    Validating a copy means opening it, which creates them. Whoever moves or
    deletes the file has to take them too, because their names are derived
    from a path that will not exist afterwards.
    """
    for suffix in ("-wal", "-shm"):
        try:
            Path(f"{database_path}{suffix}").unlink(missing_ok=True)
        except OSError:
            # Best effort: a locked sidecar is stale clutter, never a reason
            # to fail a backup that has already been written correctly.
            logging.warning("Could not remove a temporary database sidecar.")


# -- Schema ladder --------------------------------------------------------------
#
# Step n brings a chat database from version n-1 to n, and DatabaseManager runs
# each missing step in its own transaction together with the user_version bump
# (see sqlite_schema.prepare_database). Steps must be idempotent: a file written
# before versioning existed reports version 0 although it may already hold part
# of the shape. Add a step by appending the next number here and raising
# DatabaseManager.SCHEMA_VERSION; never edit a step that has shipped.


def _migrate_to_v1(connection: sqlite3.Connection) -> None:
    """Base tables: threads, messages, and their lookup indexes."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            timestamp TEXT NOT NULL
        );
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            sources TEXT,
            thoughts TEXT,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
        );
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_thread_timestamp "
        "ON messages(thread_id, timestamp, id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_threads_timestamp ON threads(timestamp)"
    )


def _migrate_to_v2(connection: sqlite3.Connection) -> None:
    """Message attachments."""
    add_column_if_missing(connection, "messages", "attachments", "TEXT")


def _migrate_to_v3(connection: sqlite3.Connection) -> None:
    """Per-message generation statistics."""
    add_column_if_missing(connection, "messages", "generation_stats_json", "TEXT")


def _migrate_to_v4(connection: sqlite3.Connection) -> None:
    """Groups (folders/projects) and the column that files a chat under one.

    ``position`` gives the user an explicit order independent of recency, and
    ``collapsed`` lives here rather than in browser storage so the sidebar looks
    the same on every launch and on any window.
    """
    connection.execute("""
        CREATE TABLE IF NOT EXISTS chat_groups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            collapsed INTEGER NOT NULL DEFAULT 0,
            timestamp TEXT NOT NULL
        );
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_groups_position "
        "ON chat_groups(position, timestamp)"
    )
    # Deliberately no FOREIGN KEY: SQLite cannot add a constrained column via
    # ALTER TABLE, and existing databases must upgrade in place rather than be
    # rebuilt. delete_group() clears the column explicitly (the same effect as
    # ON DELETE SET NULL), and the orphan sweep in DatabaseManager._create_tables
    # repairs any row that somehow outlives its group.
    add_column_if_missing(connection, "threads", "group_id", "TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_threads_group ON threads(group_id)")


_MIGRATIONS: dict[int, Migration] = {
    1: _migrate_to_v1,
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
}


def _content_digest(path: str, *, at_rest: bool = False) -> str | None:
    """A hash of every row in every table, or None if the file cannot be read that way.

    Two databases of the same schema version hold the same chats exactly when
    their digests match, regardless of how the bytes are laid out on disk. It
    reads the whole file, so it is only for the rare check that decides whether
    an existing pre-upgrade snapshot still describes the database.

    ``at_rest`` is for a snapshot that nothing is writing: it is read through
    ``open_at_rest``, which leaves no ``-wal`` or ``-shm`` beside it. The live
    database must not be read that way, because its write-ahead log is part of
    what it holds.
    """
    connection: sqlite3.Connection | None = None
    try:
        if at_rest:
            connection = open_at_rest(path)
        else:
            connection = sqlite3.connect(
                f"{Path(path).resolve().as_uri()}?mode=ro", timeout=10.0, uri=True
            )
        digest = hashlib.sha256()
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        for table in tables:
            digest.update(f"table:{table}\n".encode())
            for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                digest.update(repr(row).encode("utf-8", "surrogatepass"))
        return digest.hexdigest()
    except (OSError, sqlite3.Error, ValueError):
        return None
    finally:
        if connection is not None:
            connection.close()


class DatabaseManager:
    """Manages the persistence of chat conversations to a local SQLite database."""
    SCHEMA_VERSION = 4

    def __init__(
        self,
        db_path: str | None = None,
        legacy_history_dir: str | None = None,
        app_paths: AppPaths | None = None,
    ):
        """Initialize the manager without retaining a cross-thread SQLite connection."""
        if db_path is None or legacy_history_dir is None:
            resolved_paths = app_paths or AppPaths.for_current_user()
            db_path = db_path or str(resolved_paths.database)
            legacy_history_dir = legacy_history_dir or str(
                resolved_paths.legacy_chat_history
            )
        self.db_path = db_path
        self.legacy_history_dir = legacy_history_dir
        self.backup_path = f"{self.db_path}.bak"
        # Keep one older verified snapshot so an interrupted backup rotation
        # cannot discard the only recovery copy (mirrors sqlite_settings.py).
        self.previous_backup_path = f"{self.backup_path}.1"
        self.last_corrupt_path: str | None = None
        # What startup did about backups and recovery, for the diagnostics
        # route. A backup that could not be written is reported here; it does
        # not stop Cortex from starting on a healthy primary.
        self.backup_status = BackupStatus("ok")
        self.recovery_report: RecoveryReport | None = None
        self.pre_upgrade_snapshot_path: str | None = None
        # synchronous = NORMAL is only crash-safe under write-ahead logging, so
        # connect() applies it once _create_tables has read WAL back as enabled
        # and not before (SQLite's own default, FULL, is safe in every mode).
        self._wal_confirmed = False
        # Set when recovery had to fall back to the older backup generation
        # because .bak failed its check; see _create_backup.
        self._newest_backup_unusable = False
        self._write_lock = _chat_db_lock_for(self.db_path)
        # Paths and chat metadata are private local data.  Keep startup
        # diagnostics useful without copying them into process logs.
        logging.info("Database storage configured (private path omitted).")
        self._ensure_parent_directory()
        with self._write_lock:
            primary_verified = self._prepare_primary()
            self._snapshot_before_upgrade()
            self._create_tables()
            self._refresh_startup_backup(primary_verified=primary_verified)

    def _ensure_parent_directory(self):
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    @contextmanager
    def connect(self):
        """Yield a short-lived, thread-owned SQLite connection."""
        connection = None
        try:
            connection = sqlite3.connect(self.db_path, timeout=10.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            if self._wal_confirmed:
                connection.execute("PRAGMA synchronous = NORMAL")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise PersistenceError(
                "SQLite operation failed.",
                operation="sqlite",
                cause=exc,
            ) from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _close_connection(self):
        """Retained for compatibility; operation connections close automatically."""
        return None

    @staticmethod
    def _database_is_valid(path: str, *, quick: bool = False) -> bool:
        """Return whether an existing SQLite file can be opened and checked.

        The default is the full ``integrity_check``, which also verifies every
        index against its table; the primary and any backup a recovery is about
        to restore get that. ``quick=True`` runs ``quick_check`` (same page and
        b-tree checks, no index cross-check) for a copy that was just written
        page-for-page from a source that already passed the full one.
        """
        if not os.path.exists(path):
            return False
        connection: sqlite3.Connection | None = None
        try:
            # Read-only mode: validation must not create or mutate a file
            # before deciding whether it is safe to back up or recover from.
            uri = Path(path).resolve().as_uri()
            connection = sqlite3.connect(f"{uri}?mode=ro", timeout=10.0, uri=True)
            check = "quick_check" if quick else "integrity_check"
            result = connection.execute(f"PRAGMA {check}").fetchone()
            return result is not None and str(result[0]).lower() == "ok"
        except (OSError, sqlite3.Error, ValueError):
            return False
        finally:
            if connection is not None:
                connection.close()

    @classmethod
    def _atomic_copy_database(cls, source: str, destination: str) -> None:
        """Copy a verified SQLite file without exposing a partial destination."""
        cls._publish_verified_copy(destination, lambda temporary: shutil.copy2(source, temporary))

    @classmethod
    def _atomic_snapshot_database(
        cls, source: str, destination: str, *, displace_existing_to: str | None = None
    ) -> None:
        """Snapshot a live database, including uncheckpointed commits, into ``destination``.

        Same publish rules as _atomic_copy_database, but the bytes come from
        SQLite's online backup API instead of a file copy, so a reader that
        pins the write-ahead log cannot make the backup silently stale.
        """
        cls._publish_verified_copy(
            destination,
            lambda temporary: snapshot_database(source, temporary),
            displace_existing_to=displace_existing_to,
        )

    @classmethod
    def _publish_verified_copy(
        cls,
        destination: str,
        populate: Callable[[str], object],
        *,
        displace_existing_to: str | None = None,
    ) -> None:
        """Fill a temporary file, verify it, and only then move it into place.

        ``destination`` is replaced atomically or not at all, so a failure at
        any step (a full disk, a locked file, a failed integrity check)
        leaves whatever was there before exactly as it was.

        With ``displace_existing_to``, a file already at ``destination`` is
        renamed there first instead of being overwritten -- a rename, not a
        copy, and only after the new file has been written and verified, so a
        snapshot that fails leaves both files where they were. If the new file
        then cannot take its place, the old one is put back.

        Renaming onto a file that already exists replaces it for good, so when
        ``displace_existing_to`` is taken the file there is given a second name
        (a hard link) for the length of the swap. That name is dropped once the
        swap has succeeded and used to put the older file back if it has not, so
        both generations end up where they were. A crash in between leaves the
        second name beside the others rather than losing the file. Where the
        file system cannot make a hard link the swap goes ahead without that
        protection.
        """
        temporary_path: str | None = None
        try:
            fd, temporary_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(destination)}.",
                suffix=".tmp",
                dir=os.path.dirname(destination) or ".",
            )
            os.close(fd)
            populate(temporary_path)
            # A copy of something already checked in full: the cheaper
            # quick_check is enough to prove the copy itself came out whole.
            if not cls._database_is_valid(temporary_path, quick=True):
                raise OSError("database copy failed integrity validation")
            displaced_to: str | None = None
            older_file_kept_as: str | None = None
            if displace_existing_to is not None and os.path.exists(destination):
                older_file_kept_as = cls._link_to_spare_name(displace_existing_to)
                try:
                    os.replace(destination, displace_existing_to)
                except OSError:
                    cls._drop_spare_name(older_file_kept_as)
                    raise
                displaced_to = displace_existing_to
            try:
                os.replace(temporary_path, destination)
            except OSError:
                if displaced_to is not None:
                    try:
                        os.replace(displaced_to, destination)
                    except OSError:
                        logging.warning("Could not return a displaced database file to its name.")
                    else:
                        # Only now is the name free for the older file again.
                        cls._restore_from_spare_name(older_file_kept_as, displaced_to)
                raise
            cls._drop_spare_name(older_file_kept_as)
            # _database_is_valid opened the copy, so SQLite created
            # "<temp>-wal" and "<temp>-shm" beside it. os.replace moves only
            # the file itself, leaving those two behind under a name nothing
            # will ever reference again. Every startup rotates the backup, so
            # without this the data directory grows by two dead files a launch.
            _discard_sidecars_for(temporary_path)
            temporary_path = None
        except (OSError, shutil.Error, sqlite3.Error) as exc:
            raise PersistenceError(
                "Could not copy the chat database safely.", operation="backup", cause=exc
            ) from exc
        finally:
            if temporary_path is not None:
                _discard_sidecars_for(temporary_path)
                try:
                    os.unlink(temporary_path)
                except OSError as exc:
                    raise PersistenceError(
                        "Could not remove a temporary chat database copy.",
                        operation="backup",
                        cause=exc,
                    ) from exc

    @staticmethod
    def _link_to_spare_name(path: str) -> str | None:
        """Give an existing file a second name beside it and return that name.

        ``os.replace`` onto ``path`` would otherwise destroy the file. Nothing
        is moved, so ``path`` is where it always was; returns None when there is
        no file, or when the file system cannot make a hard link.
        """
        if not os.path.exists(path):
            return None
        spare = os.path.join(
            os.path.dirname(path) or ".", f".{os.path.basename(path)}.{uuid4().hex}.old"
        )
        try:
            os.link(path, spare)
        except (OSError, NotImplementedError):
            logging.warning(
                "Could not protect the older database backup while rotating; carrying on without."
            )
            return None
        return spare

    @staticmethod
    def _drop_spare_name(spare: str | None) -> None:
        """Remove a name made by ``_link_to_spare_name``; the file keeps its other one."""
        if spare is None:
            return
        try:
            os.unlink(spare)
        except OSError:
            logging.warning("Could not remove a temporary database backup link.")

    @staticmethod
    def _restore_from_spare_name(spare: str | None, path: str) -> None:
        """Put a file back under ``path`` after its name was taken; a failure leaves it as ``spare``."""
        if spare is None:
            return
        try:
            os.replace(spare, path)
        except OSError:
            logging.warning("Could not return an older database backup to its name.")

    def _prepare_primary(self) -> bool:
        """Validate the primary before backup rotation, recovering if needed.

        Runs once at startup rather than on every write: WAL mode already
        gives the primary strong crash safety for the continuous case (see
        _create_tables), so this defends against the rarer catastrophic case
        -- a corrupt or unreadable primary -- using the same validated,
        two-generation backup rotation already proven in sqlite_settings.py.

        This stays fail-closed. A corrupt primary with no usable backup, or a
        recovery that cannot preserve what it is replacing, raises rather than
        starting on an empty or half-restored database.

        Returns whether the primary has now been checked in full (or was just
        restored from a backup that was), which lets the startup backup skip
        reading it a second time. A primary that does not exist yet returns
        False.
        """
        if not os.path.exists(self.db_path):
            return self._resume_interrupted_recovery()
        if self._database_is_valid(self.db_path):
            return True

        candidate = self._first_valid_backup()
        if candidate is None:
            raise PersistenceError(
                "Chat database is corrupt and no valid backup is available.",
                operation="recovery",
            )
        self._record_recovery(
            self._recover_from(candidate),
            "Chat database was corrupt; recovered from a verified backup.",
        )
        return True

    def _first_valid_backup(self) -> str | None:
        """The newest backup generation that passes a full integrity check."""
        for candidate in (self.backup_path, self.previous_backup_path):
            if self._database_is_valid(candidate):
                # If the newer generation was skipped because it failed, the
                # next rotation must not push it over this one.
                self._newest_backup_unusable = candidate == self.previous_backup_path
                return candidate
        return None

    def _record_recovery(self, report: RecoveryReport, message: str) -> None:
        self.recovery_report = report
        self.last_corrupt_path = report.quarantined_path
        logging.error(
            "%s The corrupt file and its write-ahead log were preserved for "
            "inspection (path omitted from logs).",
            message,
        )

    def _resume_interrupted_recovery(self) -> bool:
        """Finish a recovery that died after moving the primary aside.

        Recovery quarantines the corrupt primary and then publishes the
        restored copy. Dying in between leaves no primary, and carrying on
        would create an empty database, back that up over the good backup, and
        push the good one out of the second generation on the launch after.
        A missing primary beside a quarantined ``.corrupt-<id>`` file and a
        valid backup is that state, so the backup is restored now. With no
        valid backup there is nothing to restore from, and a new database is
        created as it always was.
        """
        quarantined = find_interrupted_recovery(self.db_path)
        if quarantined is None:
            return False
        candidate = self._first_valid_backup()
        if candidate is None:
            logging.warning(
                "The chat database is missing beside a quarantined corrupt copy and no valid "
                "backup exists; starting a new database."
            )
            return False
        self._record_recovery(
            self._finish_interrupted_recovery(candidate, str(quarantined)),
            "A previous recovery of the chat database was interrupted; finished it from a "
            "verified backup.",
        )
        return True

    def _finish_interrupted_recovery(self, candidate: str, quarantined: str) -> RecoveryReport:
        """Publish ``candidate`` as the primary; the corrupt one is already aside."""
        try:
            # A log with no database beside it is replayed onto whatever file is
            # created there next, so it goes to the quarantined file as usual.
            move_sidecars(self.db_path, quarantined)
        except OSError as exc:
            raise PersistenceError(
                "Could not preserve the write-ahead log of the chat database before finishing "
                "an interrupted recovery.",
                operation="recovery",
                cause=exc,
            ) from exc
        # Nothing to roll back if this fails: the primary was already aside, the
        # next launch finds the same state and tries again.
        self._atomic_copy_database(candidate, self.db_path)
        return RecoveryReport(
            recovered_from=candidate,
            quarantined_path=quarantined,
            at=utc_now_iso(),
            adopted_sidecars=adopt_orphaned_sidecars(self.db_path, quarantined),
        )

    def _recover_from(self, candidate: str) -> RecoveryReport:
        """Replace the corrupt primary with ``candidate``, keeping everything it had.

        The crash that corrupts the primary is the same event that leaves an
        uncheckpointed -wal beside it. That log must not sit next to the
        restored copy: SQLite cannot tell it describes a different database
        and would replay its frames onto the backup, overwriting recovered
        rows with content from the file just declared corrupt. But it may also
        hold the newest committed messages, so it is moved next to the
        quarantined primary (``<corrupt>-wal``) rather than deleted, where
        ``sqlite3 .recover`` can still find it.

        The log moves first. A crash between the two renames then leaves the
        primary still detectably corrupt, so the next start repeats recovery;
        the other order would leave a log with no database beside it, which
        SQLite replays onto whatever file is created there next.

        If the backup cannot be put in place, everything is moved back.
        """
        corrupt_path = f"{self.db_path}.corrupt-{uuid4().hex}"
        try:
            moved_sidecars = move_sidecars(self.db_path, corrupt_path)
        except OSError as exc:
            raise PersistenceError(
                "Could not preserve the write-ahead log of the corrupt chat database "
                "before recovery.",
                operation="recovery",
                cause=exc,
            ) from exc
        try:
            os.replace(self.db_path, corrupt_path)
        except OSError as exc:
            try:
                put_sidecars_back(moved_sidecars)
            except OSError:
                logging.warning("Could not return the chat database write-ahead log.")
            raise PersistenceError(
                "Could not preserve the corrupt chat database before recovery.",
                operation="recovery",
                cause=exc,
            ) from exc
        try:
            self._atomic_copy_database(candidate, self.db_path)
        except PersistenceError:
            try:
                os.replace(corrupt_path, self.db_path)
            except OSError as rollback_exc:
                raise PersistenceError(
                    "Chat database recovery failed and the corrupt primary could not be "
                    f"restored; it remains at {corrupt_path}.",
                    operation="recovery",
                    cause=rollback_exc,
                ) from rollback_exc
            # The original primary is back, so its own log is the right one
            # again. Restored second: with the primary in place, a log that
            # cannot be returned is still safe on disk, just not replayed.
            try:
                put_sidecars_back(moved_sidecars)
            except OSError as rollback_exc:
                raise PersistenceError(
                    "Chat database recovery failed; the corrupt primary was restored but "
                    f"its write-ahead log remains at {corrupt_path}-wal.",
                    operation="recovery",
                    cause=rollback_exc,
                ) from rollback_exc
            raise
        return RecoveryReport(
            recovered_from=candidate,
            quarantined_path=corrupt_path,
            at=utc_now_iso(),
            adopted_sidecars=adopt_orphaned_sidecars(self.db_path, corrupt_path),
        )

    def _create_backup(self, *, primary_verified: bool = False) -> None:
        """Refresh the validated backup from the current primary.

        Called once at startup (after _prepare_primary and schema init), not
        on every message write -- unlike settings, chat writes happen on
        every turn, and a full-file copy on each one would not scale.

        The primary is read through SQLite's online backup API, not copied
        as a file after a checkpoint: in WAL mode recent commits can live
        only in the -wal sidecar, and wal_checkpoint(TRUNCATE) does not raise
        when a reader keeps it from finishing.

        Startup does one full read of the primary to validate it
        (_prepare_primary), one snapshot of it, a quick_check of that
        snapshot, and a bounded quick_check of the backup it replaces.
        ``primary_verified`` says the first has already happened this launch.
        The old backup becomes the older generation by rename, after the new
        snapshot has been written and verified, not by a second byte copy: a
        snapshot that fails leaves both generations where they were.

        A backup that has failed its check is overwritten, not rotated over
        the good generation behind it. Recovery finding one bad is one way to
        know (``_newest_backup_unusable``); the other is the check just
        described, which catches a backup that quietly rotted between two
        launches. A check that cannot finish in time is treated the same way,
        so the older generation is kept whenever the newer one is not known
        to be good.
        """
        with self._write_lock:
            if not os.path.exists(self.db_path):
                return
            if not primary_verified and not self._database_is_valid(self.db_path):
                raise PersistenceError(
                    "Could not back up a chat database that failed validation.",
                    operation="backup",
                )
            older_generation_slot = None if self._newest_backup_unusable else self.previous_backup_path
            if (
                older_generation_slot is not None
                and os.path.exists(self.backup_path)
                and not quick_check_at_rest(
                    self.backup_path, time_limit=_OUTGOING_BACKUP_CHECK_SECONDS
                )
            ):
                logging.warning(
                    "The newest chat database backup did not pass its check; keeping the older "
                    "generation and replacing the newest one."
                )
                older_generation_slot = None
            try:
                self._atomic_snapshot_database(
                    self.db_path,
                    self.backup_path,
                    displace_existing_to=older_generation_slot,
                )
            except PersistenceError:
                raise
            except OSError as exc:
                raise PersistenceError(
                    "Could not create a chat database backup.", operation="backup", cause=exc
                ) from exc
            self._newest_backup_unusable = False

    def _refresh_startup_backup(self, *, primary_verified: bool = False) -> None:
        """Take the startup backup without letting its failure stop the launch.

        The backup is a safety copy, and at this point the primary has been
        validated and opened. A full disk, a scanner holding the file, or a
        read-only .bak would otherwise turn "no spare copy" into "cannot
        chat", when chatting needs kilobytes. The failure is logged and
        reported through ``backup_status``; the previous backups are left
        exactly as they were, because every write into them is atomic.
        """
        try:
            self._create_backup(primary_verified=primary_verified)
        except PersistenceError as exc:
            self._note_backup_failure(str(exc), exc.cause)

    def _note_backup_failure(self, message: str, cause: BaseException | None) -> None:
        # Never log the exception text: an OS error carries the private path.
        logging.error(
            "Chat database backup failed; continuing with the existing backups (%s).",
            type(cause).__name__ if cause is not None else "no cause recorded",
        )
        if self.backup_status.state != "failed":
            self.backup_status = BackupStatus("failed", failure_detail(message, cause))

    def _stored_schema_state(self) -> tuple[int, bool]:
        """Return ``(user_version, has_tables)`` for the existing primary."""
        with self.connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            has_tables = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
                ).fetchone()
                is not None
            )
        return version, has_tables

    def _pre_upgrade_snapshot_path(self, version: int) -> str:
        return f"{self.db_path}.pre-v{version}.bak"

    def _unsupported_schema_error(self, version: int) -> PersistenceError:
        """Refuse a database a newer release has upgraded, and say how to go back."""
        # Any snapshot at or below this release's version is readable by it;
        # the highest such version is the newest data that can go back.
        pattern = re.compile(rf"{re.escape(os.path.basename(self.db_path))}\.pre-v(\d+)\.bak")
        try:
            names = os.listdir(os.path.dirname(os.path.abspath(self.db_path)))
        except OSError:
            names = []
        readable = {
            int(match.group(1)): match.group(0)
            for match in map(pattern.fullmatch, names)
            if match is not None and int(match.group(1)) <= self.SCHEMA_VERSION
        }
        if readable:
            advice = (
                "To go back to this release, close Cortex, move the current database and any "
                "-wal and -shm files beside it to a folder of your own (whatever was written "
                "since the upgrade stays only in those files), then copy "
                f"{readable[max(readable)]} to the database's name. The newer release kept "
                "that snapshot before it upgraded the database."
            )
        else:
            advice = "Install the release that wrote it, or restore a backup taken before it was upgraded."
        return PersistenceError(
            f"Unsupported database schema version {version}; this release reads up to "
            f"{self.SCHEMA_VERSION}. {advice}",
            operation="schema_check",
        )

    def _superseded_snapshot_path(self, snapshot_path: str) -> str:
        """An unused name to keep an older pre-upgrade snapshot under."""
        number = 1
        while os.path.exists(f"{snapshot_path}.superseded-{number}"):
            number += 1
        return f"{snapshot_path}.superseded-{number}"

    def _snapshot_is_current(self, snapshot_path: str) -> bool:
        """Whether an existing pre-upgrade snapshot holds exactly what the primary holds now."""
        digest = _content_digest(snapshot_path, at_rest=True)
        return digest is not None and digest == _content_digest(self.db_path)

    def _snapshot_before_upgrade(self) -> None:
        """Keep the pre-upgrade state of a database this release is about to change.

        The ordinary backup is refreshed after the schema upgrade, so it and
        the generation behind it both hold the new schema, and an older
        release refuses all of them. A database on an older schema version is
        therefore snapshotted first, to ``<db>.pre-v<version>.bak``. That name
        always holds the newest snapshot for the version, and a snapshot is
        never overwritten: one already there that no longer matches the
        database (a rollback was followed by more chats on the older release,
        and now a second upgrade) is renamed to ``<name>.superseded-<n>`` first.
        One that still matches (an upgrade that was interrupted and is being
        retried) is kept as it is.

        A database from a newer release is refused here, before anything
        touches it, with the name of the snapshot to restore.

        The upgrade does not run without its snapshot. A snapshot that cannot
        be written -- a full disk, a file held by another program -- refuses
        the upgrade with the database untouched, and the next launch tries the
        snapshot again. Carrying on would complete the upgrade in this same
        launch, and nothing would ever retry: the rollback point would simply
        never exist.
        """
        if not os.path.exists(self.db_path):
            return
        version, has_tables = self._stored_schema_state()
        if version > self.SCHEMA_VERSION:
            raise self._unsupported_schema_error(version)
        # A pre-versioning file (user_version 0) that already has tables is
        # real history about to be altered; an empty or brand-new one is not.
        if version == self.SCHEMA_VERSION or not has_tables:
            return
        snapshot_path = self._pre_upgrade_snapshot_path(version)
        try:
            if os.path.exists(snapshot_path) and self._snapshot_is_current(snapshot_path):
                self.pre_upgrade_snapshot_path = snapshot_path
                return
            self._atomic_snapshot_database(
                self.db_path,
                snapshot_path,
                displace_existing_to=self._superseded_snapshot_path(snapshot_path),
            )
        except PersistenceError as exc:
            logging.error(
                "Could not keep a pre-upgrade copy of the chat database; not upgrading (%s).",
                type(exc.cause).__name__ if exc.cause is not None else "no cause recorded",
            )
            raise PersistenceError(
                "Could not keep a copy of the chat database before upgrading it, so it was not "
                "upgraded and nothing was changed. Free some disk space and close any program "
                "that has the database open, then start Cortex again.",
                operation="backup",
                cause=exc.cause,
            ) from exc
        self.pre_upgrade_snapshot_path = snapshot_path

    def _create_tables(self) -> None:
        """Bring the database to ``SCHEMA_VERSION``, then repair what a migration cannot.

        The order is in sqlite_schema.prepare_database: version gate, then
        write-ahead logging (read back, not assumed), then one transaction per
        missing step of ``_MIGRATIONS``. WAL plus ``synchronous = NORMAL`` is
        SQLite's documented safe-and-fast combination: an application or OS
        crash can lose at most the last transaction but cannot corrupt the
        file, which the rollback journal does not guarantee under NORMAL.
        """
        connection: sqlite3.Connection | None = None
        try:
            connection = open_for_upgrade(self.db_path)
            prepare_database(connection, _MIGRATIONS, target=self.SCHEMA_VERSION)
        except SchemaTooNewError as exc:
            raise self._unsupported_schema_error(exc.version) from exc
        except WriteAheadLogUnavailableError as exc:
            raise PersistenceError(
                "SQLite would not enable write-ahead logging for the chat database (it reported "
                f"journal mode '{exc.mode}'), which Cortex needs to store chats safely. Some "
                "network, cloud-synced and removable drives do not support it. Start Cortex with "
                "--data-dir pointing at a folder on a local drive.",
                operation="journal_mode",
                cause=exc,
            ) from exc
        except sqlite3.Error as exc:
            raise PersistenceError(
                "Could not upgrade the chat database schema; it was left at its previous version.",
                operation="schema_migration",
                cause=exc,
            ) from exc
        finally:
            if connection is not None:
                connection.close()
        # From here the file is in WAL mode, which is what makes NORMAL safe.
        self._wal_confirmed = True
        with self.connect() as conn:
            # Self-heal, deliberately not a migration step: without a real FK, a
            # chat could in principle point at a group that no longer exists (an
            # interrupted delete, an externally edited file). Such a chat would
            # be filed under a group the sidebar never renders, making it look
            # deleted. Return any orphan to the ungrouped list on startup.
            conn.execute(
                "UPDATE threads SET group_id = NULL WHERE group_id IS NOT NULL "
                "AND group_id NOT IN (SELECT id FROM chat_groups)"
            )
            logging.info("Database schema is at version %d.", stored_version(conn))

    @staticmethod
    def _parse_legacy_attachment(value: object) -> dict | None:
        """Keep only attachment metadata that the API response accepts."""
        if not isinstance(value, dict):
            return None
        required = {
            "attachment_id",
            "filename",
            "mime_type",
            "size",
            "sha256",
            "kind",
            "expires_at",
        }
        if set(value) != required:
            return None
        if (
            not isinstance(value["attachment_id"], str)
            or _LEGACY_ATTACHMENT_ID.fullmatch(value["attachment_id"]) is None
            or not isinstance(value["filename"], str)
            or not 1 <= len(value["filename"]) <= 180
            or not isinstance(value["mime_type"], str)
            or not 1 <= len(value["mime_type"]) <= 128
            or type(value["size"]) is not int
            or not 0 < value["size"] <= MAX_LEGACY_ATTACHMENT_BYTES
            or not isinstance(value["sha256"], str)
            or _LEGACY_SHA256.fullmatch(value["sha256"]) is None
            or not isinstance(value["kind"], str)
            or value["kind"] not in {"image", "document"}
            or not isinstance(value["expires_at"], str)
        ):
            return None
        try:
            datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
        except ValueError:
            return None
        return value

    @classmethod
    def _parse_legacy_chat(cls, chat_data: object) -> dict:
        if not isinstance(chat_data, dict):
            raise ValueError("chat file must contain a JSON object")
        thread_id = chat_data.get('id')
        messages = chat_data.get('messages', [])
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("chat file is missing a non-empty id")
        if not isinstance(messages, list):
            raise ValueError("chat messages must be a list")
        if len(messages) > MAX_LEGACY_CHAT_MESSAGES:
            raise ValueError("chat contains too many messages")
        normalized_messages = []
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("chat message must be an object")
            role = message.get('role')
            if not isinstance(role, str) or role not in {'user', 'assistant', 'system'}:
                raise ValueError("chat message has an unsupported role")
            content = message.get('content')
            if not isinstance(content, str) or not 1 <= len(content) <= MAX_LEGACY_MESSAGE_CONTENT_CHARS:
                raise ValueError("chat message text is outside the supported limit")

            sources = message.get('sources')
            if not isinstance(sources, list):
                sources = None
            thoughts = message.get('thoughts')
            if role != 'assistant' or not isinstance(thoughts, str) or len(thoughts) > MAX_LEGACY_MESSAGE_CONTENT_CHARS:
                thoughts = None
            attachments = message.get('attachments')
            if isinstance(attachments, list) and len(attachments) <= MAX_LEGACY_CHAT_ATTACHMENTS:
                parsed_attachments = [cls._parse_legacy_attachment(item) for item in attachments]
                attachments = parsed_attachments if all(item is not None for item in parsed_attachments) else None
            else:
                attachments = None
            normalized_messages.append({
                'role': role,
                'content': content,
                'sources': sources,
                'thoughts': thoughts,
                'attachments': attachments,
            })
        return {
            'id': thread_id,
            'title': str(chat_data.get('title') or 'Untitled Chat'),
            'timestamp': str(chat_data.get('timestamp') or _utc_now_iso()),
            'messages': normalized_messages,
        }

    @staticmethod
    def _load_legacy_chat_file(file_path: str) -> object:
        """Read a legacy file with a byte ceiling before parsing JSON."""
        with open(file_path, 'rb') as stream:
            payload = stream.read(MAX_LEGACY_CHAT_FILE_BYTES + 1)
        if len(payload) > MAX_LEGACY_CHAT_FILE_BYTES:
            raise ValueError("legacy chat file exceeds the supported size")
        return json.loads(payload.decode('utf-8'))

    def _quarantine_legacy_file(self, file_path: str) -> str:
        quarantine_dir = os.path.join(self.legacy_history_dir, 'quarantine')
        os.makedirs(quarantine_dir, exist_ok=True)
        destination = os.path.join(quarantine_dir, os.path.basename(file_path))
        if os.path.exists(destination):
            destination = os.path.join(
                quarantine_dir,
                f"{os.path.splitext(os.path.basename(file_path))[0]}_{int(datetime.now().timestamp())}.json",
            )
        return shutil.move(file_path, destination)

    @staticmethod
    def _archive_legacy_file(file_path: str, archive_dir: str) -> str:
        os.makedirs(archive_dir, exist_ok=True)
        return shutil.move(file_path, os.path.join(archive_dir, os.path.basename(file_path)))

    def migrate_from_json_if_needed(self) -> MigrationResult:
        """Migrate valid legacy files transactionally and isolate invalid files."""
        if not os.path.isdir(self.legacy_history_dir):
            return MigrationResult()

        logging.warning("Legacy JSON chat history found. Starting migration to SQLite...")
        migrated = skipped = quarantined = 0
        archive_dir = f"{self.legacy_history_dir}_migrated_{int(datetime.now().timestamp())}"

        for filename in sorted(os.listdir(self.legacy_history_dir)):
            if not filename.lower().endswith('.json'):
                continue
            file_path = os.path.join(self.legacy_history_dir, filename)
            if not os.path.isfile(file_path):
                continue

            try:
                chat_data = self._parse_legacy_chat(self._load_legacy_chat_file(file_path))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logging.error(
                    "Quarantining invalid legacy chat file failed (%s).",
                    type(exc).__name__,
                )
                try:
                    self._quarantine_legacy_file(file_path)
                    quarantined += 1
                except OSError as quarantine_error:
                    logging.error(
                        "Could not quarantine legacy chat file (%s).",
                        type(quarantine_error).__name__,
                    )
                continue

            try:
                with self.connect() as conn:
                    existing = conn.execute(
                        "SELECT 1 FROM threads WHERE id = ?",
                        (chat_data['id'],),
                    ).fetchone()
                    if existing:
                        skipped += 1
                    else:
                        conn.execute(
                            "INSERT INTO threads (id, title, timestamp) VALUES (?, ?, ?)",
                            (chat_data['id'], chat_data['title'], chat_data['timestamp']),
                        )
                        try:
                            base_timestamp = datetime.fromisoformat(
                                chat_data['timestamp'].replace('Z', '+00:00')
                            )
                        except ValueError:
                            # Aware, so migrated rows carry an offset like
                            # every other write path. A legacy file whose own
                            # timestamp is naive still parses above and is
                            # normalised on read.
                            base_timestamp = datetime.now(timezone.utc)
                        for index, message in enumerate(chat_data['messages']):
                            message_timestamp = (base_timestamp + timedelta(microseconds=index)).isoformat()
                            conn.execute(
                                """
                                INSERT INTO messages
                                    (thread_id, role, content, sources, thoughts, attachments, timestamp)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    chat_data['id'],
                                    message['role'],
                                    message['content'],
                                    json.dumps(message.get('sources')) if message.get('sources') else None,
                                    message.get('thoughts'),
                                    json.dumps(message.get('attachments')) if message.get('attachments') else None,
                                    message_timestamp,
                                ),
                            )
                        migrated += 1
            except PersistenceError:
                raise

            try:
                self._archive_legacy_file(file_path, archive_dir)
            except OSError as exc:
                logging.error(
                    "Migrated legacy chat but could not archive the source file (%s).",
                    type(exc).__name__,
                )

        result = MigrationResult(migrated=migrated, skipped=skipped, quarantined=quarantined)
        logging.info(
            "Legacy migration complete: %s migrated, %s skipped, %s quarantined.",
            result.migrated,
            result.skipped,
            result.quarantined,
        )
        return result

    def create_chat(self, thread_id: str, title: str):
        """Creates a new chat thread record in the database."""
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO threads (id, title, timestamp) VALUES (?, ?, ?)",
                    (thread_id, title, _utc_now_iso())
                )
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to create chat thread {thread_id}.",
                operation="create_chat",
                cause=exc,
            ) from exc

    def create_chat_from_messages(self, thread_id: str, title: str, messages: list[dict]):
        """Creates a new chat thread and bulk-inserts a list of messages."""
        try:
            with self.connect() as conn:
                # 1. Create the new thread entry
                conn.execute(
                    "INSERT INTO threads (id, title, timestamp) VALUES (?, ?, ?)",
                    (thread_id, title, _utc_now_iso())
                )
                
                # 2. Prepare and insert all messages for the new thread
                messages_to_insert = []
                forked_at = datetime.now(timezone.utc)
                for i, msg in enumerate(messages):
                    # Keep each message's own time. Stamping them all with
                    # "now" made a fork claim every historical turn was sent at
                    # the moment of forking. The offset fallback only orders
                    # messages that never had a timestamp; replace(microsecond=i)
                    # also raised once i reached 1_000_000.
                    msg_timestamp = _as_utc_iso(msg.get("timestamp")) or (
                        forked_at + timedelta(microseconds=i)
                    ).isoformat(timespec="microseconds")
                    messages_to_insert.append((
                        thread_id,
                        msg.get('role'),
                        msg.get('content'),
                        json.dumps(msg.get('sources')) if msg.get('sources') else None,
                        msg.get('thoughts'),
                        json.dumps(msg.get('attachments')) if msg.get('attachments') else None,
                        json.dumps(msg.get('stats')) if msg.get('stats') else None,
                        msg_timestamp
                    ))
                
                conn.executemany("""
                    INSERT INTO messages (thread_id, role, content, sources, thoughts, attachments, generation_stats_json, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, messages_to_insert)
                logging.info("Successfully created forked chat with %s messages.", len(messages))
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to create forked chat {thread_id}.",
                operation="create_chat_from_messages",
                cause=exc,
            ) from exc

    def add_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        sources: list | None = None,
        thoughts: str | None = None,
        attachments: list | None = None,
        stats: dict | None = None,
        thread_title: str | None = None,
        expected_revision: int | None = None,
    ):
        """Adds a new message to a specific chat thread."""
        try:
            if role != "assistant":
                thoughts = None
                stats = None
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if thread_title is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO threads (id, title, timestamp) VALUES (?, ?, ?)",
                        (thread_id, thread_title, _utc_now_iso()),
                    )
                self._check_chat_revision(conn, thread_id, expected_revision)
                conn.execute("""
                    INSERT INTO messages (thread_id, role, content, sources, thoughts, attachments, generation_stats_json, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    thread_id,
                    role,
                    content,
                    json.dumps(sources) if sources else None,
                    thoughts,
                    json.dumps(attachments) if attachments else None,
                    json.dumps(stats) if stats else None,
                    _utc_now_iso()
                ))
                # Update the thread's main timestamp to reflect recent activity
                conn.execute(
                    "UPDATE threads SET timestamp = ? WHERE id = ?",
                    (_utc_now_iso(), thread_id)
                )
                return str(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        except PersistenceError as exc:
            if exc.operation == "chat_revision_conflict":
                raise
            raise PersistenceError(
                f"Failed to add message to thread {thread_id}.",
                operation="add_message",
                cause=exc,
            ) from exc

    @staticmethod
    def _check_chat_revision(
        conn: sqlite3.Connection,
        thread_id: str,
        expected_revision: int | None,
    ) -> None:
        if expected_revision is None:
            return
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        actual_revision = int(
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0]
        )
        if actual_revision != expected_revision:
            raise PersistenceError(
                f"Chat revision changed (expected {expected_revision}, found {actual_revision}).",
                operation="chat_revision_conflict",
            )

    def load_chat_overview(self, thread_id: str) -> dict | None:
        """Thread metadata and message count, without loading the messages.

        chat_revision() is the message count, and a caller that needs only the
        revision or the title should not pay for every row of a long thread
        being read and JSON-decoded. Returns the same keys load_chat does,
        minus "messages", plus "revision".
        """
        try:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT id, title, timestamp, group_id FROM threads WHERE id = ?",
                    (thread_id,),
                ).fetchone()
                if not row:
                    return None
                overview = dict(row)
                if "timestamp" in overview:
                    overview["timestamp"] = _as_utc_iso(overview["timestamp"])
                overview["revision"] = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE thread_id = ?",
                        (thread_id,),
                    ).fetchone()[0]
                )
                return overview
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to load chat overview {thread_id}.",
                operation="load_chat_overview",
                cause=exc,
            ) from exc

    def load_chat(self, thread_id: str) -> dict | None:
        """Loads a full chat thread (metadata and messages) from the database."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, title, timestamp, group_id FROM threads WHERE id = ?",
                    (thread_id,),
                )
                thread_row = cursor.fetchone()
                if not thread_row:
                    return None
                
                chat_data = dict(thread_row)
                if 'timestamp' in chat_data:
                    chat_data['timestamp'] = _as_utc_iso(chat_data['timestamp'])
                
                cursor.execute(
                    "SELECT id, role, content, sources, thoughts, attachments, generation_stats_json, timestamp FROM messages "
                    "WHERE thread_id = ? ORDER BY timestamp ASC, id ASC",
                    (thread_id,)
                )
                messages = []
                for msg_row in cursor.fetchall():
                    msg_dict = dict(msg_row)
                    msg_dict['timestamp'] = _as_utc_iso(msg_dict.get('timestamp'))
                    if msg_dict.get('sources'):
                        msg_dict['sources'] = json.loads(msg_dict['sources'])
                    if msg_dict.get('attachments'):
                        msg_dict['attachments'] = json.loads(msg_dict['attachments'])
                    stats_json = msg_dict.pop('generation_stats_json', None)
                    msg_dict['stats'] = json.loads(stats_json) if stats_json else None
                    messages.append(msg_dict)
                
                chat_data['messages'] = messages
                return chat_data
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to load chat {thread_id}.",
                operation="load_chat",
                cause=exc,
            ) from exc
        except (json.JSONDecodeError, TypeError) as exc:
            raise PersistenceError(
                f"Stored data for chat {thread_id} is invalid.",
                operation="load_chat",
                cause=exc,
            ) from exc

    def delete_chat(self, thread_id: str):
        """Deletes a chat thread and all its associated messages from the database."""
        try:
            with self.connect() as conn:
                conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
                logging.info("Deleted chat thread.")
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to delete chat {thread_id}.",
                operation="delete_chat",
                cause=exc,
            ) from exc

    def delete_last_assistant_message(self, thread_id: str):
        """Deletes the most recent 'assistant' role message from a given thread."""
        try:
            with self.connect() as conn:
                conn.execute("""
                    DELETE FROM messages 
                    WHERE id = (
                        SELECT id FROM messages 
                        WHERE thread_id = ? AND role = 'assistant' 
                        ORDER BY timestamp DESC, id DESC
                        LIMIT 1
                    )
                """, (thread_id,))
                logging.info("Deleted the last assistant message for a chat thread.")
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to delete last assistant message for thread {thread_id}.",
                operation="delete_last_assistant_message",
                cause=exc,
            ) from exc

    def replace_message(
        self,
        thread_id: str,
        message_id: int,
        content: str,
        *,
        sources: list | None = None,
        thoughts: str | None = None,
        attachments: list | None = None,
        stats: dict | None = None,
        expected_revision: int | None = None,
    ) -> None:
        """Replace one assistant response without disturbing its user turn."""
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._check_chat_revision(conn, thread_id, expected_revision)
                # `attachments` only joins the SET clause when the caller actually
                # passed a value (including an explicit `[]`). Leaving it out of
                # both the clause and the params otherwise means an unspecified
                # `attachments=None` call leaves the existing column untouched,
                # matching InMemoryChatRepository.replace_message instead of
                # unconditionally wiping it to NULL.
                set_clauses = [
                    "content = ?",
                    "sources = ?",
                    "thoughts = ?",
                    "generation_stats_json = ?",
                    "timestamp = ?",
                ]
                params: list = [
                    content,
                    json.dumps(sources) if sources else None,
                    thoughts,
                    json.dumps(stats) if stats else None,
                    _utc_now_iso(),
                ]
                if attachments is not None:
                    set_clauses.append("attachments = ?")
                    params.append(json.dumps(attachments))
                params.extend([message_id, thread_id])
                cursor = conn.execute(
                    f"""
                    UPDATE messages
                    SET {", ".join(set_clauses)}
                    WHERE id = ? AND thread_id = ? AND role = 'assistant'
                    """,
                    params,
                )
                if cursor.rowcount != 1:
                    raise PersistenceError(
                        f"Assistant message {message_id} was not found.",
                        operation="replace_message",
                    )
                conn.execute(
                    "UPDATE threads SET timestamp = ? WHERE id = ?",
                    (_utc_now_iso(), thread_id),
                )
        except PersistenceError as exc:
            if exc.operation == "chat_revision_conflict":
                raise
            raise PersistenceError(
                f"Failed to replace message {message_id}.",
                operation="replace_message",
                cause=exc,
            ) from exc

    def update_chat_title(self, thread_id: str, new_title: str):
        """Updates the title of a specific chat thread."""
        try:
            with self.connect() as conn:
                conn.execute("UPDATE threads SET title = ? WHERE id = ?", (new_title, thread_id))
                logging.info("Renamed chat thread (private title omitted).")
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to rename chat {thread_id}.",
                operation="update_chat_title",
                cause=exc,
            ) from exc

    def get_all_chats_summary(self) -> list[dict]:
        """Retrieves a summary (id, title, timestamp, group_id) of all chats, sorted by recency."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, title, timestamp, group_id FROM threads ORDER BY timestamp DESC"
                )
                return [
                    {**dict(row), "timestamp": _as_utc_iso(row["timestamp"])}
                    for row in cursor.fetchall()
                ]
        except PersistenceError as exc:
            raise PersistenceError(
                "Failed to get chat summaries.",
                operation="get_all_chats_summary",
                cause=exc,
            ) from exc

    # -- chat groups (folders/projects) -----------------------------------

    def list_groups(self) -> list[dict]:
        """All groups in user-defined order, oldest-created first within a position."""
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    "SELECT id, name, position, collapsed, timestamp FROM chat_groups "
                    "ORDER BY position ASC, timestamp ASC"
                )
                return [
                    {
                        **dict(row),
                        "collapsed": bool(row["collapsed"]),
                        "timestamp": _as_utc_iso(row["timestamp"]),
                    }
                    for row in cursor.fetchall()
                ]
        except PersistenceError as exc:
            raise PersistenceError(
                "Failed to list chat groups.", operation="list_groups", cause=exc
            ) from exc

    def create_group(self, group_id: str, name: str) -> None:
        """Append a group after every existing one."""
        try:
            with self.connect() as conn:
                next_position = conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM chat_groups"
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO chat_groups (id, name, position, collapsed, timestamp) "
                    "VALUES (?, ?, ?, 0, ?)",
                    (group_id, name, next_position, _utc_now_iso()),
                )
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to create chat group {group_id}.",
                operation="create_group",
                cause=exc,
            ) from exc

    def update_group(
        self, group_id: str, *, name: str | None = None, collapsed: bool | None = None
    ) -> bool:
        """Rename and/or collapse a group. Returns False when it does not exist."""
        assignments: list[str] = []
        values: list[object] = []
        if name is not None:
            assignments.append("name = ?")
            values.append(name)
        if collapsed is not None:
            assignments.append("collapsed = ?")
            values.append(1 if collapsed else 0)
        if not assignments:
            return self.group_exists(group_id)
        values.append(group_id)
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    f"UPDATE chat_groups SET {', '.join(assignments)} WHERE id = ?",
                    tuple(values),
                )
                return cursor.rowcount > 0
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to update chat group {group_id}.",
                operation="update_group",
                cause=exc,
            ) from exc

    def delete_group(self, group_id: str) -> None:
        """Delete a group and return its chats to the ungrouped list.

        Chats are never deleted with their group -- losing conversations as a
        side effect of tidying the sidebar would be indefensible.
        """
        try:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE threads SET group_id = NULL WHERE group_id = ?", (group_id,)
                )
                conn.execute("DELETE FROM chat_groups WHERE id = ?", (group_id,))
        except PersistenceError as exc:
            raise PersistenceError(
                f"Failed to delete chat group {group_id}.",
                operation="delete_group",
                cause=exc,
            ) from exc

    def group_exists(self, group_id: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM chat_groups WHERE id = ?", (group_id,)
                ).fetchone()
                is not None
            )

    def set_chat_group(self, thread_id: str, group_id: str | None) -> bool:
        """Move a chat into a group, or out of every group when ``group_id`` is None."""
        try:
            with self.connect() as conn:
                if group_id is not None and conn.execute(
                    "SELECT 1 FROM chat_groups WHERE id = ?", (group_id,)
                ).fetchone() is None:
                    raise PersistenceError(
                        "Chat group does not exist.", operation="set_chat_group"
                    )
                cursor = conn.execute(
                    "UPDATE threads SET group_id = ? WHERE id = ?", (group_id, thread_id)
                )
                return cursor.rowcount > 0
        except PersistenceError as exc:
            if exc.operation == "set_chat_group":
                raise
            raise PersistenceError(
                f"Failed to move chat {thread_id}.",
                operation="set_chat_group",
                cause=exc,
            ) from exc

    def clear_all_data(self):
        """Deletes all data from the threads and messages tables."""
        logging.warning("Clearing all chat history from the database...")
        try:
            with self.connect() as conn:
                conn.execute("DELETE FROM messages")
                conn.execute("DELETE FROM threads")
                logging.info("Successfully cleared all chat history from the database.")
        except PersistenceError as exc:
            raise PersistenceError(
                "Failed to clear all chat history.",
                operation="clear_all_data",
                cause=exc,
            ) from exc


class PermanentMemoryManager:
    """Manages the persistence of long-term 'memory nuggets' for the AI."""
    MAX_MEMOS = 100
    MAX_MEMO_LENGTH = 500

    def __init__(
        self,
        memory_file_path: str | None = None,
        app_paths: AppPaths | None = None,
    ):
        """Initialize the manager and recover from a valid backup when needed."""
        if memory_file_path is None:
            resolved_paths = app_paths or AppPaths.for_current_user()
            memory_file_path = str(resolved_paths.permanent_memory)
        self.memory_file_path = memory_file_path
        self.backup_file_path = f"{self.memory_file_path}.bak"
        self._lock = threading.RLock()
        self.memos = self._load_memos()

    @staticmethod
    def _validate_memo_data(data: object) -> list[str]:
        if not isinstance(data, dict) or not isinstance(data.get('memos'), list):
            raise ValueError("memory file must contain a memos list")
        if not all(isinstance(memo, str) for memo in data['memos']):
            raise ValueError("memory entries must be strings")
        return list(data['memos'])

    @classmethod
    def _read_memos(cls, path: str) -> list[str]:
        with open(path, encoding='utf-8') as stream:
            return cls._validate_memo_data(json.load(stream))

    @classmethod
    def _atomic_copy_memos(cls, source: str, destination: str) -> None:
        """Copy a validated memory file without exposing a partial destination."""
        directory = os.path.dirname(os.path.abspath(destination))
        temporary_path = None
        try:
            fd, temporary_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(destination)}.",
                suffix='.tmp',
                dir=directory,
            )
            os.close(fd)
            shutil.copy2(source, temporary_path)
            cls._read_memos(temporary_path)
            os.replace(temporary_path, destination)
            temporary_path = None
        except (OSError, shutil.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PersistenceError(
                "Could not copy permanent memory safely.",
                operation="save_permanent_memory",
                cause=exc,
            ) from exc
        finally:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError as exc:
                    raise PersistenceError(
                        "Could not remove a temporary permanent-memory copy.",
                        operation="save_permanent_memory",
                        cause=exc,
                    ) from exc

    @classmethod
    def normalize_memos(cls, memos: list[str]) -> list[str]:
        """Validate, trim, cap, and case-insensitively deduplicate memos."""
        if not isinstance(memos, list):
            raise ValueError("memos must be a list")
        normalized: list[str] = []
        seen: set[str] = set()
        for memo in memos:
            if not isinstance(memo, str):
                raise ValueError("memory entries must be strings")
            memo = memo.strip()
            if not memo:
                continue
            if len(memo) > cls.MAX_MEMO_LENGTH:
                raise ValueError(f"memory entries may not exceed {cls.MAX_MEMO_LENGTH} characters")
            key = memo.casefold()
            if key in seen:
                continue
            if len(normalized) >= cls.MAX_MEMOS:
                raise ValueError(f"no more than {cls.MAX_MEMOS} memories may be stored")
            seen.add(key)
            normalized.append(memo)
        return normalized

    def _load_memos(self) -> list[str]:
        """
        Loads the list of memos from the JSON file.

        Returns:
            A list of memo strings, or an empty list if the file doesn't exist or is corrupt.
        """
        for candidate in (self.memory_file_path, self.backup_file_path):
            if not os.path.exists(candidate):
                continue
            try:
                memos = self._read_memos(candidate)
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
                logging.error(
                    "Failed to load permanent memory file (%s).",
                    type(exc).__name__,
                )
                continue
            if candidate == self.backup_file_path:
                try:
                    # A later save must never rotate a corrupt primary over the
                    # only good backup. Repair the primary before returning the
                    # recovered data, while keeping the backup unchanged if the
                    # repair cannot be completed.
                    self._atomic_copy_memos(candidate, self.memory_file_path)
                except PersistenceError as exc:
                    logging.error(
                        "Could not restore permanent memory file from backup (%s).",
                        type(exc).__name__,
                    )
            return memos
        return []

    def _prepare_primary_for_save(self) -> None:
        """Ensure the primary is valid before rotating it into the backup."""
        if not os.path.exists(self.memory_file_path):
            return
        try:
            self._read_memos(self.memory_file_path)
            return
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            primary_error = exc

        try:
            self._read_memos(self.backup_file_path)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            # Nothing left to protect: the primary is unreadable and so is the
            # backup. Refusing the save here made the corruption permanent --
            # every write failed, including the Clear that would have replaced
            # the damaged file outright, so the only repair was deleting the
            # file by hand from the data directory. Set the damaged bytes
            # aside instead, so they stay recoverable, and let the new
            # already-validated content be written over the gap.
            self._set_aside_corrupt_memory_file(primary_error)
            return
        self._atomic_copy_memos(self.backup_file_path, self.memory_file_path)

    def _set_aside_corrupt_memory_file(self, cause: BaseException) -> None:
        """Move an unrecoverable memory file out of the way of a fresh save.

        Moving rather than deleting: the damaged bytes may still be readable
        by a person even when this parser gives up on them. A single fixed
        name is used on purpose -- an unbounded ``.corrupt.1``, ``.corrupt.2``
        series would be its own slow leak in the data directory.
        """
        damaged_path = f"{self.memory_file_path}.corrupt"
        try:
            os.replace(self.memory_file_path, damaged_path)
        except OSError as exc:
            # Usually a lock. The save that follows would fail on the same
            # file anyway, so report the condition rather than pressing on.
            raise PersistenceError(
                "Cannot save permanent memory because the existing file is corrupt "
                "and could not be set aside.",
                operation="save_permanent_memory",
                cause=cause,
            ) from exc
        logging.warning(
            "The permanent memory file was unreadable (%s) and no valid backup "
            "existed; it was moved aside and a new file was written.",
            type(cause).__name__,
        )

    def _save_memos(self):
        """Validate and atomically replace the memory file, retaining a backup."""
        directory = os.path.dirname(os.path.abspath(self.memory_file_path))
        os.makedirs(directory, exist_ok=True)
        temporary_path = None
        try:
            fd, temporary_path = tempfile.mkstemp(
                prefix=f"{os.path.basename(self.memory_file_path)}.",
                suffix='.tmp',
                dir=directory,
            )
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'memos': self.normalize_memos(self.memos)}, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())

            with open(temporary_path, encoding='utf-8') as stream:
                self._validate_memo_data(json.load(stream))
            self._prepare_primary_for_save()
            if os.path.exists(self.memory_file_path):
                self._atomic_copy_memos(self.memory_file_path, self.backup_file_path)
            os.replace(temporary_path, self.memory_file_path)
            temporary_path = None
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PersistenceError(
                "Failed to save permanent memory atomically.",
                operation="save_permanent_memory",
                cause=exc,
            ) from exc
        finally:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError:
                    logging.warning("Could not remove temporary permanent-memory file.")

    def get_memos(self) -> list[str]:
        """
        Returns the current list of in-memory memos.

        Returns:
            A list of memo strings.
        """
        with self._lock:
            return list(self.memos)

    def add_memo(self, memo_text: str):
        """
        Adds a new, unique memo to the list and saves to disk.

        Args:
            memo_text (str): The fact to be remembered.
        """
        with self._lock:
            normalized = self.normalize_memos(self.memos + [memo_text])
            if normalized == self.memos:
                return
            previous_memos = list(self.memos)
            self.memos = normalized
            try:
                self._save_memos()
            except PersistenceError:
                self.memos = previous_memos
                raise

    def update_memos(self, memos: list[str]):
        """
        Replaces the entire list of memos with a new list and saves to disk.

        Args:
            memos (list[str]): The new, complete list of memos.
        """
        # Filter out any empty strings that might have come from the UI.
        with self._lock:
            previous_memos = list(self.memos)
            self.memos = self.normalize_memos(memos)
            try:
                self._save_memos()
            except PersistenceError:
                self.memos = previous_memos
                raise
            logging.info("Permanent memory updated with %s memos.", len(self.memos))

    def clear_memos(self):
        """Clears all memos from the list and saves the empty list to disk."""
        with self._lock:
            previous_memos = list(self.memos)
            self.memos.clear()
            try:
                self._save_memos()
            except PersistenceError:
                self.memos = previous_memos
                raise
