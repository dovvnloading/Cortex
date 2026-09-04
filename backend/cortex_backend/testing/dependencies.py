"""In-memory dependency wiring for tests and deterministic demos.

This used to live in ``cortex_backend.api.app``, which meant the production
application module imported a fake model gateway at module scope and shipped
it in the packaged build. Worse, ``create_app`` fell back to it whenever it was
called without dependencies, so a mis-wired composition root produced a working
application backed by a fake instead of failing.

``create_app`` now requires its dependencies. Anything that wants the in-memory
stack asks for it here, by name.
"""

from __future__ import annotations

from cortex_backend.api.app import BackendDependencies
from cortex_backend.repositories.chats import InMemoryChatRepository
from cortex_backend.repositories.memories import InMemoryMemoryRepository
from cortex_backend.repositories.settings import InMemorySettingsRepository
from cortex_backend.services.attachments import ChatAttachmentService
from cortex_backend.services.generation import GenerationService
from cortex_backend.services.models import ModelService

from .fake_ollama import FakeGenerationEngine, FakeOllamaGateway, FakeOllamaState


def build_demo_dependencies(
    *,
    ollama_state: FakeOllamaState | None = None,
) -> BackendDependencies:
    """Build deterministic in-memory dependencies without Qt or Ollama."""
    state = ollama_state or FakeOllamaState()
    settings = InMemorySettingsRepository()
    chats = InMemoryChatRepository()
    memories = InMemoryMemoryRepository()
    gateway = FakeOllamaGateway(state)
    models = ModelService(gateway)
    generation = GenerationService(
        history_loader=lambda thread_id: (chats.get_chat(thread_id) or {}).get(
            "messages", []
        ),
        memory_loader=memories.get_memos,
        engine_factory=lambda snapshot: FakeGenerationEngine(state),
    )
    return BackendDependencies(
        settings=settings,
        chats=chats,
        memories=memories,
        models=models,
        generation=generation,
        attachments=ChatAttachmentService(),
    )
