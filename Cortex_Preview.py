"""Build the local Cortex FastAPI application and its durable dependencies."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

import ollama  # noqa: E402
import uvicorn  # noqa: E402

from cortex_backend.api import BackendDependencies, create_app  # noqa: E402
from cortex_backend.api.security import SessionManager  # noqa: E402
from cortex_backend.core.paths import AppPaths  # noqa: E402
from cortex_backend.execution.repository import ExecutionRepository  # noqa: E402
from cortex_backend.repositories.chats import LegacyDatabaseChatRepository  # noqa: E402
from cortex_backend.repositories.legacy_settings import LegacySettingsReader  # noqa: E402
from cortex_backend.repositories.legacy_storage import (  # noqa: E402
    DatabaseManager,
    PermanentMemoryManager,
)
from cortex_backend.repositories.memories import LegacyPermanentMemoryRepository  # noqa: E402
from cortex_backend.repositories.sqlite_settings import SQLiteSettingsRepository  # noqa: E402
from cortex_backend.services.generation import GenerationService  # noqa: E402
from cortex_backend.services.llm import SynthesisAgent  # noqa: E402
from cortex_backend.services.models import ModelService  # noqa: E402


def build_preview_app(
    *,
    data_dir: Path | None = None,
    frontend_dist: Path | None = None,
    serve_frontend: bool = True,
    handoff_secret: str | None = None,
):
    """Build the local web application without starting a server."""
    paths = AppPaths.from_data_dir(data_dir) if data_dir else AppPaths.for_current_user()
    execution_repository = ExecutionRepository(
        paths.execution_database,
        paths.execution_artifacts,
    )
    database = DatabaseManager(app_paths=paths)
    database.migrate_from_json_if_needed()
    permanent_memory = PermanentMemoryManager(app_paths=paths)
    settings_repository = SQLiteSettingsRepository(
        paths.database,
        legacy=LegacySettingsReader(),
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

    def readiness_check() -> bool:
        """Verify durable paths, SQLite schema, memory, and settings access."""
        if not paths.data_dir.is_dir() or not paths.database.is_file():
            return False
        with database.connect() as connection:
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        if schema_version > database.SCHEMA_VERSION:
            return False
        if not {"threads", "messages"}.issubset(tables):
            return False
        permanent_memory.get_memos()
        settings_repository.load()
        if not paths.execution_database.is_file():
            return False
        execution_repository.installation_principal_id
        return True

    session_manager = SessionManager(
        allowed_hosts=("127.0.0.1", "localhost", "::1"),
        installation_principal_id=execution_repository.installation_principal_id,
    )
    app = create_app(
        dependencies,
        session_manager=session_manager,
        preview=True,
        serve_frontend=serve_frontend,
        frontend_dist=frontend_dist,
        ollama_host=ollama_host,
        handoff_secret=handoff_secret,
        readiness_check=readiness_check,
        installation_principal_id=execution_repository.installation_principal_id,
    )
    app.state.execution_repository = execution_repository
    app.state.required_paths = (
        paths.data_dir,
        paths.database,
        paths.execution_database,
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
