"""Versioned resource and job routes for the local Cortex API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

from cortex_backend.core.generation import ConnectionResult, GenerationAttachment, GenerationSnapshot
from cortex_backend.services.chat import (
    ChatDomainError,
    chat_revision,
    message_position,
    normalize_title,
    title_from_first_message,
)
from cortex_backend.services.code_prompt import should_offer_code_execution
from cortex_backend.core.settings import (
    CortexSettings,
    GENERATION_OVERRIDE_FIELDS,
    GenerationOptionsOverride,
)
from cortex_backend.repositories.chats import (
    ChatGroupNotFound,
    ChatRepositoryError,
    ChatRevisionConflict,
)
from cortex_backend.repositories.settings import SettingsMigrationReport
from cortex_backend.services.models import ModelPullProgress
from cortex_backend.services.attachments import (
    ChatAttachmentError,
    ChatAttachmentService,
    MAX_CHAT_ATTACHMENTS,
    MAX_CHAT_ATTACHMENT_TOTAL_BYTES,
)
from cortex_backend.execution.coordinator import DurableFakeCoordinator
from cortex_backend.execution.fake import FakeExecutionPlan
from cortex_backend.execution.attachment_staging import (
    AttachmentStagingError,
    AttachmentStagingService,
)
from cortex_backend.execution.artifact_boundary import ArtifactBoundary
from cortex_backend.execution.code_execution import (
    CODE_EXECUTION_PROFILE,
    CodeCapabilities,
    CodeExecutionError,
    CodeExecutionRequest as CodeExecutionTaskRequest,
)
from cortex_backend.execution.models import ExecutionJob, ExecutionEvent, TerminalExecutionStatus
from cortex_backend.execution.recipe_coordinator import (
    RECIPE_IMAGE_PROFILE,
    RecipeExecutionError,
    RecipeImageRequest,
)
from cortex_backend.execution.scratch_compute import (
    ScratchComputeError,
    ScratchComputeRequest as ScratchExecutionRequest,
    extract_automatic_expression,
)
from cortex_backend.execution.repository import (
    ApprovalPolicyError,
    ApprovalTransitionError,
    ExecutionRepositoryError,
)
from cortex_backend.llamacpp.download import (
    DownloadSource,
    GGUFDownloadError,
    download_gguf,
    list_huggingface_gguf_files,
    resolve_download_url,
)

from .app_types import BackendDependenciesProtocol
from .jobs import (
    JobConflict,
    JobNotFound,
    JobOwnershipError,
    JobReservation,
    JobRegistryClosed,
    JobSnapshot,
)
from .schemas import (
    AddMemoryRequest,
    AddMessageRequest,
    ChatGroup,
    ChatResponse,
    ChatSummary,
    ClearMemoryRequest,
    CreateChatGroupRequest,
    CreateChatRequest,
    DiagnosticsResponse,
    AttachmentStageAccepted,
    AttachmentStageRequest,
    ChatAttachment,
    ChatAttachmentStageRequest,
    CodeCapabilitiesRequest,
    CodeExecutionAccepted,
    CodeExecutionRequest,
    CodeExecutionSourceResponse,
    ExecutionAccepted,
    ExecutionApprovalDecisionRequest,
    ExecutionPreviewRequest,
    RecipeImageTransformAccepted,
    RecipeImageTransformRequest,
    ScratchComputeAccepted,
    ScratchComputeRequest,
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
    HuggingFaceFileListResponse,
    JobAccepted,
    JobStatusResponse,
    LlamaCppRuntimeStatus,
    MemoryResponse,
    ModelDownloadRequest,
    ModelPullRequest,
    ModelResponse,
    InstalledModel,
    MoveChatToGroupRequest,
    RenameChatRequest,
    ReplaceMemoryRequest,
    SessionExchangeRequest,
    SessionExchangeResponse,
    UpdateChatGroupRequest,
    ShutdownResponse,
    SettingsResponse,
    SettingsMigrationReport as SettingsMigrationReportResponse,
    SettingsUpdateRequest,
    SSEEvent,
    SystemResponse,
)
from .security import SessionPrincipal, SessionSecurityError


DEFAULT_AUTOMATIC_COMPUTE_WAIT_SECONDS = 1.5


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
        coordinator = request.app.state.execution_coordinator
        return SystemResponse(
            preview=request.app.state.preview,
            execution_preview_available=(
                request.app.state.preview
                and coordinator is not None
            ),
            scratch_compute_available=bool(
                request.app.state.preview
                and getattr(
                    coordinator,
                    "scratch_available",
                    False,
                )
            ),
            code_execution_available=bool(
                request.app.state.preview
                and coordinator is not None
                and getattr(coordinator, "code_execution_available", False)
            ),
            image_transform_available=bool(
                request.app.state.preview
                and coordinator is not None
                and getattr(
                    coordinator,
                    "image_transform_available",
                    callable(getattr(coordinator, "start_image_transform", None)),
                )
            ),
            started_at=request.app.state.started_at,
            ollama_host=request.app.state.ollama_host,
            ollama_setup_url=request.app.state.ollama_setup_url,
            llamacpp=_llamacpp_status(request),
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
            llamacpp=_llamacpp_status(request),
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

    @router.get("/chat-groups", response_model=list[ChatGroup])
    def list_chat_groups(
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> list[ChatGroup]:
        try:
            return [ChatGroup.model_validate(item) for item in deps.chats.list_groups()]
        except Exception as exc:
            _raise_repository_error("list chat groups", exc)

    @router.post(
        "/chat-groups", response_model=ChatGroup, status_code=status.HTTP_201_CREATED
    )
    def create_chat_group(
        payload: CreateChatGroupRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatGroup:
        group_id = uuid4().hex
        try:
            deps.chats.create_group(group_id, payload.name.strip())
            created = next(
                (item for item in deps.chats.list_groups() if item["id"] == group_id),
                None,
            )
        except Exception as exc:
            _raise_repository_error("create chat group", exc)
        if created is None:
            raise HTTPException(
                status_code=500, detail="Chat group creation did not persist."
            )
        return ChatGroup.model_validate(created)

    @router.patch("/chat-groups/{group_id}", response_model=ChatGroup)
    def update_chat_group(
        group_id: str,
        payload: UpdateChatGroupRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatGroup:
        try:
            updated = deps.chats.update_group(
                group_id,
                name=payload.name.strip() if payload.name is not None else None,
                collapsed=payload.collapsed,
            )
            if not updated:
                raise HTTPException(status_code=404, detail="Chat group not found.")
            group = next(
                (item for item in deps.chats.list_groups() if item["id"] == group_id),
                None,
            )
        except HTTPException:
            raise
        except Exception as exc:
            _raise_repository_error("update chat group", exc)
        if group is None:
            raise HTTPException(status_code=404, detail="Chat group not found.")
        return ChatGroup.model_validate(group)

    @router.delete("/chat-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_chat_group(
        group_id: str,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> None:
        """Delete the group only. Its chats return to the ungrouped list --
        tidying the sidebar must never destroy conversations."""
        try:
            deps.chats.delete_group(group_id)
        except Exception as exc:
            _raise_repository_error("delete chat group", exc)

    @router.patch("/chats/{thread_id}/group", response_model=ChatSummary)
    def move_chat_to_group(
        thread_id: str,
        payload: MoveChatToGroupRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatSummary:
        try:
            moved = deps.chats.set_chat_group(thread_id, payload.group_id)
            if not moved:
                raise HTTPException(status_code=404, detail="Chat not found.")
            summary = next(
                (item for item in deps.chats.list_summaries() if item["id"] == thread_id),
                None,
            )
        except HTTPException:
            raise
        except ChatGroupNotFound as exc:
            raise HTTPException(status_code=404, detail="Chat group not found.") from exc
        except Exception as exc:
            _raise_repository_error("move chat", exc)
        if summary is None:
            raise HTTPException(status_code=404, detail="Chat not found.")
        return ChatSummary.model_validate(summary)

    @router.post("/chats/{thread_id}/messages", response_model=ChatResponse)
    def add_message(
        thread_id: str,
        payload: AddMessageRequest,
        request: Request,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        principal: SessionPrincipal = Depends(require_session),
    ) -> ChatResponse:
        try:
            existing = deps.chats.get_chat(thread_id)
            attachment_refs = _validate_chat_attachment_refs(
                request,
                deps,
                principal,
                payload.attachments or [],
            )
            deps.chats.add_message(
                thread_id,
                payload.role,
                payload.content,
                sources=payload.sources,
                thoughts=payload.thoughts,
                attachments=[attachment.model_dump(mode="json") for attachment in attachment_refs],
                thread_title="New Chat" if existing is None else None,
                expected_revision=payload.base_revision,
            )
            chat = deps.chats.get_chat(thread_id)
        except ChatAttachmentError as exc:
            _raise_chat_attachment_error(exc)
        except ChatRevisionConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ChatDomainError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
        coordinator = _fake_execution_coordinator(request)
        try:
            job = coordinator.start(
                owner=_execution_owner(principal),
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

    @router.post(
        "/execution/scratch",
        response_model=ScratchComputeAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_scratch_compute(
        request: Request,
        payload: ScratchComputeRequest,
        principal: SessionPrincipal = Depends(require_session),
    ) -> ScratchComputeAccepted:
        """Start one bounded calculation in the local worker profile."""

        coordinator = _scratch_coordinator(request)
        try:
            job = coordinator.start_scratch(
                ScratchExecutionRequest(
                    owner=_execution_owner(principal),
                    request_id=payload.request_id,
                    expression=payload.expression,
                )
            )
        except ScratchComputeError as exc:
            _raise_scratch_request_error(exc)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Safe computation request is invalid.",
            ) from exc
        return ScratchComputeAccepted(
            job_id=job.job_id,
            request_id=job.request_id,
            profile="scratch.auto.v1",
            status=job.status,
            sequence=job.sequence,
        )

    @router.post(
        "/execution/code",
        response_model=CodeExecutionAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_code_execution(
        request: Request,
        payload: CodeExecutionRequest,
        principal: SessionPrincipal = Depends(require_session),
    ) -> CodeExecutionAccepted:
        """Queue one local Python run and pause until the user approves it."""

        current_settings = _load_settings(dependencies(request))
        if not current_settings.execution.code_execution_enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Local code execution is disabled in Settings.",
            )
        coordinator = _code_coordinator(request)
        try:
            job = coordinator.start_code(
                CodeExecutionTaskRequest(
                    owner=_execution_owner(principal),
                    request_id=payload.request_id,
                    source=payload.source,
                    intent_summary=payload.intent_summary,
                    capabilities=payload.capabilities.to_runtime(),
                )
            )
        except CodeExecutionError as exc:
            detail = {
                "request_conflict": "Code request conflicts with an existing request.",
                "source_too_large": "Code is too large to run locally.",
                "syntax_invalid": "The code could not be parsed safely.",
                "syntax_not_allowed": "That code uses an unsupported construct.",
            }.get(exc.code, "Code execution request is invalid.")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT if exc.code == "request_conflict" else status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=detail,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Code execution request is invalid.") from exc
        return CodeExecutionAccepted(
            job_id=job.job_id,
            request_id=job.request_id,
            profile=CODE_EXECUTION_PROFILE,
            status=job.status,
            sequence=job.sequence,
            approval_state=job.approval_state,
            source_digest=str(job.payload.get("source_digest", "")),
            capabilities=payload.capabilities,
        )

    @router.post(
        "/execution/recipe/image",
        response_model=RecipeImageTransformAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_recipe_image_transform(
        request: Request,
        payload: RecipeImageTransformRequest,
        principal: SessionPrincipal = Depends(require_session),
    ) -> RecipeImageTransformAccepted:
        """Start one explicitly qualified, owner-scoped image recipe.

        The route is intentionally unavailable unless the app was built with
        the explicit qualification lifecycle and that lifecycle completed its
        health-gated startup. Attachment staging is a separate trusted boundary;
        callers provide only its opaque artifact identifier here.
        """

        coordinator = _recipe_coordinator(request)
        try:
            job = coordinator.start_image_transform(
                RecipeImageRequest(
                    owner=_execution_owner(principal),
                    request_id=payload.request_id,
                    source_artifact_id=payload.source_artifact_id,
                    plan=payload.plan,
                    retention_seconds=payload.retention_seconds,
                )
            )
        except RecipeExecutionError as exc:
            _raise_recipe_request_error(exc)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Recipe request is invalid.",
            ) from exc
        return RecipeImageTransformAccepted(
            job_id=job.job_id,
            request_id=job.request_id,
            profile=RECIPE_IMAGE_PROFILE,
            status=job.status,
            sequence=job.sequence,
        )

    @router.post(
        "/execution/attachments",
        response_model=AttachmentStageAccepted,
        status_code=status.HTTP_201_CREATED,
    )
    def stage_attachment(
        request: Request,
        payload: AttachmentStageRequest,
        principal: SessionPrincipal = Depends(require_session),
    ) -> AttachmentStageAccepted:
        """Stage one bounded attachment for a qualified recipe request.

        The route accepts only a bounded base64 envelope.  Decoded bytes pass
        through the same owner-scoped artifact boundary used by recipe output;
        no caller path, filename, or executable instruction is accepted.
        """

        coordinator = _recipe_coordinator(request)
        try:
            content = base64.b64decode(payload.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Attachment payload is invalid.",
            ) from None
        try:
            staged = AttachmentStagingService(
                coordinator.repository,
                coordinator.artifact_boundary,
            ).stage(
                owner=_execution_owner(principal),
                request_id=payload.request_id,
                content=content,
                retention_seconds=payload.retention_seconds,
            )
        except AttachmentStagingError as exc:
            _raise_attachment_staging_error(exc)
        artifact = staged.artifact
        return AttachmentStageAccepted(
            job_id=staged.job.job_id,
            request_id=staged.job.request_id,
            profile="attachment.stage.v1",
            status="succeeded",
            sequence=staged.job.sequence,
            artifact_id=artifact.artifact_id,
            mime_type=artifact.mime_type,
            size=artifact.size,
            sha256=artifact.sha256,
            expires_at=datetime.fromisoformat(artifact.expires_at),
        )

    @router.post(
        "/attachments",
        response_model=ChatAttachment,
        status_code=status.HTTP_201_CREATED,
    )
    def stage_chat_attachment(
        payload: ChatAttachmentStageRequest,
        request: Request,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        principal: SessionPrincipal = Depends(require_session),
    ) -> ChatAttachment:
        """Stage one bounded image or text document for a chat turn.

        The browser receives opaque metadata only.  Content is retained in the
        owner-scoped local attachment store and is resolved only when the
        generation job starts.
        """

        try:
            content = base64.b64decode(payload.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Attachment payload is invalid.",
            ) from None
        service = _chat_attachment_service(request, deps)
        try:
            descriptor = service.stage(
                owner=_attachment_owner(request, principal),
                request_id=payload.request_id,
                filename=payload.filename,
                content=content,
            )
        except ChatAttachmentError as exc:
            _raise_chat_attachment_error(exc)
        return ChatAttachment.model_validate(descriptor.as_dict())

    @router.get("/execution/artifacts/{artifact_id}")
    def download_execution_artifact(
        artifact_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Response:
        """Return one owner-scoped, integrity-checked result artifact.

        The UI receives bytes only after the repository re-checks retention,
        location, size, and digest.  Neither the artifact path nor the source
        filename is exposed to the browser.
        """

        repository = _execution_repository(request)
        artifact = repository.get_artifact(
            artifact_id,
            owner=_execution_owner(principal),
        )
        if artifact is None:
            raise HTTPException(status_code=404, detail="Execution artifact is unavailable.")
        try:
            content = repository.read_artifact(artifact.artifact_id)
        except ExecutionRepositoryError:
            raise HTTPException(status_code=404, detail="Execution artifact is unavailable.") from None
        suffix = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
        }.get(artifact.mime_type, "bin")
        return Response(
            content=content,
            media_type=artifact.mime_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="cortex-result.{suffix}"',
            },
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
                owner=_execution_owner(principal),
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
        job = repository.get_job(job_id, owner=_execution_owner(principal))
        if job is None:
            raise HTTPException(status_code=404, detail="Execution job not found.")
        return _execution_status_response(repository, job)

    @router.get(
        "/execution/{job_id}/source",
        response_model=CodeExecutionSourceResponse,
    )
    def execution_source(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> CodeExecutionSourceResponse:
        repository = _execution_repository(request)
        job = repository.get_job(job_id, owner=_execution_owner(principal))
        if job is None or job.profile != CODE_EXECUTION_PROFILE:
            raise HTTPException(status_code=404, detail="Code execution job not found.")
        payload = job.payload
        try:
            capabilities = CodeCapabilitiesRequest.model_validate(payload.get("capabilities", {}))
            source = str(payload["source"])
            digest = str(payload["source_digest"])
            intent = str(payload["intent_summary"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=500, detail="Code source metadata is unavailable.") from None
        return CodeExecutionSourceResponse(
            job_id=job.job_id,
            language="python",
            source=source,
            source_digest=digest,
            intent_summary=intent,
            capabilities=capabilities,
        )

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
                owner=_execution_owner(principal),
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
        job = repository.get_job(job_id, owner=_execution_owner(principal))
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
        coordinator = _execution_runtime(request)
        try:
            coordinator.cancel(job_id, owner=_execution_owner(principal))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Execution job not found.") from exc
        job = coordinator.repository.get_job(job_id, owner=_execution_owner(principal))
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
        if repository.get_job(job_id, owner=_execution_owner(principal)) is None:
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
                    current = repository.get_job(job_id, owner=_execution_owner(principal))
                    if current is not None and current.status in TerminalExecutionStatus:
                        return
                else:
                    idle_rounds += 1
                    current = repository.get_job(job_id, owner=_execution_owner(principal))
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
        except JobRegistryClosed as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
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
            sink.publish_progress("model_check", "Scanning local models.")
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
        except JobRegistryClosed as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except JobConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        return _accepted(snapshot)

    @router.get(
        "/models/gguf/huggingface-files", response_model=HuggingFaceFileListResponse
    )
    def list_huggingface_files(
        repo_id: str,
        _: SessionPrincipal = Depends(require_session),
    ) -> HuggingFaceFileListResponse:
        try:
            files = list_huggingface_gguf_files(repo_id)
        except GGUFDownloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return HuggingFaceFileListResponse(repo_id=repo_id, files=files)

    @router.post(
        "/models/gguf/downloads",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_gguf_download(
        payload: ModelDownloadRequest,
        request: Request,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobAccepted:
        # A new job kind, not "models": a multi-minute HF/URL download must
        # not block Ollama rescans/pulls for its duration (JobRegistry allows
        # only one active job per kind).
        settings = _load_settings(deps)
        directory = _gguf_directory(settings, request)
        try:
            url, filename = resolve_download_url(
                DownloadSource(
                    source=payload.source,
                    url=payload.url,
                    repo_id=payload.repo_id,
                    filename=payload.filename,
                )
            )
        except GGUFDownloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def runner(sink, cancel_event):
            sink.publish_progress("gguf_download", "starting", data={"filename": filename})

            def publish(update) -> None:
                sink.publish_progress(
                    "gguf_download",
                    update.status,
                    data={
                        "filename": update.filename,
                        "completed": update.completed,
                        "total": update.total,
                        "percent": update.percent,
                    },
                )

            download_gguf(
                url,
                filename,
                directory,
                progress_callback=publish,
                cancellation_event=cancel_event,
            )
            return {"filename": filename}

        try:
            snapshot = await request.app.state.jobs.start(
                kind="gguf_download",
                owner=principal.session_id,
                thread_id=None,
                runner=runner,
            )
        except JobRegistryClosed as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
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
                request_fingerprint=_request_fingerprint("create", payload),
            )
        except JobRegistryClosed as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
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
        request_fingerprint = _request_fingerprint(
            "regenerate",
            payload,
            path_thread_id=thread_id,
        )
        try:
            reservation = request.app.state.jobs.reserve(
                kind="generation",
                owner=principal.session_id,
                thread_id=thread_id,
                request_id=payload.request_id,
                request_fingerprint=request_fingerprint,
            )
            if not reservation.created:
                snapshot, _ = await request.app.state.jobs.wait_until_prepared(
                    reservation.snapshot.job_id,
                    owner=principal.session_id,
                )
            else:
                try:
                    chat = deps.chats.get_chat(thread_id)
                    if chat is None:
                        raise HTTPException(status_code=404, detail="Chat not found.")
                    position = message_position(chat, payload.message_id)
                    messages = list(chat.get("messages", ()))
                    if (
                        position != len(messages) - 1
                        or messages[position].get("role") != "assistant"
                    ):
                        raise ChatDomainError(
                            "Only the final assistant response can be regenerated."
                        )
                    if (
                        position == 0
                        or messages[position - 1].get("role") != "user"
                    ):
                        raise ChatDomainError(
                            "The selected response has no user turn to regenerate."
                        )
                    user_input = (
                        payload.user_input
                        or messages[position - 1].get("content", "")
                    ).strip()
                    if not user_input:
                        raise ChatDomainError(
                            "A regeneration request needs user input."
                        )
                    current_revision = chat_revision(chat)
                    if (
                        payload.base_revision is not None
                        and payload.base_revision != current_revision
                    ):
                        raise ChatDomainError(
                            "This chat changed. Reload it before regenerating."
                        )
                    generation_payload = GenerationRequest(
                        request_id=payload.request_id,
                        thread_id=thread_id,
                        user_input=user_input,
                        base_revision=current_revision,
                        attachments=payload.attachments,
                        options=payload.options,
                    )
                    snapshot, _ = await _start_generation_job(
                        request,
                        deps,
                        principal,
                        generation_payload,
                        request_fingerprint=request_fingerprint,
                        reservation=reservation,
                        target_message_id=payload.message_id,
                        history_messages=messages[:position],
                    )
                finally:
                    request.app.state.jobs.abort_reservation(
                        reservation,
                        owner=principal.session_id,
                    )
        except HTTPException:
            raise
        except JobRegistryClosed as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except JobConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ChatRevisionConflict as exc:
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
        jobs = request.app.state.jobs
        try:
            reservation = jobs.reserve(
                kind="generation",
                owner=principal.session_id,
                thread_id=payload.thread_id,
                request_id=payload.request_id,
                request_fingerprint=_request_fingerprint("legacy", payload),
            )
            if not reservation.created:
                snapshot, _ = await jobs.wait_until_prepared(
                    reservation.snapshot.job_id,
                    owner=principal.session_id,
                )
            else:
                try:
                    settings = _load_settings(deps)
                    generation_snapshot = _generation_snapshot(
                        reservation.snapshot.job_id,
                        payload,
                        settings,
                        deps.models.list_installed(),
                        attachments=_resolve_generation_attachments(
                            request,
                            deps,
                            principal,
                            payload.attachments,
                            settings=settings,
                        ),
                    )

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
                                "clear_requested": (
                                    result.memory_command.clear_requested
                                ),
                            },
                        }

                    snapshot, _ = jobs.start_reserved(
                        reservation,
                        owner=principal.session_id,
                        runner=runner,
                    )
                finally:
                    jobs.abort_reservation(
                        reservation,
                        owner=principal.session_id,
                    )
        except JobRegistryClosed as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except JobConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        except ChatDomainError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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


def _request_fingerprint(
    operation: str,
    payload: GenerationRequest | RegenerationRequest,
    *,
    path_thread_id: str | None = None,
) -> str:
    """Hash one validated client intent for safe request-ID idempotency."""
    canonical = json.dumps(
        {
            "version": 1,
            "operation": operation,
            "path_thread_id": path_thread_id,
            "payload": payload.model_dump(mode="json", exclude={"request_id"}),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


async def _start_generation_job(
    request: Request,
    deps: BackendDependenciesProtocol,
    principal: SessionPrincipal,
    payload: GenerationRequest,
    *,
    request_fingerprint: str,
    reservation: JobReservation | None = None,
    target_message_id: str | None = None,
    history_messages: list[Mapping[str, Any]] | None = None,
) -> tuple[JobSnapshot, str | None]:
    """Atomically admit, prepare, and run one authoritative generation job."""
    jobs = request.app.state.jobs
    candidate_thread_id = payload.thread_id or uuid4().hex
    if reservation is None:
        reservation = jobs.reserve(
            kind="generation",
            owner=principal.session_id,
            thread_id=candidate_thread_id,
            request_id=payload.request_id,
            request_fingerprint=request_fingerprint,
        )
    if not reservation.created:
        snapshot, acceptance = await jobs.wait_until_prepared(
            reservation.snapshot.job_id,
            owner=principal.session_id,
        )
        replayed_message_id = acceptance.get("user_message_id")
        return snapshot, (
            replayed_message_id if isinstance(replayed_message_id, str) else None
        )

    thread_id = reservation.snapshot.thread_id or candidate_thread_id
    started = False
    try:
        chat = await asyncio.to_thread(deps.chats.get_chat, thread_id)
        current_revision = chat_revision(chat) if chat is not None else 0
        admission_revision = current_revision
        if (
            payload.base_revision is not None
            and current_revision != payload.base_revision
        ):
            raise ChatDomainError(
                "This chat changed. Reload it before generating again."
            )

        settings = await asyncio.to_thread(_load_settings, deps)
        attachment_refs = list(payload.attachments)
        if target_message_id is not None and not attachment_refs and chat is not None:
            messages = list(chat.get("messages", ()))
            try:
                target_position = message_position(chat, target_message_id)
            except ChatDomainError:
                target_position = -1
            if target_position > 0:
                prior_user = messages[target_position - 1]
                attachment_refs = [
                    ChatAttachment.model_validate(item)
                    for item in (prior_user.get("attachments") or [])
                ]
        generation_payload = payload.model_copy(
            update={"thread_id": thread_id, "attachments": attachment_refs}
        )
        compute_observation = await _automatic_compute_observation(
            request,
            principal,
            settings,
            generation_payload,
            reservation.snapshot.job_id,
        )
        installed_models = await asyncio.to_thread(deps.models.list_installed)
        resolved_attachments = await asyncio.to_thread(
            _resolve_generation_attachments,
            request,
            deps,
            principal,
            attachment_refs,
            settings=settings,
        )
        generation_snapshot = _generation_snapshot(
            reservation.snapshot.job_id,
            generation_payload,
            settings,
            installed_models,
            compute_observation=compute_observation,
            attachments=resolved_attachments,
        )

        user_message_id: str | None = None
        prepared_revision: int | None = None
        prepared_history = history_messages

        def prepare() -> Mapping[str, Any]:
            nonlocal prepared_history, prepared_revision, user_message_id
            current_chat = deps.chats.get_chat(thread_id)
            current_revision = (
                chat_revision(current_chat) if current_chat is not None else 0
            )
            if current_revision != admission_revision:
                raise ChatDomainError(
                    "This chat changed. Reload it before generating again."
                )
            if target_message_id is not None:
                if current_chat is None:
                    raise ChatDomainError("Chat not found.")
                current_messages = list(current_chat.get("messages", ()))
                target_position = message_position(current_chat, target_message_id)
                if (
                    target_position != len(current_messages) - 1
                    or current_messages[target_position].get("role") != "assistant"
                ):
                    raise ChatDomainError(
                        "Only the final assistant response can be regenerated."
                    )
                if (
                    target_position == 0
                    or current_messages[target_position - 1].get("role") != "user"
                ):
                    raise ChatDomainError(
                        "The selected response has no user turn to regenerate."
                    )
                prepared_history = current_messages[:target_position]
                prepared_revision = current_revision
            else:
                user_message_id = deps.chats.add_message(
                    thread_id,
                    "user",
                    payload.user_input,
                    attachments=[
                        attachment.model_dump(mode="json")
                        for attachment in attachment_refs
                    ],
                    thread_title="New Chat" if current_chat is None else None,
                    expected_revision=admission_revision,
                )
                updated_chat = deps.chats.get_chat(thread_id)
                if updated_chat is None:
                    raise ChatRepositoryError("Chat did not persist the user message.")
                prepared_revision = chat_revision(updated_chat)
            return {"user_message_id": user_message_id}

        def runner(sink, cancel_event):
            result = deps.generation.generate(
                generation_snapshot,
                progress_sink=sink,
                cancellation_event=cancel_event,
                history_messages=prepared_history,
            )
            # The generation service checks cancellation around its model work,
            # but the API owns the following persistence and optional title work.
            # Keep those side effects behind explicit checkpoints as well.
            if cancel_event.is_set():
                return {"cancelled": True}
            code_execution_job_id = _queue_code_proposal(
                request,
                principal,
                settings,
                generation_snapshot.job_id,
                result,
            )
            if code_execution_job_id:
                sink.publish_progress(
                    "code_approval",
                    "A local code task is waiting for your approval.",
                    data={"execution_job_id": code_execution_job_id},
                )
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
            stats_payload = asdict(result.stats) if result.stats else None
            if target_message_id is None:
                assistant_message_id = deps.chats.add_message(
                    thread_id,
                    "assistant",
                    result.response,
                    thoughts=result.thoughts,
                    stats=stats_payload,
                    expected_revision=prepared_revision,
                )
            else:
                deps.chats.replace_message(
                    thread_id,
                    target_message_id,
                    result.response,
                    thoughts=result.thoughts,
                    stats=stats_payload,
                    expected_revision=prepared_revision,
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
                title_generator = getattr(
                    deps.generation, "generate_chat_title", None
                )
                if callable(title_generator):
                    try:
                        raw_title = title_generator(
                            generation_snapshot, result.response
                        )
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
                "code_execution_job_id": code_execution_job_id,
                "stats": stats_payload,
            }

        snapshot, acceptance = jobs.start_reserved(
            reservation,
            owner=principal.session_id,
            runner=runner,
            prepare=prepare,
        )
        started = True
        accepted_message_id = acceptance.get("user_message_id")
        return snapshot, (
            accepted_message_id if isinstance(accepted_message_id, str) else None
        )
    finally:
        if not started:
            jobs.abort_reservation(
                reservation,
                owner=principal.session_id,
            )


async def _automatic_compute_observation(
    request: Request,
    principal: SessionPrincipal,
    settings: CortexSettings,
    payload: GenerationRequest,
    generation_job_id: str,
) -> str | None:
    """Run an explicit arithmetic request before generation when it is useful.

    This is intentionally conservative: normal prose is never turned into a
    program.  If the local worker is unavailable, slow, cancelled, or rejects
    the expression, ordinary chat proceeds without a hidden retry loop.
    """

    if not settings.execution.automatic_compute:
        return None
    expression = extract_automatic_expression(payload.user_input)
    if expression is None:
        return None
    coordinator = getattr(request.app.state, "execution_coordinator", None)
    if (
        coordinator is None
        or not getattr(coordinator, "scratch_available", False)
        or not callable(getattr(coordinator, "start_scratch", None))
        or not callable(getattr(coordinator, "wait", None))
    ):
        return None
    try:
        job = await asyncio.to_thread(
            coordinator.start_scratch,
            ScratchExecutionRequest(
                owner=_execution_owner(principal),
                request_id=f"auto-{generation_job_id}",
                expression=expression,
            ),
        )
        completed = await asyncio.to_thread(
            coordinator.wait,
            job.job_id,
            timeout=DEFAULT_AUTOMATIC_COMPUTE_WAIT_SECONDS,
        )
    except (ScratchComputeError, TimeoutError, TypeError, ValueError):
        return None
    except Exception as exc:
        logging.getLogger("cortex.execution").warning(
            "Automatic safe computation was unavailable (%s).", type(exc).__name__
        )
        return None
    if completed.status != "succeeded" or not isinstance(completed.result, Mapping):
        return None
    value = completed.result.get("value")
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    return (
        "A local safe-compute worker verified this exact arithmetic result: "
        f"{expression} = {value}. Treat it as a reliable fact, explain it plainly, "
        "and do not claim to have run any other code."
    )


def _queue_code_proposal(
    request: Request,
    principal: SessionPrincipal,
    settings: CortexSettings,
    generation_job_id: str,
    result: Any,
) -> str | None:
    """Turn only a validated model proposal into a pending approval job."""

    if not settings.execution.code_execution_enabled:
        return None
    proposal = getattr(result, "code_execution_proposal", None)
    if proposal is None:
        return None
    coordinator = getattr(request.app.state, "execution_coordinator", None)
    if (
        coordinator is None
        or not getattr(coordinator, "code_execution_available", False)
        or not callable(getattr(coordinator, "start_code", None))
    ):
        return None
    try:
        job = coordinator.start_code(
            CodeExecutionTaskRequest(
                owner=_execution_owner(principal),
                request_id=f"model-{generation_job_id}",
                source=str(proposal.source),
                intent_summary=str(proposal.intent_summary),
                capabilities=CodeCapabilities.from_mapping(proposal.capabilities),
            )
        )
        return job.job_id
    except (CodeExecutionError, TypeError, ValueError) as exc:
        logging.getLogger("cortex.execution").warning(
            "Ignoring malformed model code proposal (%s).", type(exc).__name__
        )
        return None


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
        "loading_model": "generation.loading_model",
    }.get(phase or "", "generation.status")


def _llamacpp_status(request: Request) -> LlamaCppRuntimeStatus:
    manager = getattr(request.app.state, "llamacpp_manager", None)
    if manager is None:
        return LlamaCppRuntimeStatus()
    live = manager.status
    return LlamaCppRuntimeStatus(
        state=live.state,
        binary_present=live.binary_present,
        loaded_model=live.loaded_model,
        last_error=live.last_error,
        models_directory=live.models_directory,
        models_directory_exists=live.models_directory_exists,
        active_backend=live.active_backend,
        last_restart_reason=live.last_restart_reason,
    )


def _gguf_directory(settings: CortexSettings, request: Request) -> Path:
    from cortex_backend.llamacpp.model_directory import resolve_configured_directory

    default_dir = getattr(request.app.state, "default_gguf_models_dir", None) or Path(
        "gguf_models"
    )
    return resolve_configured_directory(settings.models.gguf_directory, default_dir)


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
                capabilities=model.capabilities,
                supports_vision=model.supports_vision,
                parameter_size=model.parameter_size,
                quantization_level=model.quantization_level,
                family=model.family,
                context_length=model.context_length,
                source=model.source,
            )
            for model in models
        ),
    )


def _merged_model_options(
    settings: CortexSettings,
    override: GenerationOptionsOverride | None,
) -> dict[str, float | int]:
    """Layer a per-request override on top of the standing generation defaults.

    Bounds are validated once, on GenerationSettings/GenerationOptionsOverride's
    own field definitions, so an override can't smuggle a value past what the
    global setting itself would allow.
    """
    merged: dict[str, float | int] = {
        field_name: getattr(settings.generation, field_name)
        for field_name in GENERATION_OVERRIDE_FIELDS
    }
    if override is not None:
        for field_name in GENERATION_OVERRIDE_FIELDS:
            value = getattr(override, field_name, None)
            if value is not None:
                merged[field_name] = value
    return merged


def _generation_snapshot(
    job_id: str,
    payload: GenerationRequest,
    settings: CortexSettings,
    installed_models: tuple[str, ...],
    *,
    compute_observation: str | None = None,
    attachments: tuple[GenerationAttachment, ...] = (),
) -> GenerationSnapshot:
    chat_model = _selected_local_model(settings.models.chat, installed_models)
    if chat_model is None:
        raise ChatDomainError(
            "No local model is available. Install one in Ollama, or add a GGUF file, then rescan Models in Settings."
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
    instructions = settings.generation.system_instructions or None
    if compute_observation:
        instructions = (
            f"{instructions}\n\n{compute_observation}"
            if instructions
            else compute_observation
        )
    return GenerationSnapshot(
        job_id=job_id,
        thread_id=payload.thread_id or "",
        user_input=payload.user_input,
        model=chat_model,
        title_model=title_model,
        translation_model=settings.models.translation,
        model_options=_merged_model_options(settings, payload.options),
        memories_enabled=settings.memory.enabled,
        translation_enabled=settings.translation.enabled,
        target_language=settings.translation.target_language,
        user_system_instructions=instructions,
        attachments=attachments,
        code_execution_eligible=(
            settings.execution.code_execution_enabled
            and should_offer_code_execution(payload.user_input)
        ),
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
            "attachments": message.get("attachments"),
            "stats": message.get("stats"),
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


def _execution_owner(principal: SessionPrincipal) -> str:
    """Use the durable installation owner for execution, not the expiring session."""
    return principal.installation_principal_id


def _execution_runtime(request: Request):
    """Require an explicitly enabled execution runtime for shared job routes."""
    coordinator = getattr(request.app.state, "execution_coordinator", None)
    if not request.app.state.preview or coordinator is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution preview is unavailable.",
        )
    return coordinator


def _fake_execution_coordinator(request: Request) -> DurableFakeCoordinator:
    """Keep the deterministic preview route separate from recipe execution."""

    coordinator = _execution_runtime(request)
    if not isinstance(coordinator, DurableFakeCoordinator):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution preview is unavailable.",
        )
    return coordinator


def _recipe_coordinator(request: Request):
    """Expose a ready fixed-image profile without granting general execution."""

    lifecycle = getattr(request.app.state, "execution_lifecycle", None)
    if (
        not request.app.state.preview
        or lifecycle is None
        or not getattr(getattr(lifecycle, "snapshot", None), "available", False)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recipe execution is unavailable.",
        )
    coordinator = getattr(lifecycle, "coordinator", None)
    if (
        coordinator is None
        or not callable(getattr(coordinator, "start_image_transform", None))
        or not isinstance(getattr(coordinator, "artifact_boundary", None), ArtifactBoundary)
        or not getattr(coordinator, "image_transform_available", True)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recipe execution is unavailable.",
        )
    return coordinator


def _scratch_coordinator(request: Request):
    """Require the narrow local safe-compute capability explicitly."""

    coordinator = _execution_runtime(request)
    if (
        not getattr(coordinator, "scratch_available", False)
        or not callable(getattr(coordinator, "start_scratch", None))
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Safe computation is unavailable.",
        )
    return coordinator


def _code_coordinator(request: Request):
    """Require the local approval-gated code capability explicitly."""

    coordinator = _execution_runtime(request)
    if (
        not getattr(coordinator, "code_execution_available", False)
        or not callable(getattr(coordinator, "start_code", None))
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Local code execution is unavailable.",
        )
    return coordinator


def _raise_recipe_request_error(exc: RecipeExecutionError) -> None:
    """Map internal recipe categories to stable, non-sensitive HTTP responses."""

    if exc.code == "request_conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Recipe request conflicts with an existing request.",
        ) from exc
    if exc.code == "input_artifact_unavailable":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source artifact is unavailable.",
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Recipe request could not be accepted safely.",
    ) from exc


def _raise_scratch_request_error(exc: ScratchComputeError) -> None:
    """Map scratch errors without exposing input or worker details."""

    if exc.code == "request_conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Safe computation request conflicts with an existing request.",
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Safe computation request could not be accepted safely.",
    ) from exc


def _raise_attachment_staging_error(exc: AttachmentStagingError) -> None:
    """Map attachment stage categories to stable, non-sensitive responses."""

    if exc.code == "request_conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Attachment request conflicts with an existing request.",
        ) from exc
    if exc.code == "attachment_in_progress":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Attachment request is already in progress.",
        ) from exc
    if exc.code in {"attachment_artifact_unavailable", "attachment_failed"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Attachment request is no longer available.",
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Attachment could not be staged safely.",
    ) from exc


def _chat_attachment_service(
    request: Request,
    deps: BackendDependenciesProtocol,
) -> ChatAttachmentService:
    service = getattr(deps, "attachments", None) or getattr(
        request.app.state, "chat_attachment_service", None
    )
    if not isinstance(service, ChatAttachmentService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat attachments are unavailable in this runtime.",
        )
    return service


def _attachment_owner(request: Request, principal: SessionPrincipal) -> str:
    """Bind attachments to the local installation, not an expiring session."""

    return str(
        getattr(request.app.state, "installation_principal_id", None)
        or principal.session_id
    )


def _raise_chat_attachment_error(exc: ChatAttachmentError) -> None:
    messages = {
        "attachment_too_large": "Files must be 10 MB or smaller.",
        "attachment_type_unsupported": "Cortex supports images and common text/code/config documents.",
        "attachment_not_text": "That document is not a readable text file.",
        "attachment_image_invalid": "The image could not be verified safely.",
        "attachment_request_conflict": "That upload request conflicts with an existing attachment.",
        "attachment_unavailable": "That attachment is no longer available. Upload it again.",
        "attachment_integrity_failed": "The attachment failed an integrity check. Upload it again.",
    }
    if exc.code == "attachment_request_conflict":
        status_code = status.HTTP_409_CONFLICT
    elif exc.code in {"attachment_unavailable", "attachment_integrity_failed"}:
        status_code = status.HTTP_404_NOT_FOUND
    else:
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    raise HTTPException(
        status_code=status_code,
        detail=messages.get(exc.code, "Attachment could not be staged safely."),
    ) from exc


def _resolve_generation_attachments(
    request: Request,
    deps: BackendDependenciesProtocol,
    principal: SessionPrincipal,
    references: list[ChatAttachment],
    *,
    settings: CortexSettings,
) -> tuple[GenerationAttachment, ...]:
    if not references:
        return ()
    if len(references) > MAX_CHAT_ATTACHMENTS:
        raise ChatDomainError("A message can include at most eight attachments.")
    if sum(item.size for item in references) > MAX_CHAT_ATTACHMENT_TOTAL_BYTES:
        raise ChatDomainError("The combined attachment size is too large for one message.")
    installed = deps.models.list_installed()
    model = _selected_local_model(settings.models.chat, installed)
    contains_image = any(item.kind == "image" for item in references)
    if contains_image and model is not None:
        vision = deps.models.model_supports_vision(model)
        if vision is False:
            raise ChatDomainError(
                f"Selected model '{model}' does not support image input. Choose a vision model or remove the image."
            )
    service = _chat_attachment_service(request, deps)
    owner = _attachment_owner(request, principal)
    resolved: list[GenerationAttachment] = []
    seen: set[str] = set()
    for reference in references:
        if reference.attachment_id in seen:
            raise ChatDomainError("The same attachment cannot be added twice.")
        seen.add(reference.attachment_id)
        try:
            item = service.resolve(owner=owner, descriptor=reference.model_dump(mode="json"))
        except ChatAttachmentError as exc:
            _raise_chat_attachment_error(exc)
        resolved.append(
            GenerationAttachment(
                attachment_id=item.descriptor.attachment_id,
                filename=item.descriptor.filename,
                mime_type=item.descriptor.mime_type,
                kind="image" if item.descriptor.kind == "image" else "document",
                text_content=item.text_content,
                image_base64=item.image_base64,
            )
        )
    return tuple(resolved)


def _validate_chat_attachment_refs(
    request: Request,
    deps: BackendDependenciesProtocol,
    principal: SessionPrincipal,
    references: list[ChatAttachment],
) -> list[ChatAttachment]:
    """Validate metadata-only message writes against the local attachment store."""

    if not references:
        return []
    if len(references) > MAX_CHAT_ATTACHMENTS:
        raise ChatDomainError("A message can include at most eight attachments.")
    if sum(item.size for item in references) > MAX_CHAT_ATTACHMENT_TOTAL_BYTES:
        raise ChatDomainError("The combined attachment size is too large for one message.")
    service = _chat_attachment_service(request, deps)
    owner = _attachment_owner(request, principal)
    normalized: list[ChatAttachment] = []
    seen: set[str] = set()
    for reference in references:
        if reference.attachment_id in seen:
            raise ChatDomainError("The same attachment cannot be added twice.")
        seen.add(reference.attachment_id)
        try:
            resolved = service.resolve(owner=owner, descriptor=reference.model_dump(mode="json"))
        except ChatAttachmentError:
            raise
        normalized.append(ChatAttachment.model_validate(resolved.descriptor.as_dict()))
    return normalized


def _execution_repository(request: Request):
    return _execution_runtime(request).repository


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
    code_fields = _code_job_fields(job)
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
        **code_fields,
    )


def _execution_task_summary(repository, job: ExecutionJob) -> ExecutionTaskSummary:
    response = _execution_status_response(repository, job)
    code_fields = _code_job_fields(job, include_result=True)
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
        error=response.error,
        created_at=datetime.fromisoformat(job.created_at),
        updated_at=datetime.fromisoformat(job.updated_at),
        **code_fields,
    )


def _code_job_fields(job: ExecutionJob, *, include_result: bool = False) -> dict[str, Any]:
    if job.profile != CODE_EXECUTION_PROFILE:
        return {}
    payload = job.payload
    capabilities = payload.get("capabilities")
    fields: dict[str, Any] = {
        "intent_summary": payload.get("intent_summary") if isinstance(payload.get("intent_summary"), str) else None,
        "source_digest": payload.get("source_digest") if isinstance(payload.get("source_digest"), str) else None,
        "capabilities": CodeCapabilitiesRequest.model_validate(capabilities or {}),
    }
    if include_result:
        result = job.result if isinstance(job.result, Mapping) else None
        result_summary: dict[str, Any] | None = None
        if result is not None:
            result_summary = dict(result)
            for key in ("stdout", "stderr"):
                value = result_summary.get(key)
                if isinstance(value, str) and len(value) > 8_192:
                    result_summary[key] = value[:8_192]
                    result_summary["truncated"] = True
        fields["result"] = result_summary
    return fields


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
