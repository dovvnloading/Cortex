"""Versioned resource and job routes for the local Cortex API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
import asyncio
import hmac
import json
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from cortex_backend.core.generation import ConnectionResult, GenerationSnapshot
from cortex_backend.services.chat import (
    ChatDomainError,
    chat_revision,
    message_position,
    normalize_title,
    title_from_first_message,
)
from cortex_backend.core.settings import CortexSettings
from cortex_backend.repositories.chats import ChatRepositoryError
from cortex_backend.repositories.settings import SettingsMigrationReport
from cortex_backend.services.models import ModelPullProgress
from cortex_backend.execution.coordinator import DurableFakeCoordinator
from cortex_backend.execution.fake import FakeExecutionPlan
from cortex_backend.execution.models import ExecutionJob, ExecutionEvent, TerminalExecutionStatus
from cortex_backend.execution.repository import (
    ApprovalPolicyError,
    ApprovalTransitionError,
    ExecutionRepositoryError,
)

from .app_types import BackendDependenciesProtocol
from .jobs import JobConflict, JobNotFound, JobOwnershipError, JobSnapshot
from .schemas import (
    AddMemoryRequest,
    AddMessageRequest,
    ChatResponse,
    ChatSummary,
    ClearMemoryRequest,
    CreateChatRequest,
    DiagnosticsResponse,
    ExecutionAccepted,
    ExecutionApprovalDecisionRequest,
    ExecutionPreviewRequest,
    ExecutionSSEEvent,
    ExecutionStatusResponse,
    ExecutionTaskListResponse,
    ExecutionTaskSummary,
    ForkRequest,
    GenerationEvent,
    GenerationRequest,
    HandoffResponse,
    RegenerationRequest,
    HealthResponse,
    JobAccepted,
    JobStatusResponse,
    MemoryResponse,
    ModelPullRequest,
    ModelResponse,
    InstalledModel,
    RenameChatRequest,
    ReplaceMemoryRequest,
    SessionExchangeRequest,
    SessionExchangeResponse,
    ShutdownResponse,
    SettingsResponse,
    SettingsMigrationReport as SettingsMigrationReportResponse,
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

    @router.get("/health/live", response_model=HealthResponse)
    def health_live(request: Request) -> HealthResponse:
        request.app.state.session_manager.validate_request_context(request)
        return HealthResponse()

    @router.get("/health/ready", response_model=HealthResponse)
    def health_ready(request: Request) -> HealthResponse:
        request.app.state.session_manager.validate_request_context(request)
        if not _runtime_is_ready(request):
            raise HTTPException(status_code=503, detail="Cortex is not ready.")
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

    @router.post("/session/handoff", response_model=HandoffResponse)
    def handoff(request: Request) -> HandoffResponse:
        manager = request.app.state.session_manager
        manager.validate_request_context(request)
        supplied = request.headers.get("X-Cortex-Handoff", "")
        expected = request.app.state.handoff_secret
        if not expected or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="Cortex handoff unavailable.")
        token, expires_at = manager.issue_bootstrap_token()
        return HandoffResponse(bootstrap_token=token, expires_at=expires_at)

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
            execution_preview_available=(
                request.app.state.preview
                and request.app.state.execution_coordinator is not None
            ),
            started_at=request.app.state.started_at,
            ollama_host=request.app.state.ollama_host,
            ollama_setup_url=request.app.state.ollama_setup_url,
        )

    @router.post("/system/shutdown", response_model=ShutdownResponse)
    def shutdown(
        request: Request,
        _: SessionPrincipal = Depends(require_session),
    ) -> ShutdownResponse:
        callback = request.app.state.shutdown_callback
        if callback is None:
            raise HTTPException(status_code=409, detail="Shutdown is unavailable in this preview.")
        request.app.state.shutting_down = True
        callback()
        return ShutdownResponse()

    @router.get("/diagnostics", response_model=DiagnosticsResponse)
    def diagnostics(
        request: Request,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> DiagnosticsResponse:
        settings = _load_settings_result(deps)
        required, optional = _model_sets(settings.settings)
        inventory, connection = deps.models.inventory()
        installed = tuple(model.name for model in inventory)
        return DiagnosticsResponse(
            settings_source=settings.source,
            invalid_settings_keys=settings.invalid_keys,
            migration=_migration_response(settings.migration),
            installed_models=installed,
            required_models=required,
            optional_models=optional,
            connection=connection,
            ollama_host=request.app.state.ollama_host,
            ollama_setup_url=request.app.state.ollama_setup_url,
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
            present_keys=loaded.present_keys,
            invalid_keys=loaded.invalid_keys,
            migration=_migration_response(loaded.migration),
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
            present_keys=loaded.present_keys,
            invalid_keys=loaded.invalid_keys,
            migration=_migration_response(loaded.migration),
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
        if payload.confirmation_intent not in (None, "clear_permanent_memory"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A clear-memory confirmation intent is required.",
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
        inventory, connection = deps.models.inventory()
        installed = tuple(model.name for model in inventory)
        return _model_response(
            required,
            optional,
            installed,
            models=inventory,
            connection=connection,
        )

    @router.post(
        "/execution/preview/fake",
        response_model=ExecutionAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_fake_execution(
        request: Request,
        payload: ExecutionPreviewRequest,
        principal: SessionPrincipal = Depends(require_session),
    ) -> ExecutionAccepted:
        coordinator = _execution_coordinator(request)
        try:
            job = coordinator.start(
                owner=principal.session_id,
                request_id=payload.request_id,
                plan=FakeExecutionPlan(
                    outcome=payload.outcome,
                    steps=payload.steps,
                    step_delay_seconds=payload.step_delay_seconds,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ExecutionAccepted(
            job_id=job.job_id,
            request_id=job.request_id,
            profile="fake.v1",
            status=job.status,
            sequence=job.sequence,
        )

    @router.get("/execution/tasks", response_model=ExecutionTaskListResponse)
    def execution_tasks(
        request: Request,
        include_terminal: bool = False,
        limit: int = 50,
        principal: SessionPrincipal = Depends(require_session),
    ) -> ExecutionTaskListResponse:
        repository = _execution_repository(request)
        try:
            jobs = repository.list_jobs(
                owner=principal.session_id,
                include_terminal=include_terminal,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ExecutionTaskListResponse(
            tasks=[_execution_task_summary(repository, job) for job in jobs]
        )

    @router.get("/execution/{job_id}", response_model=ExecutionStatusResponse)
    def execution_status(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> ExecutionStatusResponse:
        repository = _execution_repository(request)
        job = repository.get_job(job_id, owner=principal.session_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Execution job not found.")
        return _execution_status_response(repository, job)

    @router.post(
        "/execution/{job_id}/approval", response_model=ExecutionStatusResponse
    )
    def decide_execution_approval(
        job_id: str,
        payload: ExecutionApprovalDecisionRequest,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> ExecutionStatusResponse:
        repository = _execution_repository(request)
        try:
            repository.decide_approval(
                job_id,
                owner=principal.session_id,
                decision=payload.decision,
            )
        except (ApprovalPolicyError, ApprovalTransitionError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except ExecutionRepositoryError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Execution job not found.",
            ) from exc
        job = repository.get_job(job_id, owner=principal.session_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Execution job not found.")
        return _execution_status_response(repository, job)

    @router.post(
        "/execution/{job_id}/cancel", response_model=ExecutionStatusResponse
    )
    def cancel_execution(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> ExecutionStatusResponse:
        coordinator = _execution_coordinator(request)
        try:
            coordinator.cancel(job_id, owner=principal.session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Execution job not found.") from exc
        job = coordinator.repository.get_job(job_id, owner=principal.session_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Execution job not found.")
        return _execution_status_response(coordinator.repository, job)

    @router.get(
        "/execution/{job_id}/events",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Server-sent execution events.",
                "content": {"text/event-stream": {}},
            }
        },
    )
    async def execution_events(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> StreamingResponse:
        repository = _execution_repository(request)
        if repository.get_job(job_id, owner=principal.session_id) is None:
            raise HTTPException(status_code=404, detail="Execution job not found.")
        cursor = _last_event_cursor(request)

        async def stream():
            next_sequence = cursor
            idle_rounds = 0
            while True:
                events = repository.events(job_id, after_sequence=next_sequence)
                if events:
                    idle_rounds = 0
                    for event in events:
                        next_sequence = event.sequence
                        yield _execution_sse_line(event)
                    current = repository.get_job(job_id, owner=principal.session_id)
                    if current is not None and current.status in TerminalExecutionStatus:
                        return
                else:
                    idle_rounds += 1
                    current = repository.get_job(job_id, owner=principal.session_id)
                    if current is None:
                        return
                    if current.status in TerminalExecutionStatus:
                        return
                    if idle_rounds >= 600:
                        return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.01)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post(
        "/models/pulls",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def pull_model(
        payload: ModelPullRequest,
        request: Request,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobAccepted:
        model = payload.model.strip()

        def runner(sink, cancel_event):
            sink.publish_progress(
                "model_check",
                "Preparing the exact model tag.",
                data={"model": model},
            )

            def publish(update: ModelPullProgress) -> None:
                sink.publish_progress(
                    "model_pull",
                    update.status,
                    data={
                        "model": update.model,
                        "completed": update.completed,
                        "total": update.total,
                        "percent": update.percent,
                        "digest": update.digest,
                    },
                )

            pulled = deps.models.pull_model(
                model,
                progress_callback=publish,
                cancellation_event=cancel_event,
            )
            if not pulled:
                return {"cancelled": True, "model": model}
            return {"model": model, "installed_models": deps.models.list_installed()}

        try:
            snapshot = await request.app.state.jobs.start(
                kind="models",
                owner=principal.session_id,
                thread_id=None,
                runner=runner,
            )
        except JobConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _accepted(snapshot)

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
            sink.publish_progress("model_check", "Scanning local Ollama models.")
            connection = deps.models.check(
                required_models=required,
                optional_models=optional,
                progress_callback=lambda update: sink.publish_progress(
                    "model_pull",
                    update.status,
                    data={
                        "model": update.model,
                        "completed": update.completed,
                        "total": update.total,
                        "percent": update.percent,
                        "digest": update.digest,
                    },
                ),
                cancellation_event=cancel_event,
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
        "/generations",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_generation(
        payload: GenerationRequest,
        request: Request,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobAccepted:
        try:
            snapshot, user_message_id = await _start_generation_job(
                request,
                deps,
                principal,
                payload,
            )
        except JobConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        except ChatDomainError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        return _accepted(snapshot, user_message_id=user_message_id)

    @router.get("/generations/{job_id}", response_model=JobStatusResponse)
    def generation_status(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobStatusResponse:
        return _job_response(_job_status(request, job_id, principal))

    @router.post(
        "/generations/{job_id}/cancel", response_model=JobStatusResponse
    )
    def cancel_generation(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobStatusResponse:
        try:
            snapshot = request.app.state.jobs.cancel(job_id, owner=principal.session_id)
        except (JobNotFound, JobOwnershipError) as exc:
            _raise_job_error(exc)
        return _job_response(snapshot)

    @router.get("/generations/{job_id}/events", response_model=GenerationEvent)
    async def generation_events(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> StreamingResponse:
        cursor = _event_cursor(request)
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
                event_name = _generation_event_name(event.kind, event.status, event.phase)
                payload = GenerationEvent(
                    event_id=event.sequence,
                    event=event_name,
                    job_id=event.job_id,
                    thread_id=event.thread_id or "",
                    timestamp=datetime.now(timezone.utc),
                    data=dict(event.data),
                ).model_dump(mode="json")
                yield (
                    f"id: {event.sequence}\n"
                    f"event: {event_name}\n"
                    f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post(
        "/chats/{thread_id}/forks",
        response_model=ChatResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def fork_chat(
        thread_id: str,
        payload: ForkRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatResponse:
        try:
            source = deps.chats.get_chat(thread_id)
            if source is None:
                raise HTTPException(status_code=404, detail="Chat not found.")
            message_position(source, payload.message_id)
            new_thread_id = uuid4().hex
            deps.chats.fork_chat(thread_id, payload.message_id, new_thread_id)
            forked = deps.chats.get_chat(new_thread_id)
        except HTTPException:
            raise
        except (ChatDomainError, ChatRepositoryError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            _raise_repository_error("fork chat", exc)
        if forked is None:
            raise HTTPException(status_code=500, detail="Chat fork did not persist.")
        return _chat_response(forked)

    @router.post(
        "/chats/{thread_id}/regenerations",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def regenerate_chat(
        thread_id: str,
        payload: RegenerationRequest,
        request: Request,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobAccepted:
        try:
            chat = deps.chats.get_chat(thread_id)
            if chat is None:
                raise HTTPException(status_code=404, detail="Chat not found.")
            position = message_position(chat, payload.message_id)
            messages = list(chat.get("messages", ()))
            if position != len(messages) - 1 or messages[position].get("role") != "assistant":
                raise ChatDomainError("Only the final assistant response can be regenerated.")
            if position == 0 or messages[position - 1].get("role") != "user":
                raise ChatDomainError("The selected response has no user turn to regenerate.")
            user_input = (payload.user_input or messages[position - 1].get("content", "")).strip()
            if not user_input:
                raise ChatDomainError("A regeneration request needs user input.")
            generation_payload = GenerationRequest(
                request_id=payload.request_id,
                thread_id=thread_id,
                user_input=user_input,
                base_revision=chat_revision(chat),
            )
            snapshot, _ = await _start_generation_job(
                request,
                deps,
                principal,
                generation_payload,
                target_message_id=payload.message_id,
                history_messages=messages[:position],
            )
        except HTTPException:
            raise
        except JobConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ChatDomainError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
        try:
            settings = _load_settings(deps)
            job_id = uuid4().hex
            generation_snapshot = _generation_snapshot(
                job_id,
                payload,
                settings,
                deps.models.list_installed(),
            )
        except ChatDomainError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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


def _runtime_is_ready(request: Request) -> bool:
    app = request.app
    if not app.state.ready or app.state.shutting_down:
        return False
    if any(not path.exists() for path in tuple(app.state.required_paths)):
        return False
    if app.state.serve_frontend and not (app.state.frontend_dist / "index.html").is_file():
        return False
    route_paths = set(app.openapi().get("paths", {}))
    if not {
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/api/v1/system",
    }.issubset(route_paths):
        return False
    readiness_check = app.state.readiness_check
    if readiness_check is None:
        return True
    try:
        return bool(readiness_check())
    except Exception as exc:  # keep readiness safe without exposing internals
        logging.getLogger("cortex.readiness").warning(
            "Cortex readiness check failed (%s).", type(exc).__name__
        )
        return False


async def _start_generation_job(
    request: Request,
    deps: BackendDependenciesProtocol,
    principal: SessionPrincipal,
    payload: GenerationRequest,
    *,
    target_message_id: str | None = None,
    history_messages: list[Mapping[str, Any]] | None = None,
) -> tuple[JobSnapshot, str | None]:
    """Persist the user turn, then run one authoritative generation job."""
    jobs = request.app.state.jobs
    existing = jobs.request_snapshot(
        kind="generation",
        owner=principal.session_id,
        request_id=payload.request_id,
    )
    if existing is not None:
        return existing, None
    if jobs.active_snapshot(kind="generation") is not None:
        raise JobConflict("A generation job is already active.")

    thread_id = payload.thread_id or uuid4().hex
    chat = deps.chats.get_chat(thread_id)
    if payload.base_revision is not None and chat is not None:
        if chat_revision(chat) != payload.base_revision:
            raise ChatDomainError("This chat changed. Reload it before generating again.")

    settings = _load_settings(deps)
    job_id = uuid4().hex
    generation_payload = payload.model_copy(update={"thread_id": thread_id})
    generation_snapshot = _generation_snapshot(
        job_id,
        generation_payload,
        settings,
        deps.models.list_installed(),
    )

    user_message_id: str | None = None
    if target_message_id is None:
        user_message_id = deps.chats.add_message(
            thread_id,
            "user",
            payload.user_input,
            thread_title="New Chat" if chat is None else None,
        )

    def runner(sink, cancel_event):
        result = deps.generation.generate(
            generation_snapshot,
            progress_sink=sink,
            cancellation_event=cancel_event,
            history_messages=history_messages,
        )
        # The generation service checks cancellation around its model work,
        # but the API owns the following persistence and optional title work.
        # Keep those side effects behind explicit checkpoints as well.
        if cancel_event.is_set():
            return {"cancelled": True}
        if result.thoughts:
            for delta in _chunks(result.thoughts):
                if cancel_event.is_set():
                    return {"cancelled": True}
                sink.publish_progress(
                    "thinking_delta",
                    "Reasoning available.",
                    data={"delta": delta},
                )
        for delta in _chunks(result.response):
            if cancel_event.is_set():
                return {"cancelled": True}
            sink.publish_progress(
                "content_delta",
                "Response content available.",
                data={"delta": delta},
            )

        for memo in result.memory_command.additions:
            if cancel_event.is_set():
                return {"cancelled": True}
            deps.memories.add_memo(memo)
        if cancel_event.is_set():
            return {"cancelled": True}
        sink.publish_progress("persisting", "Saving the response.")
        if cancel_event.is_set():
            return {"cancelled": True}
        if target_message_id is None:
            assistant_message_id = deps.chats.add_message(
                thread_id,
                "assistant",
                result.response,
                thoughts=result.thoughts,
            )
        else:
            deps.chats.replace_message(
                thread_id,
                target_message_id,
                result.response,
                thoughts=result.thoughts,
            )
            assistant_message_id = target_message_id

        if cancel_event.is_set():
            return {"cancelled": True}
        updated_chat = deps.chats.get_chat(thread_id) or {"messages": []}
        title = str(updated_chat.get("title") or "New Chat")
        if target_message_id is None and title == "New Chat":
            if cancel_event.is_set():
                return {"cancelled": True}
            raw_title = None
            title_generator = getattr(deps.generation, "generate_chat_title", None)
            if callable(title_generator):
                try:
                    raw_title = title_generator(generation_snapshot, result.response)
                except Exception as exc:  # optional title work must not fail a chat
                    logging.warning(
                        "Cortex chat title generation failed (%s).",
                        type(exc).__name__,
                    )
            if cancel_event.is_set():
                return {"cancelled": True}
            generated_title = normalize_title(raw_title, fallback="")
            if (
                not generated_title
                or generated_title.casefold() in {"new chat", "untitled chat"}
            ):
                generated_title = title_from_first_message(payload.user_input)
            if generated_title != title:
                if cancel_event.is_set():
                    return {"cancelled": True}
                try:
                    deps.chats.rename_chat(thread_id, generated_title)
                    title = generated_title
                except Exception as exc:
                    logging.warning(
                        "Cortex title update failed (%s).", type(exc).__name__
                    )
        if cancel_event.is_set():
            return {"cancelled": True}
        updated_chat = deps.chats.get_chat(thread_id) or updated_chat
        return {
            "thread_id": thread_id,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "chat_revision": chat_revision(updated_chat),
            "title": str(updated_chat.get("title") or title),
            "response": result.response,
            "thoughts": result.thoughts,
            "clear_requested": result.memory_command.clear_requested,
        }

    snapshot = await jobs.start(
        kind="generation",
        owner=principal.session_id,
        thread_id=thread_id,
        runner=runner,
        request_id=payload.request_id,
    )
    return snapshot, user_message_id


def _chunks(value: str, size: int = 80):
    for start in range(0, len(value), size):
        yield value[start : start + size]


def _event_cursor(request: Request) -> int:
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
    return cursor


def _generation_event_name(kind: str, job_status: str, phase: str | None) -> str:
    if kind == "completed":
        return "generation.completed"
    if kind == "error":
        return "generation.failed"
    if kind == "state" and job_status == "cancelling":
        return "generation.cancelling"
    if kind == "state" and job_status == "cancelled":
        return "generation.cancelled"
    if kind == "state" and job_status == "queued":
        return "generation.queued"
    if kind == "state":
        return "generation.started"
    return {
        "thinking_delta": "generation.thinking_delta",
        "content_delta": "generation.content_delta",
        "translation": "generation.translation_started",
        "persisting": "generation.persisting",
    }.get(phase or "", "generation.status")


def _load_settings(deps: BackendDependenciesProtocol) -> CortexSettings:
    return _load_settings_result(deps).settings


def _load_settings_result(deps: BackendDependenciesProtocol):
    try:
        return deps.settings.load()
    except Exception as exc:
        _raise_repository_error("load settings", exc)
        raise AssertionError("unreachable")


def _migration_response(report: SettingsMigrationReport | None):
    if report is None:
        return None
    return SettingsMigrationReportResponse(
        status=report.status,
        source=report.source,
        migration_key=report.migration_key,
        imported_keys=report.imported_keys,
        invalid_keys=report.invalid_keys,
        backup_path=report.backup_path,
        message=report.message,
    )


def _model_sets(settings: CortexSettings) -> tuple[tuple[str, ...], tuple[str, ...]]:
    # Model selection is driven by the local Ollama inventory, not a bundled
    # list of model tags. A selected chat model is used directly at generation
    # time, while translation remains the only opt-in optional dependency.
    required: tuple[str, ...] = ()
    optional: list[str] = []
    if settings.translation.enabled:
        optional.append(settings.models.translation)
    return required, tuple(
        model for model in dict.fromkeys(optional) if model not in required
    )


def _model_response(
    required: tuple[str, ...],
    optional: tuple[str, ...],
    installed: tuple[str, ...],
    connection: ConnectionResult | None = None,
    models=(),
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
        models=tuple(
            InstalledModel(
                name=model.name,
                size=model.size,
                modified_at=model.modified_at,
            )
            for model in models
        ),
    )


def _generation_snapshot(
    job_id: str,
    payload: GenerationRequest,
    settings: CortexSettings,
    installed_models: tuple[str, ...],
) -> GenerationSnapshot:
    chat_model = _selected_local_model(settings.models.chat, installed_models)
    if chat_model is None:
        raise ChatDomainError(
            "No local Ollama model is available. Install one, then rescan Models in Settings."
        )
    # Titles intentionally share the selected chat model. This keeps model
    # selection to one local, user-visible choice and avoids hidden defaults.
    title_model = chat_model
    if (
        settings.translation.enabled
        and settings.models.translation not in installed_models
    ):
        raise ChatDomainError(
            "Choose or install a local translation model before enabling translation."
        )
    return GenerationSnapshot(
        job_id=job_id,
        thread_id=payload.thread_id or "",
        user_input=payload.user_input,
        model=chat_model,
        title_model=title_model,
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


def _selected_local_model(
    configured_model: str | None,
    installed_models: tuple[str, ...],
) -> str | None:
    """Use a saved local model when present, otherwise the live inventory."""
    if configured_model and configured_model in installed_models:
        return configured_model
    return installed_models[0] if installed_models else None


def _chat_response(chat: Mapping[str, Any]) -> ChatResponse:
    normalized = dict(chat)
    normalized["revision"] = chat_revision(chat)
    normalized["messages"] = [
        {
            "id": str(message.get("id")) if message.get("id") is not None else None,
            "role": message.get("role"),
            "content": message.get("content", ""),
            "timestamp": message.get("timestamp"),
            "sources": message.get("sources"),
            "thoughts": message.get("thoughts"),
        }
        for message in chat.get("messages", [])
    ]
    return ChatResponse.model_validate(normalized)


def _accepted(
    snapshot: JobSnapshot,
    *,
    user_message_id: str | None = None,
) -> JobAccepted:
    return JobAccepted(
        job_id=snapshot.job_id,
        kind=snapshot.kind,
        status=snapshot.status,
        thread_id=snapshot.thread_id,
        user_message_id=user_message_id,
    )


def _job_response(snapshot: JobSnapshot) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=snapshot.job_id,
        kind=snapshot.kind,
        thread_id=snapshot.thread_id,
        status=snapshot.status,
        sequence=snapshot.sequence,
        error=snapshot.error,
        result=dict(snapshot.result) if snapshot.result is not None else None,
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


def _execution_coordinator(request: Request) -> DurableFakeCoordinator:
    """Require the explicitly injected fake-only preview coordinator."""
    coordinator = getattr(request.app.state, "execution_coordinator", None)
    if not request.app.state.preview or coordinator is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution preview is unavailable.",
        )
    return coordinator


def _execution_repository(request: Request):
    return _execution_coordinator(request).repository


def _execution_latest_event(
    repository, job: ExecutionJob
) -> ExecutionEvent | None:
    events = repository.events(job.job_id, after_sequence=max(0, job.sequence - 1))
    return events[-1] if events else None


def _execution_message(event: ExecutionEvent | None) -> str | None:
    if event is None:
        return None
    message = event.data.get("message")
    return message if isinstance(message, str) else None


def _execution_status_response(repository, job: ExecutionJob) -> ExecutionStatusResponse:
    event = _execution_latest_event(repository, job)
    approval = repository.get_approval(job.job_id, owner=job.owner)
    approval_state = approval.state if approval is not None else job.approval_state
    return ExecutionStatusResponse(
        job_id=job.job_id,
        request_id=job.request_id,
        profile=job.profile,
        status=job.status,
        sequence=job.sequence,
        phase=event.phase if event else None,
        message=_execution_message(event),
        approval_state=approval_state,
        approval_reason=approval.reason if approval is not None else None,
        approval_expires_at=(
            datetime.fromisoformat(approval.expires_at)
            if approval is not None and approval.expires_at is not None
            else None
        ),
        can_cancel=(
            job.status in {"queued", "running", "cancelling"}
            and approval_state in {"not_required", "approved"}
        ),
        error=job.error,
        result=dict(job.result) if job.result is not None else None,
    )


def _execution_task_summary(repository, job: ExecutionJob) -> ExecutionTaskSummary:
    response = _execution_status_response(repository, job)
    return ExecutionTaskSummary(
        job_id=response.job_id,
        profile=response.profile,
        status=response.status,
        sequence=response.sequence,
        phase=response.phase,
        message=response.message,
        approval_state=response.approval_state,
        approval_reason=response.approval_reason,
        approval_expires_at=response.approval_expires_at,
        can_cancel=response.can_cancel,
        created_at=datetime.fromisoformat(job.created_at),
        updated_at=datetime.fromisoformat(job.updated_at),
    )


def _last_event_cursor(request: Request) -> int:
    raw = request.headers.get("last-event-id", "0")
    try:
        cursor = int(raw or "0")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer.") from exc
    if cursor < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be non-negative.")
    return cursor


def _execution_sse_line(event: ExecutionEvent) -> str:
    payload = ExecutionSSEEvent(
        id=event.sequence,
        sequence=event.sequence,
        job_id=event.job_id,
        event=f"execution.{event.event}",
        status=event.status,
        phase=event.phase,
        data=dict(event.data),
    ).model_dump(mode="json")
    return (
        f"id: {event.sequence}\n"
        f"event: execution.{event.event}\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    )
