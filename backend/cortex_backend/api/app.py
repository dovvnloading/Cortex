"""FastAPI application factory for the staged local Cortex backend."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
import tempfile
from pathlib import Path
from collections.abc import Callable
from collections.abc import Iterable
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cortex_backend import __version__
from cortex_backend.repositories.chats import ChatRepository
from cortex_backend.repositories.memories import MemoryRepository
from cortex_backend.repositories.settings import SettingsRepository
from cortex_backend.services.generation import GenerationService
from cortex_backend.services.attachments import ChatAttachmentService
from cortex_backend.services.models import ModelCatalog
from cortex_backend.execution.cleanup import ExecutionCleanupSupervisor
from cortex_backend.execution.lifecycle import ExecutionLifecycle, LifecycleCoordinator
from cortex_backend.execution.repository import ExecutionRepository
from cortex_backend.llamacpp.server_manager import LlamaServerManager
from .jobs import JobRegistry
from .observability import RequestIdMiddleware
from .routers import build_router
from .security import SessionManager

logger = logging.getLogger(__name__)

# A rejected request can report where it went wrong and why, never what the
# caller sent: that would put prompts and attachment bodies into error
# responses, where devtools, proxies and logs keep them. What is left is
# bounded as well, so an absurd field name or a huge batch of errors cannot
# turn the report into an echo of its own.
_MAX_VALIDATION_ISSUES = 50
_MAX_VALIDATION_TEXT = 300


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_VALIDATION_TEXT else text[:_MAX_VALIDATION_TEXT] + "..."


def redact_validation_errors(errors: Iterable[Any]) -> list[dict[str, Any]]:
    """Keep each issue's location, message and type; drop ``input``, ``ctx`` and ``url``."""
    return [
        {
            "loc": [part if isinstance(part, int) else _clip(str(part)) for part in error.get("loc", ())],
            "msg": _clip(str(error.get("msg", ""))),
            "type": str(error.get("type", "")),
        }
        for error in list(errors)[:_MAX_VALIDATION_ISSUES]
    ]


# The largest legitimate request body is a base64 attachment: the 10 MiB file
# limit is a little under 14 MB once encoded (see MAX_CHAT_ATTACHMENT_BYTES and
# MAX_ATTACHMENT_BASE64_LENGTH in schemas). Everything else is far smaller.
MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024


