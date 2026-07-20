"""FastAPI application factory for the staged local Cortex backend."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cortex_backend.repositories.chats import ChatRepository, InMemoryChatRepository
from cortex_backend.repositories.memories import (
    InMemoryMemoryRepository,
    MemoryRepository,
)
from cortex_backend.repositories.settings import (
    InMemorySettingsRepository,
    SettingsRepository,
)
from cortex_backend.services.generation import GenerationService
from cortex_backend.services.models import ModelService
from cortex_backend.testing.fake_ollama import (
    FakeGenerationEngine,
    FakeOllamaGateway,
    FakeOllamaState,
)

from .jobs import JobRegistry
from .routes import build_router
from .security import SessionManager


@dataclass(slots=True)
class BackendDependencies:
    """Explicit service/repository dependencies injected into the API."""

    settings: SettingsRepository
    chats: ChatRepository
    memories: MemoryRepository
    models: ModelService
    generation: GenerationService


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
    )


def create_app(
    dependencies: BackendDependencies | None = None,
    *,
    session_manager: SessionManager | None = None,
    preview: bool = True,
    qt_default: bool = True,
    allowed_hosts: Iterable[str] | None = None,
    serve_frontend: bool = False,
    frontend_dist: Path | None = None,
) -> FastAPI:
    """Create a request-safe local API without import-time side effects."""
    if allowed_hosts is None:
        allowed = (
            tuple(session_manager.allowed_hosts)
            if session_manager
            else (
                "127.0.0.1",
                "localhost",
                "::1",
            )
        )
    else:
        allowed = tuple(allowed_hosts)
    manager = session_manager or SessionManager(allowed_hosts=allowed)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started_at = datetime.now(timezone.utc)
        yield
        await app.state.jobs.shutdown()

    app = FastAPI(
        title="Cortex Local API",
        version="0.1.0",
        description="Loopback-only versioned backend contract for the Cortex web migration.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.dependencies = dependencies or build_demo_dependencies()
    app.state.session_manager = manager
    app.state.jobs = JobRegistry()
    app.state.preview = preview
    app.state.qt_default = qt_default
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(allowed),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$",
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
        max_age=600,
    )
    app.include_router(build_router(), prefix="/api/v1")
    if serve_frontend:
        _mount_frontend(
            app,
            frontend_dist
            or Path(__file__).resolve().parents[3] / "frontend" / "dist",
        )
    return app


def _mount_frontend(app: FastAPI, frontend_dist: Path) -> None:
    """Serve a verified production bundle without intercepting API paths."""
    dist = frontend_dist.resolve()
    index = dist / "index.html"
    if not index.is_file():
        return
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend_route(path: str):
        if path.startswith("api/"):
            return FileResponse(index, status_code=404)
        candidate = (dist / path).resolve()
        if candidate.is_relative_to(dist) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)
