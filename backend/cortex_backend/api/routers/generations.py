"""Generation admission, streaming, and the job registry.

Registered by cortex_backend.api.routers.build_router. Shared helpers live
in cortex_backend.api.routes.
"""

from __future__ import annotations

from fastapi import APIRouter
import json
from cortex_backend.api.app_types import BackendDependenciesProtocol
from cortex_backend.api.jobs import (
    JobConflict,
    JobNotFound,
    JobOwnershipError,
    JobRegistryClosed,
)
from cortex_backend.api.routes import (
    _accepted,
    _durable_owner,
    _event_cursor,
    _generation_event_name,
    _job_response,
    _job_status,
    _raise_chat_domain_error,
    _raise_job_error,
    _request_fingerprint,
    _start_generation_job,
)
from cortex_backend.api.schemas import (
    GenerationEvent,
    GenerationRequest,
    JobAccepted,
    JobStatusResponse,
    SSEEvent,
)
from cortex_backend.api.security import SessionPrincipal
from cortex_backend.repositories.chats import ChatRevisionConflict
from cortex_backend.services.chat import ChatDomainError
from datetime import (
    datetime,
    timezone,
)
from fastapi import (
    Depends,
    HTTPException,
    Header,
    Request,
    status,
)
from fastapi.responses import StreamingResponse


GENERATIONS_TAG = "generations"
JOBS_TAG = "jobs"

# Shown in the OpenAPI document (see create_app). The two families overlap on
# purpose only one way: /jobs is generic, /generations is a strict subset.
OPENAPI_TAGS = [
    {
        "name": GENERATIONS_TAG,
        "description": (
            "Chat turns. Status, cancel and events answer only for a job of "
            "kind generation; the id of any other kind of job is a 404 here."
        ),
    },
    {
        "name": JOBS_TAG,
        "description": (
            "The generic job family: status, cancel and events for a job of "
            "any kind (generation, models, gguf_download). Model and GGUF "
            "download progress is read here; chat clients use /generations."
        ),
    },
]


def register(router: APIRouter, *, require_session, dependencies) -> None:
    """Attach the generations routes to ``router``."""


    @router.post(
        "/generations",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        tags=[GENERATIONS_TAG],
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
        except ChatRevisionConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        except ChatDomainError as exc:
            _raise_chat_domain_error(exc)
        return _accepted(snapshot, user_message_id=user_message_id)


    @router.get(
        "/generations/{job_id}",
        response_model=JobStatusResponse,
        tags=[GENERATIONS_TAG],
    )
    def generation_status(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobStatusResponse:
        return _job_response(
            _job_status(request, job_id, principal, kind="generation")
        )


    @router.post(
        "/generations/{job_id}/cancel",
        response_model=JobStatusResponse,
        tags=[GENERATIONS_TAG],
    )
    def cancel_generation(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobStatusResponse:
        # Checked first: a job of another kind must not be stopped from here.
        _job_status(request, job_id, principal, kind="generation")
        try:
            snapshot = request.app.state.jobs.cancel(job_id, owner=_durable_owner(principal))
        except (JobNotFound, JobOwnershipError) as exc:
            _raise_job_error(exc)
        return _job_response(snapshot)


    @router.get(
        "/generations/{job_id}/events",
        response_model=GenerationEvent,
        response_class=StreamingResponse,
        tags=[GENERATIONS_TAG],
        responses={
            200: {
                "description": "Server-sent generation events.",
                "content": {
                    "text/event-stream": {
                        "schema": {"$ref": "#/components/schemas/GenerationEvent"}
                    }
                },
            }
        },
    )
    async def generation_events(
        job_id: str,
        request: Request,
        last_event_id: str | None = Header(
            default=None,
            alias="Last-Event-ID",
            description="Resume after this event sequence number.",
        ),
        principal: SessionPrincipal = Depends(require_session),
    ) -> StreamingResponse:
        cursor = _event_cursor(request, last_event_id)
        _job_status(request, job_id, principal, kind="generation")
        try:
            event_stream = request.app.state.jobs.events(
                job_id,
                owner=_durable_owner(principal),
                after_sequence=cursor,
                # An open stream would hold graceful shutdown open for as long
                # as its job runs; end it once shutdown has been requested.
                stop=lambda: request.app.state.shutting_down,
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


    @router.get(
        "/jobs/{job_id}", response_model=JobStatusResponse, tags=[JOBS_TAG]
    )
    def get_job(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobStatusResponse:
        snapshot = _job_status(request, job_id, principal)
        return _job_response(snapshot)


    @router.post(
        "/jobs/{job_id}/cancel", response_model=JobStatusResponse, tags=[JOBS_TAG]
    )
    def cancel_job(
        job_id: str,
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobStatusResponse:
        try:
            snapshot = request.app.state.jobs.cancel(job_id, owner=_durable_owner(principal))
        except (JobNotFound, JobOwnershipError) as exc:
            _raise_job_error(exc)
        return _job_response(snapshot)


    @router.get(
        "/jobs/{job_id}/events",
        response_model=SSEEvent,
        response_class=StreamingResponse,
        tags=[JOBS_TAG],
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
            request.app.state.jobs.status(job_id, owner=_durable_owner(principal))
            event_stream = request.app.state.jobs.events(
                job_id,
                owner=_durable_owner(principal),
                after_sequence=cursor,
                # An open stream would hold graceful shutdown open for as long
                # as its job runs; end it once shutdown has been requested.
                stop=lambda: request.app.state.shutting_down,
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
