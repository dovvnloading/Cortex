"""Persistence boundaries for backend services."""

from .chats import (
    ChatRepository,
    ChatRepositoryError,
    ChatRevisionConflict,
    InMemoryChatRepository,
    LegacyDatabaseChatRepository,
)
from .memories import (
    MemoryRepository,
    MemoryRepositoryError,
    InMemoryMemoryRepository,
    LegacyPermanentMemoryRepository,
)
from .legacy_settings import LegacySettingsReader
from .storage import DatabaseManager, PermanentMemoryManager, PersistenceError
from .settings import (
    InMemorySettingsRepository,
    SettingsReadResult,
    SettingsRepository,
    SettingsRepositoryError,
    SettingsRevisionConflict,
)

__all__ = [
    "ChatRepository",
    "ChatRepositoryError",
    "ChatRevisionConflict",
    "SettingsRevisionConflict",
    "InMemoryChatRepository",
    "LegacyDatabaseChatRepository",
    "MemoryRepository",
    "MemoryRepositoryError",
    "InMemoryMemoryRepository",
    "LegacyPermanentMemoryRepository",
    "LegacySettingsReader",
    "DatabaseManager",
    "PermanentMemoryManager",
    "PersistenceError",
    "InMemorySettingsRepository",
    "SettingsReadResult",
    "SettingsRepository",
    "SettingsRepositoryError",
]
