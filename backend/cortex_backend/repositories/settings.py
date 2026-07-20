"""Repository contract for validated Cortex settings snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from cortex_backend.core.settings import CortexSettings


class SettingsRepositoryError(RuntimeError):
    """Raised when a settings backend cannot read or durably save settings."""


@dataclass(frozen=True, slots=True)
class SettingsReadResult:
    settings: CortexSettings
    source: str
    present_keys: tuple[str, ...] = field(default_factory=tuple)
    invalid_keys: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class SettingsRepository(Protocol):
    """Storage boundary consumed by future Qt-free settings services."""

    def load(self, *, defaults: CortexSettings | None = None) -> SettingsReadResult:
        """Load a validated settings snapshot without mutating the source."""

    def save(self, settings: CortexSettings) -> None:
        """Durably save a complete validated settings snapshot."""
