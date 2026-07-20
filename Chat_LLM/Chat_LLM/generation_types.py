"""Compatibility exports for the canonical Qt-free generation contracts."""

from cortex_backend.core.generation import (
    ConnectionResult,
    ConnectionStatus,
    GenerationResult,
    GenerationSnapshot,
    MemoryCommand,
    ModelOperationError,
    TranslationResult,
)

__all__ = [
    "ConnectionResult",
    "ConnectionStatus",
    "GenerationResult",
    "GenerationSnapshot",
    "MemoryCommand",
    "ModelOperationError",
    "TranslationResult",
]
