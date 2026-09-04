"""The deterministic execution-preview route, kept out of the shipped API.

``/execution/preview/fake`` drives the generic execution surface -- owner
scoping, idempotent replay, durable cancel, leases, the task list, and the SSE
stream -- without running real work. That makes it good test scaffolding and
bad production surface: in a shipped build it validated a request body and then
answered 404, and its request/response models sat in the generated contract
under a ``fake.v1`` profile no client could ever receive.

Tests attach it explicitly with :func:`install_execution_preview`.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from pydantic import Field

from cortex_backend.api.routes import _durable_owner, _execution_runtime
from cortex_backend.api.schemas import APIModel, ExecutionStatus
from cortex_backend.api.security import SessionPrincipal

from .execution_preview import DurableFakeCoordinator
from .fake_execution import FakeExecutionPlan


class ExecutionPreviewRequest(APIModel):
    request_id: str = Field(min_length=1, max_length=200)
    outcome: Literal["success", "failure"] = "success"
    steps: int = Field(default=3, ge=1, le=20)
    step_delay_seconds: float = Field(default=0.0, ge=0.0, le=1.0)


class ExecutionAccepted(APIModel):
    job_id: str
    request_id: str
    profile: Literal["fake.v1"]
    status: ExecutionStatus
    sequence: int


def _preview_coordinator(request: Request) -> DurableFakeCoordinator:
    """Keep the deterministic preview route separate from recipe execution."""

    coordinator = _execution_runtime(request)
    if not isinstance(coordinator, DurableFakeCoordinator):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution preview is unavailable.",
        )
    return coordinator


def build_execution_preview_router() -> APIRouter:
    """Build the preview router against the app's own session dependency."""

    router = APIRouter()

    def require_session(request: Request) -> SessionPrincipal:
        return request.app.state.session_manager.require(request)

    @router.post(
        "/execution/preview/fake",
        response_model=ExecutionAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        include_in_schema=False,
    )
    def start_fake_execution(
        request: Request,
        payload: ExecutionPreviewRequest,
        principal: SessionPrincipal = Depends(require_session),  # noqa: B008
    ) -> ExecutionAccepted:
        coordinator = _preview_coordinator(request)
        try:
            job = coordinator.start(
                owner=_durable_owner(principal),
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

    return router


def install_execution_preview(app: FastAPI) -> FastAPI:
    """Attach the preview route to an app built with a fake coordinator."""

    app.include_router(build_execution_preview_router(), prefix="/api/v1")
    return app