class RequestBodyLimitMiddleware:
    """Refuse an oversized API request body before anything parses it.

    Field limits such as ``max_length`` only apply after Starlette has buffered
    and JSON-decoded the whole body, and some fields have no limit at all, so a
    single request could make the backend hold and decode as much as a client
    cared to send. This stops that at the door: a declared ``Content-Length``
    over the ceiling is refused without running the app, and a body that arrives
    without one (chunked) or that lies about its length is cut off as soon as
    the bytes received pass the ceiling.

    Only API paths are limited; the static frontend takes no request bodies.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = MAX_REQUEST_BODY_BYTES,
        path_prefix: str = "/api/",
    ) -> None:
        self.app = app
        self._max_body_bytes = max_body_bytes
        self._path_prefix = path_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(
            self._path_prefix
        ):
            await self.app(scope, receive, send)
            return
        declared = self._declared_length(scope)
        if declared is not None and declared > self._max_body_bytes:
            await self._refuse(send)
            return

        received = 0
        overflowed = False  # stop feeding the app body bytes
        refused = False  # our 413 is on the wire; the app's output is dropped
        app_response_started = False

        async def limited_receive() -> Message:
            nonlocal received, overflowed, refused
            if overflowed:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_body_bytes:
                    overflowed = True
                    if not app_response_started:
                        refused = True
                        await self._refuse(send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal app_response_started
            if refused:
                # The app noticed the disconnect and answers it (a 400, or
                # nothing). The caller has already been told why.
                return
            if message["type"] == "http.response.start":
                app_response_started = True
            await send(message)

        await self.app(scope, limited_receive, guarded_send)

    @staticmethod
    def _declared_length(scope: Scope) -> int | None:
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    length = int(value)
                except ValueError:
                    return None  # not a length; the running total still applies
                return length if length >= 0 else None
        return None

    async def _refuse(self, send: Send) -> None:
        body = b'{"detail":"Request body is too large."}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    # The rest of the upload is not going to be read.
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


async def _validation_error_response(request: Request, exc: Exception) -> JSONResponse:
    del request
    errors = exc.errors() if isinstance(exc, RequestValidationError) else ()
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": redact_validation_errors(errors)},
    )


@dataclass(slots=True)
class BackendDependencies:
    """Explicit service/repository dependencies injected into the API."""

    settings: SettingsRepository
    chats: ChatRepository
    memories: MemoryRepository
    models: ModelCatalog
    generation: GenerationService
    attachments: ChatAttachmentService | None = None


def create_app(
    dependencies: BackendDependencies,
    *,
    session_manager: SessionManager | None = None,
    preview: bool = True,
    allowed_hosts: Iterable[str] | None = None,
    serve_frontend: bool = False,
    frontend_dist: Path | None = None,
    ollama_host: str | None = None,
    handoff_secret: str | None = None,
    readiness_check: Callable[[], bool] | None = None,
    execution_coordinator: LifecycleCoordinator | None = None,
    execution_lifecycle: ExecutionLifecycle | None = None,
    cleanup_supervisor: ExecutionCleanupSupervisor | None = None,
    installation_principal_id: str | None = None,
    llamacpp_manager: LlamaServerManager | None = None,
    llamacpp_chat_client: object | None = None,
    default_gguf_models_dir: Path | None = None,
    closeables: Iterable[object] = (),
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
    if execution_coordinator is not None and execution_lifecycle is not None:
        raise ValueError("execution coordinator and lifecycle are mutually exclusive")
    lifecycle_repository = (
        execution_lifecycle.repository if execution_lifecycle is not None else None
    )
    attachment_repository = getattr(
        getattr(dependencies, "attachments", None), "repository", None
    ) if dependencies is not None else None
    cleanup_repository = lifecycle_repository or (
        execution_coordinator.repository if execution_coordinator is not None else None
    ) or attachment_repository
    if cleanup_supervisor is not None and cleanup_repository is not None and (
        cleanup_supervisor.repository is not cleanup_repository
    ):
        raise ValueError("cleanup supervisor repository does not match app repository")
    repository_principal = (
        execution_coordinator.repository.installation_principal_id
        if execution_coordinator is not None
        else (
            lifecycle_repository.installation_principal_id
            if lifecycle_repository is not None
            else None
        )
    )
    if (
        installation_principal_id is not None
        and repository_principal is not None
        and installation_principal_id != repository_principal
    ):
        raise ValueError("installation principal does not match execution repository")
    configured_principal = installation_principal_id or repository_principal
    manager = session_manager or SessionManager(
        allowed_hosts=allowed,
        installation_principal_id=configured_principal,
    )
    if (
        configured_principal is not None
        and manager.installation_principal_id != configured_principal
    ):
        raise ValueError("session manager principal does not match installation principal")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started_at = datetime.now(timezone.utc)
        try:
            if app.state.execution_lifecycle is not None:
                lifecycle_snapshot = app.state.execution_lifecycle.start()
                app.state.execution_coordinator = (
                    app.state.execution_lifecycle.coordinator
                    if lifecycle_snapshot.available
                    else None
                )
            if app.state.cleanup_supervisor is not None:
                app.state.cleanup_supervisor.start()
            app.state.ready = True
            yield
        finally:
            app.state.ready = False
            # Runtime teardown below must run even if job shutdown raises --
            # it is what actually terminates the llama-server child process,
            # and a worker that refuses to unwind must not leave that
            # process (and the GPU/RAM it holds) orphaned.
            try:
                await app.state.jobs.shutdown()
            except Exception:
                logger.exception(
                    "Cortex job registry shutdown raised; continuing with runtime teardown."
                )
            if app.state.cleanup_supervisor is not None:
                try:
                    app.state.cleanup_supervisor.stop()
                except Exception:
                    logger.exception(
                        "Cortex retention cleanup shutdown raised; continuing with runtime teardown."
                    )
            try:
                if app.state.execution_lifecycle is not None:
                    app.state.execution_lifecycle.stop()
                elif app.state.execution_coordinator is not None:
                    app.state.execution_coordinator.shutdown()
            except Exception:
                logger.exception(
                    "Cortex execution runtime shutdown raised; continuing with resource teardown."
                )
            finally:
                app.state.execution_coordinator = None
                if app.state.llamacpp_manager is not None:
                    close = getattr(app.state.llamacpp_manager, "close", None)
                    try:
                        (close if callable(close) else app.state.llamacpp_manager.stop)()
                    except Exception:
                        logger.exception(
                            "Cortex llama.cpp manager shutdown raised; continuing teardown."
                        )
                if app.state.llamacpp_chat_client is not None:
                    close = getattr(app.state.llamacpp_chat_client, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            logger.exception(
                                "Cortex llama.cpp chat client shutdown raised; continuing teardown."
                            )
                # Other owned clients (the Ollama HTTP client). An abandoned
                # worker blocked in one of their reads is otherwise joined by
                # the interpreter's exit hook, which can wait out the read
                # timeout; closing the client cuts that connection.
                for resource in app.state.closeables:
                    close = getattr(resource, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            logger.exception(
                                "Cortex client shutdown raised; continuing teardown."
                            )

    app = FastAPI(
        title="Cortex Local API",
        version=__version__,
        description="Loopback-only versioned backend contract for the Cortex web migration.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_exception_handler(RequestValidationError, _validation_error_response)
    app.state.dependencies = dependencies
    app.state.chat_attachment_service = getattr(
        app.state.dependencies, "attachments", None
    )
    app.state.session_manager = manager
    app.state.jobs = JobRegistry()
    app.state.execution_lifecycle = execution_lifecycle
    # The ordinary/demo API accepts protocol-compatible fakes in tests.  The
    # janitor is deliberately narrower: it relies on the durable tombstone
    # protocol and must never be auto-wired to an in-memory stand-in.
    app.state.cleanup_supervisor = cleanup_supervisor or (
        ExecutionCleanupSupervisor(cleanup_repository)
        if isinstance(cleanup_repository, ExecutionRepository)
        else None
    )
    app.state.execution_coordinator = execution_coordinator
    app.state.installation_principal_id = manager.installation_principal_id
    app.state.preview = preview
    app.state.ready = False
    app.state.shutting_down = False
    app.state.handoff_secret = handoff_secret
    app.state.shutdown_callback = None
    app.state.readiness_check = readiness_check
    app.state.required_paths = ()
    app.state.serve_frontend = serve_frontend
    app.state.frontend_dist = (
        frontend_dist or Path(__file__).resolve().parents[3] / "frontend" / "dist"
    ).resolve()
    app.state.ollama_host = ollama_host or os.environ.get(
        "CORTEX_OLLAMA_HOST", "http://127.0.0.1:11434"
    )
    app.state.ollama_setup_url = "https://ollama.com/download"
    app.state.llamacpp_manager = llamacpp_manager
    app.state.llamacpp_chat_client = llamacpp_chat_client
    app.state.closeables = tuple(closeables)
    app.state.default_gguf_models_dir = default_gguf_models_dir or (
        Path(tempfile.gettempdir()) / "cortex-gguf-models"
    )
    # TrustedHostMiddleware and ``SessionManager.validate_request_context``
    # (security.py) must agree on what counts as valid loopback, but they
    # reduce the IPv6 loopback Host header ("[::1]" or "[::1]:PORT")
    # differently, and Starlette changed its answer between releases:
    #   - before 1.7, a naive ``split(":")[0]`` yields the literal "[";
    #   - from 1.7, ``parse_host_header`` keeps the brackets: "[::1]".
    # The requirements range admits both, so allow both spellings. Neither
    # widens the boundary: the session guard still parses the header itself
    # and rejects anything that is not exactly an allowed host.
    middleware_allowed_hosts = list(allowed)
    if "::1" in allowed:
        for spelling in ("[", "[::1]"):
            if spelling not in middleware_allowed_hosts:
                middleware_allowed_hosts.append(spelling)
    # Added first, so it sits innermost of the three: the host check and CORS
    # run around it, and a refusal still carries the CORS headers a browser
    # needs in order to read the status.
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=middleware_allowed_hosts,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$",
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
        max_age=600,
    )
    # Added last, so it is outermost: every response, a refusal by the host check
    # or the size limit included, carries the request id, and everything the
    # request runs -- routes, the worker threads they start -- can read it.
    app.add_middleware(RequestIdMiddleware)
    app.include_router(build_router(), prefix="/api/v1")
    if serve_frontend:
        _mount_frontend(
            app,
            app.state.frontend_dist,
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
