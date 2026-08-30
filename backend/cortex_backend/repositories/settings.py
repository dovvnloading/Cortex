"""Repository contract for validated Cortex settings snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Literal, Protocol, runtime_checkable

from cortex_backend.core.settings import CortexSettings


class SettingsRepositoryError(RuntimeError):
    """Raised when a settings backend cannot read or durably save settings."""


class SettingsRevisionConflict(SettingsRepositoryError):
    """The settings changed after the caller read its expected revision."""


@dataclass(frozen=True, slots=True)
class SettingsMigrationReport:
    """Safe summary of a legacy-settings import or settings-schema read."""

    status: Literal["not_needed", "migrated", "already_migrated", "failed"]
    source: str
    migration_key: str | None = None
    imported_keys: tuple[str, ...] = ()
    invalid_keys: tuple[str, ...] = ()
    backup_path: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class SettingsReadResult:
    settings: CortexSettings
    source: str
    present_keys: tuple[str, ...] = field(default_factory=tuple)
    invalid_keys: tuple[str, ...] = field(default_factory=tuple)
    migration: SettingsMigrationReport | None = None


@runtime_checkable
class SettingsRepository(Protocol):
    """Storage boundary consumed by future Qt-free settings services."""

    def load(self, *, defaults: CortexSettings | None = None) -> SettingsReadResult:
        """Load a validated settings snapshot without mutating the source."""

    def save(
        self, settings: CortexSettings, *, expected_revision: int | None = None
    ) -> None:
        """Durably save a complete validated settings snapshot."""


class InMemorySettingsRepository:
    """Deterministic settings repository for API tests and demo startup."""

    def __init__(self, settings: CortexSettings | None = None):
        self._settings = settings or CortexSettings()
        self._lock = RLock()

    def load(self, *, defaults: CortexSettings | None = None) -> SettingsReadResult:
        with self._lock:
            return SettingsReadResult(
                settings=self._settings,
                source="memory",
                present_keys=(),
                invalid_keys=(),
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
        with self._lock:
            if expected_revision is not None:
                actual_revision = self._settings.revision
                if actual_revision != expected_revision:
                    raise SettingsRevisionConflict(
                        "Settings revision changed "
                        f"(expected {expected_revision}, found {actual_revision})."
                    )
            self._settings = settings
