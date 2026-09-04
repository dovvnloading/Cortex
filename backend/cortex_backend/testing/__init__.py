"""Deterministic test doubles for the local Cortex backend."""

from .dependencies import build_demo_dependencies
from .execution_preview import DurableFakeCoordinator
from .execution_preview_router import install_execution_preview
from .fake_execution import FakeExecutionPlan
from .fake_ollama import (
    FakeGenerationEngine,
    FakeOllamaGateway,
    FakeOllamaState,
    create_fake_ollama_app,
)

__all__ = [
    "build_demo_dependencies",
    "DurableFakeCoordinator",
    "FakeExecutionPlan",
    "install_execution_preview",
    "FakeGenerationEngine",
    "FakeOllamaGateway",
    "FakeOllamaState",
    "create_fake_ollama_app",
]
