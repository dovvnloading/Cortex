"""Versioned resource and job routes for the local Cortex API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import json
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from cortex_backend.core.generation import ConnectionResult, GenerationSnapshot
from cortex_backend.core.settings import CortexSettings

from .app_types import BackendDependenciesProtocol
from .jobs import JobConflict, JobNotFound, JobOwnershipError, JobSnapshot
from .schemas import (
    AddMemoryRequest,
    AddMessageRequest,
    ChatResponse,
    ChatSummary,
    ClearMemoryRequest,
    CreateChatRequest,
    GenerationRequest,
    HealthResponse,
    JobAccepted,
    JobStatusResponse,
    MemoryResponse,
    ModelResponse,
    RenameChatRequest,
    ReplaceMemoryRequest,
    SessionExchangeRequest,
    SessionExchangeResponse,
    SettingsResponse,
    SettingsUpdateRequest,
    SSEEvent,
    SystemResponse,
)
from .security import SessionPrincipal, SessionSecurityError


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse)
    def health(request: Request) -> HealthResponse:
        request.app.state.session_manager.validate_request_context(request)
        return HealthResponse()

    @router.post("/session/exchange", response_model=SessionExchangeResponse)
    def exchange(
        request: Request,
        payload: SessionExchangeRequest,
    ) -> SessionExchangeResponse:
        manager = request.app.state.session_manager
        manager.validate_request_context(request)
        try:
            exchanged = manager.exchange(payload.bootstrap_token)
        except SessionSecurityError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Cortex bootstrap token invalid or already used",
            ) from exc
        return SessionExchangeResponse(
            session_token=exchanged.token,
            expires_at=exchanged.principal.expires_at,
        )

    def require_session(request: Request) -> SessionPrincipal:
        return request.app.state.session_manager.require(request)

    def dependencies(request: Request) -> BackendDependenciesProtocol:
        return request.app.state.dependencies

    @router.get("/system", response_model=SystemResponse)
    def system(
        request: Request, _: SessionPrincipal = Depends(require_session)
    ) -> SystemResponse:
        return SystemResponse(
            preview=request.app.state.preview,
            qt_default=request.app.state.qt_default,
            started_at=request.app.state.started_at,
        )

    @router.get("/chats", response_model=list[ChatSummary])
    def list_chats(
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> list[ChatSummary]:
        try:
            return [
                ChatSummary.model_validate(item) for item in deps.chats.list_summaries()
            ]
        except Exception as exc:
            _raise_repository_error("list chats", exc)

    @router.get("/chats/{thread_id}", response_model=ChatResponse)
    def get_chat(
        thread_id: str,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatResponse:
        try:
            chat = deps.chats.get_chat(thread_id)
        except Exception as exc:
            _raise_repository_error("load chat", exc)
        if chat is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found."
            )
        return _chat_response(chat)

    @router.post(
        "/chats", response_model=ChatResponse, status_code=status.HTTP_201_CREATED
    )
    def create_chat(
        payload: CreateChatRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatResponse:
        thread_id = uuid4().hex
        try:
            deps.chats.create_chat(thread_id, payload.title.strip())
            chat = deps.chats.get_chat(thread_id)
        except Exception as exc:
            _raise_repository_error("create chat", exc)
        if chat is None:
            raise HTTPException(
                status_code=500, detail="Chat creation did not persist."
            )
        return _chat_response(chat)

    @router.patch("/chats/{thread_id}", response_model=ChatResponse)
    def rename_chat(
        thread_id: str,
        payload: RenameChatRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatResponse:
        try:
            if deps.chats.get_chat(thread_id) is None:
                raise HTTPException(status_code=404, detail="Chat not found.")
            deps.chats.rename_chat(thread_id, payload.title.strip())
            chat = deps.chats.get_chat(thread_id)
        except HTTPException:
            raise
        except Exception as exc:
            _raise_repository_error("rename chat", exc)
        if chat is None:
            raise HTTPException(status_code=500, detail="Chat rename did not persist.")
        return _chat_response(chat)

    @router.delete("/chats/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_chat(
        thread_id: str,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> None:
        try:
            deps.chats.delete_chat(thread_id)
        except Exception as exc:
            _raise_repository_error("delete chat", exc)

    @router.post("/chats/{thread_id}/messages", response_model=ChatResponse)
    def add_message(
        thread_id: str,
        payload: AddMessageRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatResponse:
        try:
            existing = deps.chats.get_chat(thread_id)
            deps.chats.add_message(
                thread_id,
                payload.role,
                payload.content,
                sources=payload.sources,
                thoughts=payload.thoughts,
                thread_title="New Chat" if existing is None else None,
            )
            chat = deps.chats.get_chat(thread_id)
        except Exception as exc:
            _raise_repository_error("save message", exc)
        if chat is None:
            raise HTTPException(status_code=500, detail="Message did not persist.")
        return _chat_response(chat)

    @router.get("/settings", response_model=SettingsResponse)
    def get_settings(
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> SettingsResponse:
        try:
            loaded = deps.settings.load()
        except Exception as exc:
            _raise_repository_error("load settings", exc)
        return SettingsResponse(
            settings=loaded.settings,
            source=loaded.source,
            invalid_keys=loaded.invalid_keys,
        )

    @router.put("/settings", response_model=SettingsResponse)
    def update_settings(
        payload: SettingsUpdateRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> SettingsResponse:
        try:
            current = deps.settings.load().settings
            updated = payload.settings.model_copy(
                update={"revision": current.revision + 1}
            )
            deps.settings.save(updated)
            loaded = deps.settings.load()
        except Exception as exc:
            _raise_repository_error("save settings", exc)
        return SettingsResponse(
            settings=loaded.settings,
            source=loaded.source,
            invalid_keys=loaded.invalid_keys,
        )

    @router.get("/memories", response_model=MemoryResponse)
    def get_memories(
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> MemoryResponse:
        try:
            return MemoryResponse(memos=deps.memories.get_memos())
        except Exception as exc:
            _raise_repository_error("load memories", exc)

    @router.post("/memories", response_model=MemoryResponse)
    def add_memory(
        payload: AddMemoryRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> MemoryResponse:
        try:
            return MemoryResponse(memos=deps.memories.add_memo(payload.memo))
        except Exception as exc:
            _raise_repository_error("save memory", exc)

    @router.put("/memories", response_model=MemoryResponse)
    def replace_memories(
        payload: ReplaceMemoryRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> MemoryResponse:
        try:
            return MemoryResponse(memos=deps.memories.replace_memos(payload.memos))
        except Exception as exc:
            _raise_repository_error("replace memories", exc)

    @router.post("/memories/clear", response_model=MemoryResponse)
    def clear_memories(
        payload: ClearMemoryRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> MemoryResponse:
        if not payload.confirm:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Clearing permanent memories requires explicit confirmation.",
            )
        try:
            deps.memories.clear_memos()
            return MemoryResponse(memos=deps.memories.get_memos())
        except Exception as exc:
            _raise_repository_error("clear memories", exc)

    @router.get("/models", response_model=ModelResponse)
    def list_models(
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ModelResponse:
        settings = _load_settings(deps)
        required, optional = _model_sets(settings)
        installed = deps.models.list_installed()
        return _model_response(required, optional, installed)

    @router.post(
        "/jobs/models", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED
    )
    async def check_models(
        request: Request,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobAccepted:
        settings = _load_settings(deps)
        required, optional = _model_sets(settings)

        def runner(sink, cancel_event):
            if cancel_event.is_set():
                return {"cancelled": True}
            sink.publish_progress("model_check", "Checking required model tags.")
            installed = deps.models.list_installed()
            missing = tuple(model for model in required if model not in installed)
            if missing:
                sink.publish_progress(
                    "model_pull",
                    f"Pulling {len(missing)} required model tag(s).",
                )
            connection = deps.models.check(
                required_models=required,
                optional_models=optional,
            )
            return {"connection": asdict(connection)}

        try:
            snapshot = await request.app.state.jobs.start(
                kind="models",
                owner=principal.session_id,
                thread_id=None,
                runner=runner,
            )
        except JobConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        return _accepted(snapshot)

    @router.post(
        "/jobs/generation",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_generation(
        payload: GenerationRequest,
        request: Request,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobAccepted:
        settings = _load_settings(deps)
        job_id = uuid4().hex
        generation_snapshot = _generation_snapshot(job_id, payload, settings)

        def runner(sink, cancel_event):
            if cancel_event.is_set():
                return {"cancelled": True}
            result = deps.generation.generate(
                generation_snapshot,
                progress_sink=sink,
                cancellation_event=cancel_event,
            )
            return {
                "response": result.response,
                "thoughts": result.thoughts,
                "memory_command": {
                    "additions": list(result.memory_command.additions),
                    "clear_requested": result.memory_command.clear_requested,
                },
            }

        try:
            snapshot = await request.app.state.jobs.start(
                kind="generation",
                owner=principal.session_id,
                thread_id=payload.thread_id,
                runner=runner,
                request_id=payload.request_id,
            )
        except JobConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        return _accepted(snapshot)

    @router.get("/jobs/{job_id}", response_model=JobStatusResponse)
    def get_job(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobStatusResponse:
        snapshot = _job_status(request, job_id, principal)
        return _job_response(snapshot)

    @router.post("/jobs/{job_id}/cancel", response_model=JobStatusResponse)
    def cancel_job(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobStatusResponse:
        try:
            snapshot = request.app.state.jobs.cancel(job_id, owner=principal.session_id)
        except (JobNotFound, JobOwnershipError) as exc:
            _raise_job_error(exc)
        return _job_response(snapshot)

    @router.get(
        "/jobs/{job_id}/events",
        response_model=SSEEvent,
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Server-sent job events.",
                "content": {
                    "text/event-stream": {
                        "schema": {"$ref": "#/components/schemas/SSEEvent"}
                    }
                },
            }
        },
    )
    async def job_events(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> StreamingResponse:
        cursor_header = request.headers.get("last-event-id", "0")
        try:
            cursor = int(cursor_header or "0")
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Last-Event-ID must be an integer."
            ) from exc
        if cursor < 0:
            raise HTTPException(
                status_code=400, detail="Last-Event-ID must be non-negative."
            )
        try:
            request.app.state.jobs.status(job_id, owner=principal.session_id)
            event_stream = request.app.state.jobs.events(
                job_id,
                owner=principal.session_id,
                after_sequence=cursor,
            )
        except (JobNotFound, JobOwnershipError) as exc:
            _raise_job_error(exc)

        async def stream():
            async for event in event_stream:
                payload = SSEEvent(
                    id=event.sequence,
                    job_id=event.job_id,
                    kind=event.kind,
                    status=event.status,
                    phase=event.phase,
                    data=dict(event.data),
                ).model_dump(mode="json")
                yield (
                    f"id: {event.sequence}\n"
                    f"event: {event.kind}\n"
                    f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


def _load_settings(deps: BackendDependenciesProtocol) -> CortexSettings:
    try:
        return deps.settings.load().settings
    except Exception as exc:
        _raise_repository_error("load settings", exc)
        raise AssertionError("unreachable")


def _model_sets(settings: CortexSettings) -> tuple[tuple[str, ...], tuple[str, ...]]:
    required = (settings.models.chat,)
    optional: list[str] = []
    if settings.translation.enabled:
        optional.append(settings.models.translation)
    if settings.suggestions.enabled:
        optional.append(settings.suggestions.model)
    return required, tuple(
        model for model in dict.fromkeys(optional) if model not in required
    )


def _model_response(
    required: tuple[str, ...],
    optional: tuple[str, ...],
    installed: tuple[str, ...],
    connection: ConnectionResult | None = None,
) -> ModelResponse:
    missing = tuple(model for model in required if model not in installed)
    optional_missing = tuple(model for model in optional if model not in installed)
    return ModelResponse(
        required_models=required,
        optional_models=optional,
        installed_models=installed,
        missing_models=missing,
        optional_missing_models=optional_missing,
        connection=connection,
    )


def _generation_snapshot(
    job_id: str,
    payload: GenerationRequest,
    settings: CortexSettings,
) -> GenerationSnapshot:
    return GenerationSnapshot(
        job_id=job_id,
        thread_id=payload.thread_id,
        user_input=payload.user_input,
        model=settings.models.chat,
        title_model=settings.models.title,
        translation_model=settings.models.translation,
        model_options={
            "temperature": settings.generation.temperature,
            "num_ctx": settings.generation.num_ctx,
            "seed": settings.generation.seed,
        },
        memories_enabled=settings.memory.enabled,
        translation_enabled=settings.translation.enabled,
        target_language=settings.translation.target_language,
        user_system_instructions=settings.generation.system_instructions or None,
    )


def _chat_response(chat: Mapping[str, Any]) -> ChatResponse:
    normalized = dict(chat)
    normalized["messages"] = [
        {
            "role": message.get("role"),
            "content": message.get("content", ""),
            "sources": message.get("sources"),
            "thoughts": message.get("thoughts"),
        }
        for message in chat.get("messages", [])
    ]
    return ChatResponse.model_validate(normalized)


def _accepted(snapshot: JobSnapshot) -> JobAccepted:
    return JobAccepted(
        job_id=snapshot.job_id, kind=snapshot.kind, status=snapshot.status
    )


def _job_response(snapshot: JobSnapshot) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=snapshot.job_id,
        kind=snapshot.kind,
        thread_id=snapshot.thread_id,
        status=snapshot.status,
        sequence=snapshot.sequence,
        error=snapshot.error,
    )


def _job_status(
    request: Request, job_id: str, principal: SessionPrincipal
) -> JobSnapshot:
    try:
        return request.app.state.jobs.status(job_id, owner=principal.session_id)
    except (JobNotFound, JobOwnershipError) as exc:
        _raise_job_error(exc)
        raise AssertionError("unreachable")


def _raise_job_error(exc: Exception) -> None:
    if isinstance(exc, JobNotFound):
        raise HTTPException(status_code=404, detail="Job not found.") from exc
    raise HTTPException(
        status_code=403, detail="Job does not belong to this session."
    ) from exc


def _raise_repository_error(operation: str, exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    logging.error("Cortex API %s failed (%s).", operation, type(exc).__name__)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Could not {operation}.",
    ) from exc
