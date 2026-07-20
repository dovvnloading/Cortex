"""Structural dependency types kept independent from the app factory module."""

from __future__ import annotations

from typing import Protocol

from cortex_backend.repositories.chats import ChatRepository
from cortex_backend.repositories.memories import MemoryRepository
from cortex_backend.repositories.settings import SettingsRepository
from cortex_backend.services.generation import GenerationService
from cortex_backend.services.models import ModelService


class BackendDependenciesProtocol(Protocol):
    settings: SettingsRepository
    chats: ChatRepository
    memories: MemoryRepository
    models: ModelService
    generation: GenerationService
