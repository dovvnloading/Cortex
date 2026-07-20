"""Qt-free persistence boundaries for backend services."""

from .settings import (
    SettingsReadResult,
    SettingsRepository,
    SettingsRepositoryError,
)

__all__ = [
    "SettingsReadResult",
    "SettingsRepository",
    "SettingsRepositoryError",
]
