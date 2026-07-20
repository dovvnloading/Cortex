"""Opt-in local FastAPI preview launcher for the staged Cortex web migration.

The legacy Qt desktop launcher remains the default. This module intentionally
binds only to loopback and does not launch a browser or modify user data beyond
the existing database/settings/memory adapters when the preview is selected.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "Chat_LLM" / "Chat_LLM"))

import ollama  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
import uvicorn  # noqa: E402

from cortex_backend.api import BackendDependencies, create_app  # noqa: E402
from cortex_backend.api.security import SessionManager  # noqa: E402
from cortex_backend.core.paths import AppPaths  # noqa: E402
from cortex_backend.repositories.chats import LegacyDatabaseChatRepository  # noqa: E402
from cortex_backend.repositories.memories import LegacyPermanentMemoryRepository  # noqa: E402
from cortex_backend.repositories.sqlite_settings import SQLiteSettingsRepository  # noqa: E402
from cortex_backend.services.generation import GenerationService  # noqa: E402
from cortex_backend.services.models import ModelService  # noqa: E402

from memory import DatabaseManager, PermanentMemoryManager  # noqa: E402
from qt_settings_adapter import QSettingsAdapter  # noqa: E402
from synthesis_agent import SynthesisAgent  # noqa: E402


def build_preview_app():
    """Build the explicit legacy-backed preview without starting a server."""
    paths = AppPaths.for_current_user()
    database = DatabaseManager(app_paths=paths)
    permanent_memory = PermanentMemoryManager(app_paths=paths)
    legacy_settings = QSettingsAdapter(QSettings("ChatLLM", "ChatLLM-Assistant"))
    settings_repository = SQLiteSettingsRepository(
        paths.database,
        legacy=legacy_settings,
    )
    ollama_host = os.environ.get("CORTEX_OLLAMA_HOST", "http://127.0.0.1:11434")
    client = ollama.Client(host=ollama_host)

    model_service = ModelService(client)
    generation_service = GenerationService(
        history_loader=lambda thread_id: (database.load_chat(thread_id) or {}).get(
            "messages", []
        ),
        memory_loader=permanent_memory.get_memos,
        engine_factory=lambda snapshot: SynthesisAgent(
            snapshot.model,
            snapshot.title_model,
            snapshot.translation_model,
            client,
        ),
    )
    dependencies = BackendDependencies(
        settings=settings_repository,
        chats=LegacyDatabaseChatRepository(database),
        memories=LegacyPermanentMemoryRepository(permanent_memory),
        models=model_service,
        generation=generation_service,
    )
    session_manager = SessionManager(
        allowed_hosts=("127.0.0.1", "localhost", "::1"),
    )
    app = create_app(
        dependencies,
        session_manager=session_manager,
        preview=True,
        qt_default=True,
        serve_frontend=True,
        ollama_host=ollama_host,
    )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the opt-in Cortex web preview.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")

    app = build_preview_app()
    print(f"Cortex preview listening on http://127.0.0.1:{args.port}")
    print(f"Cortex bootstrap token: {app.state.session_manager.bootstrap_token}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
