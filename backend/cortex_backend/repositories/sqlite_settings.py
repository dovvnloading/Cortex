"""Transactional SQLite settings storage and legacy QSettings migration."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from threading import Lock, RLock
from uuid import uuid4

from cortex_backend.core.settings import CortexSettings

from .settings import (
    SettingsMigrationReport,
    SettingsReadResult,
    SettingsRepository,
    SettingsRepositoryError,
    SettingsRevisionConflict,
)


SETTINGS_SCHEMA_VERSION = 1
MIGRATION_KEY = "qsettings-to-sqlite-v1"

# Top-level sections that CortexSettings used to carry and no longer does.
# The model is extra="forbid", so a payload written by an older build would
# otherwise fail validation outright and read as "Stored Cortex settings are
# invalid" -- losing a real workspace's settings over a field nothing uses.
# Dropping the key on read is enough: the next save writes the current shape.
RETIRED_SETTINGS_KEYS = frozenset({"suggestions"})
COLOCATED_MIGRATION_KEY = "chatdb-colocated-settings-to-own-file-v1"

_WRITE_LOCKS_GUARD = Lock()
_WRITE_LOCKS: dict[str, RLock] = {}


def _without_retired_keys(payload: str) -> dict:
    """Decode a stored settings payload, dropping sections since removed."""
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("stored settings payload must be a JSON object")
    return {key: value for key, value in decoded.items() if key not in RETIRED_SETTINGS_KEYS}


def _write_lock_for(path: Path) -> RLock:
    key = str(path.resolve(strict=False)).casefold()
    with _WRITE_LOCKS_GUARD:
        return _WRITE_LOCKS.setdefault(key, RLock())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteSettingsRepository:
    """Store validated settings in their own database file.

    The repository creates only additive settings tables. It never writes back
    to QSettings, so the legacy Qt reader remains a safe rollback path.

    Settings used to live inside the chat database. Every save takes a
    full-file backup copy first, so colocation meant each settings write
    byte-copied the whole transcript store. ``adopt_from`` performs the
    one-time move; see :meth:`_adopt_colocated_settings`.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        legacy: SettingsRepository | None = None,
        adopt_from: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.backup_path = Path(f"{self.db_path}.bak")
        # Keep one older verified snapshot so an interrupted backup rotation
        # cannot discard the only recovery copy.
        self.previous_backup_path = Path(f"{self.backup_path}.1")
        self.last_corrupt_path: Path | None = None
        self.legacy = legacy
        # Every repository instance for a database shares this lock. Backup
        # rotation is file I/O rather than SQLite I/O, so SQLite's own
        # transaction lock cannot serialize it for concurrent API requests.
        self._write_lock = _write_lock_for(self.db_path)
        # Fresh workspace loading fans out into settings and model requests.
        # Keep the one-time legacy import atomic within this process so those
        # requests cannot race to create the initial settings row.
        self._load_lock = RLock()
        with self._write_lock:
            self._pre_schema_backup = self._prepare_primary()
            self._ensure_schema()
            if adopt_from is not None:
                self._adopt_colocated_settings(Path(adopt_from))

    def _adopt_colocated_settings(self, source_db: Path) -> None:
        """Move settings out of a database they used to share with chat data.

        Runs once per install: if this settings database has no row yet but
        the old colocated database does, copy that row across. Without this,
        every existing install would silently revert to defaults on upgrade,
        which is a worse failure than the one being fixed.

        The source row is left in place. It is small, it costs nothing to
        keep, and leaving it makes downgrading to a previous Cortex build a
        non-event rather than a data-loss bug.
        """
        if source_db == self.db_path or not source_db.exists():
            return
        with self._load_lock:
            try:
                with self.connect() as connection:
                    already = connection.execute(
                        "SELECT 1 FROM cortex_settings WHERE id = 1"
                    ).fetchone()
                if already is not None:
                    return
            except SettingsRepositoryError:
                return

            source: sqlite3.Connection | None = None
            try:
                source = sqlite3.connect(source_db, timeout=10.0)
                source.row_factory = sqlite3.Row
                source.execute("PRAGMA busy_timeout = 10000")
                table = source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cortex_settings'"
                ).fetchone()
                if table is None:
                    return
                row = source.execute(
                    "SELECT schema_version, revision, payload, updated_at "
                    "FROM cortex_settings WHERE id = 1"
                ).fetchone()
            except sqlite3.Error:
                # The old database being unreadable must not stop Cortex from
                # starting -- it just means there is nothing to adopt, and the
                # normal legacy/default path takes over.
                return
            finally:
                if source is not None:
                    source.close()

            if row is None:
                return
            try:
                with self.connect() as connection:
                    connection.execute(
                        "INSERT OR IGNORE INTO cortex_settings "
                        "(id, schema_version, revision, payload, updated_at) "
                        "VALUES (1, ?, ?, ?, ?)",
                        (
                            int(row["schema_version"]),
                            int(row["revision"]),
                            str(row["payload"]),
                            str(row["updated_at"]),
                        ),
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO settings_migration_ledger "
                        "(migration_key, source, status, imported_keys, invalid_keys, "
                        "backup_path, message, applied_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            COLOCATED_MIGRATION_KEY,
                            str(source_db),
                            "applied",
                            "[]",
                            "[]",
                            None,
                            "Adopted settings from the chat database.",
                            _utc_now(),
                        ),
                    )
            except SettingsRepositoryError:
                return

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.db_path, timeout=10.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA synchronous = NORMAL")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise SettingsRepositoryError("SQLite settings operation failed.") from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connect() as connection:
                # WAL is stored in the database header, so this only has to run
                # once to apply to every later connection. It is not optional
                # here: connect() sets synchronous = NORMAL, and NORMAL is only
                # crash-safe under WAL. In the default rollback-journal mode the
                # same pragma lets an OS crash or power loss corrupt the file
                # outright, which is exactly why the chat store switched (see
                # legacy_storage._create_tables). Backups stay whole because
                # _create_backup checkpoints before copying.
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cortex_settings (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        schema_version INTEGER NOT NULL,
                        revision INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS settings_migration_ledger (
                        migration_key TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        status TEXT NOT NULL,
                        imported_keys TEXT NOT NULL,
                        invalid_keys TEXT NOT NULL,
                        backup_path TEXT,
                        message TEXT,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
        except SettingsRepositoryError:
            raise
        except Exception as exc:
            raise SettingsRepositoryError("Could not initialize settings schema.") from exc

    @staticmethod
    def _database_is_valid(path: Path) -> bool:
        """Return whether an existing SQLite file can be opened and checked."""
        connection: sqlite3.Connection | None = None
        try:
            # Read-only mode is important here: validation must not create or
            # mutate a file before we decide whether it is safe to back up.
            connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                timeout=10.0,
                uri=True,
            )
            result = connection.execute("PRAGMA integrity_check").fetchone()
            return result is not None and str(result[0]).lower() == "ok"
        except (OSError, sqlite3.Error):
            return False
        finally:
            if connection is not None:
                connection.close()

    @classmethod
    def _atomic_copy_database(cls, source: Path, destination: Path) -> None:
        """Copy a verified SQLite file without exposing a partial destination."""
        temporary_path: Path | None = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
            )
            os.close(fd)
            temporary_path = Path(temporary_name)
            shutil.copy2(source, temporary_path)
            if not cls._database_is_valid(temporary_path):
                raise OSError("database copy failed integrity validation")
            os.replace(temporary_path, destination)
            temporary_path = None
        except (OSError, shutil.Error) as exc:
            raise SettingsRepositoryError("Could not copy the settings database safely.") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError as exc:
                    raise SettingsRepositoryError(
                        "Could not remove a temporary settings database copy."
                    ) from exc

    def _prepare_primary(self) -> str | None:
        """Validate the primary before backup rotation, recovering if needed."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.db_path.exists():
            return None
        if self._database_is_valid(self.db_path):
            return self._create_backup()

        for candidate in (self.backup_path, self.previous_backup_path):
            if not candidate.exists() or not self._database_is_valid(candidate):
                continue
            corrupt_path = Path(f"{self.db_path}.corrupt-{uuid4().hex}")
            try:
                os.replace(self.db_path, corrupt_path)
            except OSError as exc:
                raise SettingsRepositoryError(
                    "Could not preserve the corrupt settings database before recovery."
                ) from exc
            try:
                self._atomic_copy_database(candidate, self.db_path)
            except SettingsRepositoryError:
                try:
                    os.replace(corrupt_path, self.db_path)
                except OSError as rollback_exc:
                    raise SettingsRepositoryError(
                        "Settings recovery failed and the corrupt primary could not be restored; "
                        f"it remains at {corrupt_path}."
                    ) from rollback_exc
                # The rollback put the original primary back, so its own
                # sidecars are still the right ones. Leave them alone.
                raise
            # Recovery succeeded: the sidecars still on disk describe the
            # quarantined database, not the backup that just replaced it.
            self._discard_sidecars()
            self.last_corrupt_path = corrupt_path
            return str(candidate)

        raise SettingsRepositoryError(
            "Settings database is corrupt and no valid backup is available."
        )

    def _sidecar_paths(self) -> tuple[Path, ...]:
        """The WAL sidecars SQLite keeps beside the primary database file."""
        return (Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm"))

    def _discard_sidecars(self) -> None:
        """Drop WAL sidecars left behind after the primary file is replaced.

        Replacing the primary out from under SQLite (recovery, restore) leaves
        the previous database's -wal and -shm in place. The next connection
        would treat them as belonging to the new file and replay them onto it.
        They describe a database that no longer exists, so remove them before
        anything opens the replacement.
        """
        for sidecar in self._sidecar_paths():
            try:
                sidecar.unlink(missing_ok=True)
            except OSError as exc:
                raise SettingsRepositoryError(
                    "Could not remove a stale settings write-ahead log."
                ) from exc

    def _checkpoint(self) -> None:
        """Fold the write-ahead log back into the primary file.

        In WAL mode a committed write can still live only in the -wal sidecar,
        so a plain file copy of the primary would omit it. TRUNCATE moves those
        commits into the main file and empties the sidecar, which is what makes
        the copy below a complete, self-contained snapshot.
        """
        with self.connect() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _create_backup(self) -> str | None:
        with self._write_lock:
            if not self.db_path.exists():
                return None
            if not self._database_is_valid(self.db_path):
                raise SettingsRepositoryError(
                    "Could not create a settings database backup from an invalid database."
                )
            try:
                self._checkpoint()
                # Preserve the prior verified backup before replacing the current
                # generation. If the new copy fails, the old .bak remains intact.
                if self.backup_path.exists() and self._database_is_valid(self.backup_path):
                    self._atomic_copy_database(self.backup_path, self.previous_backup_path)
                self._atomic_copy_database(self.db_path, self.backup_path)
            except SettingsRepositoryError:
                raise
            except OSError as exc:
                raise SettingsRepositoryError("Could not create a settings database backup.") from exc
            return str(self.backup_path)

    def restore_backup(self) -> None:
        """Restore the last verified database backup without changing QSettings."""
        with self._write_lock:
            candidate = next(
                (
                    path
                    for path in (self.backup_path, self.previous_backup_path)
                    if path.exists() and self._database_is_valid(path)
                ),
                None,
            )
            if candidate is None:
                raise SettingsRepositoryError("No valid settings database backup is available.")
            self._atomic_copy_database(candidate, self.db_path)
            # Before _ensure_schema opens the restored file: any -wal still
            # sitting there belongs to the database this copy just replaced.
            self._discard_sidecars()
            self._ensure_schema()

    def _read_row(self) -> tuple[CortexSettings, str] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT schema_version, payload FROM cortex_settings WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        if int(row["schema_version"]) > SETTINGS_SCHEMA_VERSION:
            raise SettingsRepositoryError("Cortex settings schema is newer than this release.")
        try:
            return CortexSettings.model_validate(_without_retired_keys(row["payload"])), "sqlite"
        except (TypeError, ValueError) as exc:
            raise SettingsRepositoryError("Stored Cortex settings are invalid.") from exc

    def _ledger_report(self) -> SettingsMigrationReport | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT migration_key, source, status, imported_keys, invalid_keys,
                       backup_path, message
                FROM settings_migration_ledger
                WHERE migration_key = ?
                """,
                (MIGRATION_KEY,),
            ).fetchone()
        if row is None:
            return None
        report = SettingsMigrationReport(
            status=row["status"],
            source=row["source"],
            migration_key=row["migration_key"],
            imported_keys=tuple(json.loads(row["imported_keys"])),
            invalid_keys=tuple(json.loads(row["invalid_keys"])),
            backup_path=row["backup_path"],
            message=row["message"],
        )
        if report.status == "migrated":
            return replace(report, status="already_migrated")
        return report

    def load(self, *, defaults: CortexSettings | None = None) -> SettingsReadResult:
        with self._load_lock:
            existing = self._read_row()
            if existing is not None:
                settings, source = existing
                return SettingsReadResult(
                    settings=settings,
                    source=source,
                    migration=self._ledger_report()
                    or SettingsMigrationReport(status="not_needed", source=source),
                )

            if self.legacy is None:
                settings = defaults or CortexSettings()
                return SettingsReadResult(
                    settings=settings,
                    source="sqlite",
                    migration=SettingsMigrationReport(status="not_needed", source="defaults"),
                )

            legacy_result = self.legacy.load(defaults=defaults)
            # A database that existed before this repository was initialized
            # was already snapshotted before additive schema work. A brand-new
            # database has no settings to preserve, so avoid a redundant file
            # copy while concurrent startup requests are beginning.
            backup_path = self._pre_schema_backup
            migrated_here = False
            try:
                with self.connect() as connection:
                    insert_result = connection.execute(
                        """
                        INSERT INTO cortex_settings
                            (id, schema_version, revision, payload, updated_at)
                        VALUES (1, ?, ?, ?, ?)
                        ON CONFLICT(id) DO NOTHING
                        """,
                        (
                            SETTINGS_SCHEMA_VERSION,
                            legacy_result.settings.revision,
                            legacy_result.settings.model_dump_json(),
                            _utc_now(),
                        ),
                    )
                    migrated_here = insert_result.rowcount == 1
                    if migrated_here:
                        connection.execute(
                            """
                            INSERT INTO settings_migration_ledger
                                (migration_key, source, status, imported_keys, invalid_keys,
                                 backup_path, message, applied_at)
                            VALUES (?, ?, 'migrated', ?, ?, ?, ?, ?)
                            """,
                            (
                                MIGRATION_KEY,
                                legacy_result.source,
                                json.dumps(legacy_result.present_keys),
                                json.dumps(legacy_result.invalid_keys),
                                backup_path,
                                "Legacy QSettings imported once; source left untouched.",
                                _utc_now(),
                            ),
                        )
            except SettingsRepositoryError as exc:
                raise SettingsRepositoryError("Legacy settings migration failed.") from exc
            except Exception as exc:
                raise SettingsRepositoryError("Legacy settings migration failed.") from exc

            if not migrated_here:
                # Another repository instance or process won the atomic
                # insert. Its committed row and ledger are authoritative.
                existing = self._read_row()
                if existing is None:
                    raise SettingsRepositoryError("Settings migration did not persist.")
                settings, source = existing
                report = self._ledger_report()
                if report is None:
                    raise SettingsRepositoryError("Settings migration completed without a migration ledger.")
                return SettingsReadResult(
                    settings=settings,
                    source=source,
                    migration=report,
                )

            report = SettingsMigrationReport(
                status="migrated",
                source=legacy_result.source,
                migration_key=MIGRATION_KEY,
                imported_keys=legacy_result.present_keys,
                invalid_keys=legacy_result.invalid_keys,
                backup_path=backup_path,
                message="Legacy QSettings imported once; source left untouched.",
            )
            return SettingsReadResult(
                settings=legacy_result.settings,
                source="sqlite_migrated",
                present_keys=legacy_result.present_keys,
                invalid_keys=legacy_result.invalid_keys,
                migration=report,
            )

    def save(
        self, settings: CortexSettings, *, expected_revision: int | None = None
    ) -> None:
        if not isinstance(settings, CortexSettings):
            raise TypeError("settings must be a validated CortexSettings snapshot")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        if expected_revision is not None and settings.revision != expected_revision + 1:
            raise ValueError("settings revision must be expected_revision + 1")
        with self._write_lock:
            self._create_backup()
            try:
                with self.connect() as connection:
                    values = (
                        SETTINGS_SCHEMA_VERSION,
                        settings.revision,
                        settings.model_dump_json(),
                        _utc_now(),
                    )
                    if expected_revision is None:
                        connection.execute(
                            """
                            INSERT INTO cortex_settings
                                (id, schema_version, revision, payload, updated_at)
                            VALUES (1, ?, ?, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET
                                schema_version = excluded.schema_version,
                                revision = excluded.revision,
                                payload = excluded.payload,
                                updated_at = excluded.updated_at
                            """,
                            values,
                        )
                        return

                    result = connection.execute(
                        """
                        UPDATE cortex_settings
                        SET schema_version = ?, revision = ?, payload = ?, updated_at = ?
                        WHERE id = 1 AND revision = ?
                        """,
                        (*values, expected_revision),
                    )
                    if result.rowcount == 1:
                        return
                    if expected_revision == 0:
                        try:
                            connection.execute(
                                """
                                INSERT INTO cortex_settings
                                    (id, schema_version, revision, payload, updated_at)
                                VALUES (1, ?, ?, ?, ?)
                                """,
                                values,
                            )
                            return
                        except sqlite3.IntegrityError:
                            pass
                    actual = connection.execute(
                        "SELECT revision FROM cortex_settings WHERE id = 1"
                    ).fetchone()
                    found = int(actual[0]) if actual is not None else 0
                    raise SettingsRevisionConflict(
                        "Settings revision changed "
                        f"(expected {expected_revision}, found {found})."
                    )
            except SettingsRevisionConflict:
                raise
            except SettingsRepositoryError:
                raise
            except Exception as exc:
                raise SettingsRepositoryError("Could not save Cortex settings.") from exc
