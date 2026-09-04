"""Deterministic test doubles for the local Cortex backend."""

from .dependencies import build_demo_dependencies
from .fake_ollama import (
    FakeGenerationEngine,
    FakeOllamaGateway,
    FakeOllamaState,
    create_fake_ollama_app,
)

__all__ = [
    "build_demo_dependencies",
    "FakeGenerationEngine",
    "FakeOllamaGateway",
    "FakeOllamaState",
    "create_fake_ollama_app",
]
