"""Persistence boundaries for backend services."""

from .chats import (
    ChatRepository,
    ChatRepositoryError,
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
from .legacy_storage import DatabaseManager, PermanentMemoryManager, PersistenceError
from .settings import (
    InMemorySettingsRepository,
    SettingsReadResult,
    SettingsRepository,
    SettingsRepositoryError,
)

__all__ = [
    "ChatRepository",
    "ChatRepositoryError",
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
