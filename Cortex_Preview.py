"""Build the local Cortex FastAPI application and its durable dependencies."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402
import ollama  # noqa: E402
import uvicorn  # noqa: E402

from cortex_backend.api import BackendDependencies, create_app  # noqa: E402
from cortex_backend.api.security import SessionManager  # noqa: E402
from cortex_backend.core.paths import AppPaths  # noqa: E402
from cortex_backend.execution.lifecycle import ExecutionLifecycle  # noqa: E402
from cortex_backend.execution.qualification import (  # noqa: E402
    QualificationLifecycleConfig,
    build_execution_lifecycle,
)
from cortex_backend.execution.repository import ExecutionRepository  # noqa: E402
from cortex_backend.llamacpp.binary_fetcher import BinaryFetcher  # noqa: E402
from cortex_backend.llamacpp.binary_release import CURRENT_RELEASE  # noqa: E402
from cortex_backend.llamacpp.chat_client import LlamaCppChatClient  # noqa: E402
from cortex_backend.llamacpp.model_directory import (  # noqa: E402
    GGUFModelDirectory,
    resolve_configured_directory,
)
from cortex_backend.llamacpp.server_manager import LlamaServerManager  # noqa: E402
from cortex_backend.repositories.chats import LegacyDatabaseChatRepository  # noqa: E402
from cortex_backend.repositories.legacy_settings import LegacySettingsReader  # noqa: E402
from cortex_backend.repositories.legacy_storage import (  # noqa: E402
    DatabaseManager,
    PermanentMemoryManager,
)
from cortex_backend.repositories.memories import LegacyPermanentMemoryRepository  # noqa: E402
from cortex_backend.repositories.sqlite_settings import SQLiteSettingsRepository  # noqa: E402
from cortex_backend.services.chat_client import OllamaChatClient, RoutingChatClient  # noqa: E402
from cortex_backend.services.generation import GenerationService  # noqa: E402
from cortex_backend.services.attachments import ChatAttachmentService  # noqa: E402
from cortex_backend.services.llm import SynthesisAgent  # noqa: E402
from cortex_backend.services.model_catalog import CombinedModelCatalog  # noqa: E402
from cortex_backend.services.models import ModelService  # noqa: E402


def build_preview_app(
    *,
    data_dir: Path | None = None,
    frontend_dist: Path | None = None,
    serve_frontend: bool = True,
    handoff_secret: str | None = None,
    execution_profile: str | None = "local",
    qualification: QualificationLifecycleConfig | None = None,
    execution_lifecycle: ExecutionLifecycle | None = None,
):
    """Build the local web application without starting a server.

    Source and packaged launches select the checked-in ``local`` execution
    profile. Pass ``execution_profile="disabled"`` for a chat-only preview or
    inject a lifecycle explicitly for qualification tests.
    """
    paths = AppPaths.from_data_dir(data_dir) if data_dir else AppPaths.for_current_user()
    execution_repository = ExecutionRepository(
        paths.execution_database,
        paths.execution_artifacts,
    )
    if execution_lifecycle is not None and (
        execution_profile is not None or qualification is not None
    ):
        raise ValueError(
            "execution lifecycle cannot be combined with an execution profile"
        )
    if execution_lifecycle is None:
        execution_lifecycle = build_execution_lifecycle(
            execution_repository,
            profile=execution_profile,
            qualification=qualification,
        )
    database = DatabaseManager(app_paths=paths)
    database.migrate_from_json_if_needed()
    permanent_memory = PermanentMemoryManager(app_paths=paths)
    settings_repository = SQLiteSettingsRepository(
        paths.database,
        legacy=LegacySettingsReader(),
    )
    ollama_host = os.environ.get("CORTEX_OLLAMA_HOST", "http://127.0.0.1:11434")
    client = ollama.Client(
        host=ollama_host,
        timeout=httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=5.0),
    )

    def gguf_directory() -> Path:
        # Re-read settings each call (cheap SQLite read, same pattern the API
        # routes already use) so a Settings change takes effect immediately
        # without restarting Cortex.
        current = settings_repository.load().settings
        return resolve_configured_directory(
            current.models.gguf_directory, paths.default_gguf_models_dir
        )

    llamacpp_manager = LlamaServerManager(
        runtime_dir=paths.llamacpp_runtime_dir,
        fetcher=BinaryFetcher(paths.llamacpp_runtime_dir),
        release=CURRENT_RELEASE,
        gpu_backend_setting=lambda: settings_repository.load().settings.llamacpp.gpu_backend,
        models_directory=gguf_directory,
    )
    gguf_model_directory = GGUFModelDirectory(gguf_directory)
    model_catalog = CombinedModelCatalog(ModelService(client), gguf_model_directory)
    routing_chat_client = RoutingChatClient(
        OllamaChatClient(client),
        LlamaCppChatClient(llamacpp_manager, models_directory=gguf_directory),
    )
    generation_service = GenerationService(
        history_loader=lambda thread_id: (database.load_chat(thread_id) or {}).get(
            "messages", []
        ),
        memory_loader=permanent_memory.get_memos,
        engine_factory=lambda snapshot: SynthesisAgent(
            snapshot.model,
            snapshot.title_model,
            snapshot.translation_model,
            routing_chat_client,
            code_execution_eligible=snapshot.code_execution_eligible,
            bypass_system_prompt=snapshot.bypass_system_prompt,
        ),
    )
    dependencies = BackendDependencies(
        settings=settings_repository,
        chats=LegacyDatabaseChatRepository(database),
        memories=LegacyPermanentMemoryRepository(permanent_memory),
        models=model_catalog,
        generation=generation_service,
        attachments=ChatAttachmentService(execution_repository),
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
        execution_lifecycle=execution_lifecycle,
        installation_principal_id=execution_repository.installation_principal_id,
        llamacpp_manager=llamacpp_manager,
        default_gguf_models_dir=paths.default_gguf_models_dir,
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
