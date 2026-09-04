"""Approval-gated execution, attachments, and artifacts.

Registered by cortex_backend.api.routers.build_router. Shared helpers live
in cortex_backend.api.routes.
"""

from __future__ import annotations

from fastapi import APIRouter
import asyncio
import base64
import binascii
import time
from cortex_backend.api.app_types import BackendDependenciesProtocol
from cortex_backend.api.routes import (
    EXECUTION_STREAM_HEARTBEAT_SECONDS,
    EXECUTION_STREAM_IDLE_TIMEOUT_SECONDS,
    EXECUTION_STREAM_POLL_SECONDS,
    _attachment_owner,
    _chat_attachment_service,
    _code_coordinator,
    _durable_owner,
    _execution_repository,
    _execution_runtime,
    _execution_sse_line,
    _execution_status_response,
    _execution_task_summary,
    _last_event_cursor,
    _load_settings,
    _poll_execution_stream,
    _raise_attachment_staging_error,
    _raise_chat_attachment_error,
    _raise_recipe_request_error,
    _raise_scratch_request_error,
    _recipe_coordinator,
    _scratch_coordinator,
)
from cortex_backend.api.schemas import (
    AttachmentStageAccepted,
    AttachmentStageRequest,
    ChatAttachment,
    ChatAttachmentStageRequest,
    CodeCapabilitiesRequest,
    CodeExecutionAccepted,
    CodeExecutionRequest,
    CodeExecutionSourceResponse,
    ExecutionApprovalDecisionRequest,
    ExecutionSSEEvent,
    ExecutionStatusResponse,
    ExecutionTaskListResponse,
    RecipeImageTransformAccepted,
    RecipeImageTransformRequest,
    ScratchComputeAccepted,
    ScratchComputeRequest,
)
from cortex_backend.api.security import SessionPrincipal
from cortex_backend.execution.attachment_staging import (
    AttachmentStagingError,
    AttachmentStagingService,
)
from cortex_backend.execution.code_execution import (
    CODE_EXECUTION_PROFILE,
    CodeExecutionError,
    CodeExecutionRequest as CodeExecutionTaskRequest,
)
from cortex_backend.execution.models import TerminalExecutionStatus
from cortex_backend.execution.recipe_coordinator import (
    RECIPE_IMAGE_PROFILE,
    RecipeExecutionError,
    RecipeImageRequest,
)
from cortex_backend.execution.repository import (
    ApprovalPolicyError,
    ApprovalTransitionError,
    ExecutionRepositoryError,
)
from cortex_backend.execution.scratch_compute import (
    ScratchComputeError,
    ScratchComputeRequest as ScratchExecutionRequest,
)
from cortex_backend.services.attachments import ChatAttachmentError
from datetime import datetime
from fastapi import (
    Depends,
    HTTPException,
    Header,
    Request,
    status,
)
from fastapi.responses import (
    Response,
    StreamingResponse,
)


def register(router: APIRouter, *, require_session, dependencies) -> None:
    """Attach the execution routes to ``router``."""


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
                    owner=_durable_owner(principal),
                    request_id=payload.request_id,
                    expression=payload.expression,
                )
            )
        except ScratchComputeError as exc:
            _raise_scratch_request_error(exc)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
                    owner=_durable_owner(principal),
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
                "process_capability_unavailable": (
                    "Process access is unavailable until native sandbox isolation is enabled."
                ),
            }.get(exc.code, "Code execution request is invalid.")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT if exc.code == "request_conflict" else status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=detail,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Code execution request is invalid.") from exc
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
        """Start one owner-scoped image recipe.

        The route is intentionally unavailable unless the app was built with
        an execution lifecycle that completed its health-gated startup.
        Attachment staging is a separate trusted boundary; callers provide
        only its opaque artifact identifier here.
        """

        coordinator = _recipe_coordinator(request)
        try:
            job = coordinator.start_image_transform(
                RecipeImageRequest(
                    owner=_durable_owner(principal),
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
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Attachment payload is invalid.",
            ) from None
        try:
            staged = AttachmentStagingService(
                coordinator.repository,
                coordinator.artifact_boundary,
            ).stage(
                owner=_durable_owner(principal),
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
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
            owner=_durable_owner(principal),
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
                owner=_durable_owner(principal),
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
        job = repository.get_job(job_id, owner=_durable_owner(principal))
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
        job = repository.get_job(job_id, owner=_durable_owner(principal))
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
                owner=_durable_owner(principal),
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
        job = repository.get_job(job_id, owner=_durable_owner(principal))
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
            coordinator.cancel(job_id, owner=_durable_owner(principal))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Execution job not found.") from exc
        job = coordinator.repository.get_job(job_id, owner=_durable_owner(principal))
        if job is None:
            raise HTTPException(status_code=404, detail="Execution job not found.")
        return _execution_status_response(coordinator.repository, job)


    @router.get(
        "/execution/{job_id}/events",
        response_model=ExecutionSSEEvent,
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Server-sent execution events.",
                "headers": {
                    "Cache-Control": {
                        "description": "Prevent intermediary caching of the live event stream.",
                        "schema": {"type": "string"},
                    },
                    "X-Accel-Buffering": {
                        "description": "Disable proxy buffering for incremental events.",
                        "schema": {"type": "string", "enum": ["no"]},
                    },
                },
                "content": {
                    "text/event-stream": {
                        "schema": {"$ref": "#/components/schemas/ExecutionSSEEvent"},
                    }
                },
            }
        },
    )
    async def execution_events(
        job_id: str,
        request: Request,
        last_event_id: str | None = Header(
            default=None,
            alias="Last-Event-ID",
            description="Resume after this event sequence number.",
        ),
        principal: SessionPrincipal = Depends(require_session),
    ) -> StreamingResponse:
        repository = _execution_repository(request)
        if repository.get_job(job_id, owner=_durable_owner(principal)) is None:
            raise HTTPException(status_code=404, detail="Execution job not found.")
        cursor = _last_event_cursor(request, last_event_id)

        owner = _durable_owner(principal)

        async def stream():
            next_sequence = cursor
            idle_since = time.monotonic()
            last_heartbeat = idle_since
            while True:
                # One hop off the event loop per tick, covering both reads.
                # These are synchronous SQLite calls -- each opens a
                # connection and issues its pragmas -- and running them
                # inline blocked the loop that also serves the generation
                # stream and every other request.
                events, current = await asyncio.to_thread(
                    _poll_execution_stream, repository, job_id, owner, next_sequence
                )
                if events:
                    idle_since = time.monotonic()
                    for event in events:
                        next_sequence = event.sequence
                        yield _execution_sse_line(event)
                    last_heartbeat = idle_since
                if current is None or current.status in TerminalExecutionStatus:
                    return
                now = time.monotonic()
                if not events:
                    if now - idle_since >= EXECUTION_STREAM_IDLE_TIMEOUT_SECONDS:
                        return
                    if now - last_heartbeat >= EXECUTION_STREAM_HEARTBEAT_SECONDS:
                        last_heartbeat = now
                        # An SSE comment: clients ignore it, but it keeps the
                        # connection demonstrably alive while a job waits on
                        # an approval that emits nothing.
                        yield ": keep-alive\n\n"
                if await request.is_disconnected():
                    return
                await asyncio.sleep(EXECUTION_STREAM_POLL_SECONDS)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
