"""Transactional SQLite settings storage and legacy QSettings migration."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sqlite3
from collections.abc import Iterator

from cortex_backend.core.settings import CortexSettings

from .settings import (
    SettingsMigrationReport,
    SettingsReadResult,
    SettingsRepository,
    SettingsRepositoryError,
)


SETTINGS_SCHEMA_VERSION = 1
MIGRATION_KEY = "qsettings-to-sqlite-v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteSettingsRepository:
    """Store validated settings beside the existing chat database.

    The repository creates only additive settings tables. It never writes back
    to QSettings, so the legacy Qt reader remains a safe rollback path.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        legacy: SettingsRepository | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.backup_path = Path(f"{self.db_path}.bak")
        self.legacy = legacy
        self._pre_schema_backup = self._create_backup()
        self._ensure_schema()

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

    def _create_backup(self) -> str | None:
        if not self.db_path.exists():
            return None
        try:
            shutil.copy2(self.db_path, self.backup_path)
        except OSError as exc:
            raise SettingsRepositoryError("Could not create a settings database backup.") from exc
        return str(self.backup_path)

    def restore_backup(self) -> None:
        """Restore the last verified database backup without changing QSettings."""
        if not self.backup_path.exists():
            raise SettingsRepositoryError("No settings database backup is available.")
        try:
            shutil.copy2(self.backup_path, self.db_path)
        except OSError as exc:
            raise SettingsRepositoryError("Could not restore the settings database backup.") from exc
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
            return CortexSettings.model_validate_json(row["payload"]), "sqlite"
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
        backup_path = self._pre_schema_backup or self._create_backup()
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO cortex_settings
                        (id, schema_version, revision, payload, updated_at)
                    VALUES (1, ?, ?, ?, ?)
                    """,
                    (
                        SETTINGS_SCHEMA_VERSION,
                        legacy_result.settings.revision,
                        legacy_result.settings.model_dump_json(),
                        _utc_now(),
                    ),
                )
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
        except sqlite3.IntegrityError:
            # Another request completed the one-time import. The durable row is
            # authoritative and is returned below.
            existing = self._read_row()
            if existing is None:
                raise SettingsRepositoryError("Settings migration did not persist.")
            settings, source = existing
            return SettingsReadResult(
                settings=settings,
                source=source,
                migration=self._ledger_report(),
            )
        except SettingsRepositoryError:
            raise
        except Exception as exc:
            raise SettingsRepositoryError("Legacy settings migration failed.") from exc

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

    def save(self, settings: CortexSettings) -> None:
        if not isinstance(settings, CortexSettings):
            raise TypeError("settings must be a validated CortexSettings snapshot")
        self._create_backup()
        try:
            with self.connect() as connection:
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
                    (
                        SETTINGS_SCHEMA_VERSION,
                        settings.revision,
                        settings.model_dump_json(),
                        _utc_now(),
                    ),
                )
        except SettingsRepositoryError:
            raise
        except Exception as exc:
            raise SettingsRepositoryError("Could not save Cortex settings.") from exc
