"""Deterministic test doubles for the local Cortex backend."""

from .fake_ollama import (
    FakeGenerationEngine,
    FakeOllamaGateway,
    FakeOllamaState,
    create_fake_ollama_app,
)

__all__ = [
    "FakeGenerationEngine",
    "FakeOllamaGateway",
    "FakeOllamaState",
    "create_fake_ollama_app",
]
