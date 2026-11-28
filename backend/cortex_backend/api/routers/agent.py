"""Bearer-session-only API for opt-in, work-copy-isolated coding-agent runs."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from cortex_backend.agent.run_store import AgentRun
from cortex_backend.agent.workcopy import WorkCopyError
from cortex_backend.api.app_types import BackendDependenciesProtocol
from cortex_backend.api.security import SessionPrincipal
from cortex_backend.services.agent_runs import AgentRuns, AgentWorkflowError


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartAgentRun(AgentModel):
    project_root: str = Field(min_length=1, max_length=4096)
    goal: str = Field(min_length=1, max_length=20000)
    mode: Literal["plan_only", "ask", "auto_sandboxed"]
    model: str = Field(min_length=1, max_length=2048)
    budgets: dict[str, int | float] = Field(default_factory=dict)
    confirmed_project: bool = Field(default=False, strict=True)
    confirmed_cloud: bool = Field(default=False, strict=True)


class DecideAgentApproval(AgentModel):
    decision: Literal["allow_once", "deny"]


class AcceptAgentChanges(AgentModel):
    paths: list[str] = Field(min_length=1, max_length=32)
    confirmed: bool = Field(default=False, strict=True)


class UndoAgentChanges(AgentModel):
    confirmed: bool = Field(default=False, strict=True)


class AgentRunResponse(AgentModel):
    id: str
    project_root: str
    mode: str
    model: str
    status: str
    budgets: dict[str, int | float]
    created: str
    updated: str
    stop_reason: str | None = None
    undo_available: bool = False
    # The work copy still exists, so its changes can be reviewed or discarded.
    copy_available: bool = False
    # A decision was recorded; the run continues only after an explicit Resume.
    resume_required: bool = False


class AgentRunListResponse(AgentModel):
    runs: list[AgentRunResponse]


class AgentChange(AgentModel):
    path: str
    kind: Literal["add", "modify", "delete"]
    baseline_sha256: str | None
    new_sha256: str | None
    diff: str


class AgentChangesetResponse(AgentModel):
    run_id: str
    changes: list[AgentChange]


class AgentAcceptedResponse(AgentModel):
    paths: list[str]


class AgentDiscardedResponse(AgentModel):
    discarded: bool


class AgentUndoneResponse(AgentModel):
    paths: list[str]


class AgentSSEEvent(AgentModel):
    event_id: int
    event: Literal["step", "output", "approval", "status"]
    run_id: str
    data: dict[str, Any]


# No exception strings, source text, model output or filesystem paths enter
# an error body. Unrecognized failures collapse to an intentionally opaque code.
_ERRORS: dict[str, tuple[int, str]] = {
    "agent_disabled": (403, "Agent mode is disabled in Settings."),
    "consent_required": (403, "Explicit project approval is required."),
    "cloud_consent_required": (403, "Explicit cloud-model approval is required."),
    "busy": (409, "Another agent operation is in progress."),
    "not_found": (404, "That agent run is unavailable."),
    "copy_unavailable": (409, "That work copy is no longer available."),
    "model_unavailable": (409, "An agent model connection is unavailable."),
    "agent_unavailable": (503, "Local agent services are unavailable."),
    "invalid_mode": (422, "The agent mode is invalid."),
    "invalid_budgets": (422, "The agent budgets are invalid."),
    "invalid_decision": (422, "The approval decision is invalid."),
    "invalid_cursor": (400, "Last-Event-ID must be a nonnegative event number."),
    "invalid_state": (409, "This run cannot perform the requested action."),
    "approval_required": (409, "An approval decision is required before resuming."),
    "approval_unavailable": (409, "That approval is no longer available."),
    "approval_expired": (409, "The approval has expired."),
    "invalid_root": (422, "The selected project folder is invalid."),
    "protected_root": (422, "The selected project folder is protected."),
    "invalid_data_dir": (503, "Local agent storage is unavailable."),
    "invalid_run_id": (422, "The run identifier is invalid."),
    "run_exists": (409, "A work copy with that identifier already exists."),
    "copy_limit": (422, "The project exceeds the work-copy limits."),
    "ignore_limit": (422, "The project ignore rules exceed the supported limit."),
    "scan_failed": (409, "The project could not be inspected safely."),
    "source_changed": (409, "A project file changed during preparation."),
    "unsafe_file": (409, "An unsafe source file was rejected."),
    "unsafe_path": (409, "An unsafe linked path was rejected."),
    "invalid_path": (422, "A relative source path is invalid."),
    "invalid_patch": (422, "The requested edit is invalid."),
    "invalid_selection": (422, "Select one to thirty-two changed source paths."),
    "diff_too_large": (409, "A proposed diff is too large to review."),
    "stale_root": (409, "The project or work-copy root changed."),
    "stale_source": (409, "A project file changed; the edit was not applied."),
    "discarded": (409, "The work copy has already been discarded."),
    "copy_failed": (409, "The work copy could not be prepared."),
    "discard_failed": (409, "The work copy could not be discarded safely."),
    "git_unavailable": (409, "The project's Git worktree is unavailable."),
    "git_failed": (409, "A Git worktree operation failed."),
    "checkpoint_pending": (409, "A write requires checkpoint recovery."),
    "checkpoint_limit": (409, "The checkpoint storage limit was reached."),
    "checkpoint_corrupt": (409, "The recovery checkpoint is invalid."),
    "checkpoint_missing": (409, "There are no accepted changes to undo."),
    "restore_failed": (409, "The accepted changes could not be restored safely."),
    "recovery_required": (409, "An unfinished recovery checkpoint must be resolved."),
    "storage_unavailable": (503, "Local recovery storage is unavailable."),
    "storage_version": (503, "Local recovery storage uses an unsupported format."),
    "partial_rollback": (409, "An interrupted write requires manual recovery."),
    "write_failed": (409, "The selected edit was not applied."),
}


def _error(code: str) -> HTTPException:
    status, message = _ERRORS.get(code, _ERRORS["agent_unavailable"])
    safe_code = code if code in _ERRORS else "agent_unavailable"
    return HTTPException(status_code=status, detail={"code": safe_code, "message": message})


def _call(function: Callable[..., Any], **kwargs: Any) -> Any:
    try:
        return function(**kwargs)
    except (AgentWorkflowError, WorkCopyError) as exc:
        raise _error(exc.code) from None
    except Exception:
        # SQLite or filesystem exception messages may contain absolute paths,
        # source data or secrets. Never echo them or log raw exceptions.
        raise _error("agent_unavailable") from None


def _response(run: AgentRun, workflow: AgentRuns, owner: str) -> AgentRunResponse:
    return AgentRunResponse(
        id=run.id, project_root=run.project_root, mode=run.mode, model=run.model,
        status=run.status, budgets=run.budgets, created=run.created,
        updated=run.updated, stop_reason=run.stop_reason,
        undo_available=workflow.undo_available(run.id, owner=owner),
        copy_available=workflow.copy_available(run.id, owner=owner),
        resume_required=workflow.resume_required(run.id, owner=owner),
    )


def _cursor(value: str | None) -> int:
    if value is None or value == "":
        return 0
    if re.fullmatch(r"[0-9]{1,16}", value) is None:
        raise _error("invalid_cursor")
    return int(value)


def register(
    router: APIRouter, *,
    require_session: Callable[..., SessionPrincipal],
    dependencies: Callable[..., BackendDependenciesProtocol],
) -> None:
    """Register every method under bearer auth and the persisted feature gate."""

    def enabled(
        request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> SessionPrincipal:
        try:
            settings = dependencies(request).settings.load().settings
        except Exception:
            raise _error("agent_unavailable") from None
        if not settings.agent.enabled:
            raise _error("agent_disabled")
        return principal

    @router.post("/agent/runs", response_model=AgentRunResponse, status_code=202)
    def start(
        payload: StartAgentRun, request: Request,
        principal: SessionPrincipal = Depends(enabled),
    ) -> AgentRunResponse:
        from cortex_backend.services.chat_client import is_cloud_model

        if not payload.confirmed_project:
            raise _error("consent_required")
        if is_cloud_model(payload.model) and not payload.confirmed_cloud:
            raise _error("cloud_consent_required")
        workflow: AgentRuns = request.app.state.agent_workflow
        run = _call(
            workflow.start, owner=principal.session_id, project_root=payload.project_root,
            goal=payload.goal, mode=payload.mode, model=payload.model, budgets=payload.budgets,
            confirmed_project=payload.confirmed_project, confirmed_cloud=payload.confirmed_cloud,
        )
        return _response(run, workflow, principal.session_id)

    @router.get("/agent/runs", response_model=AgentRunListResponse)
    def list_runs(
        request: Request,
        principal: SessionPrincipal = Depends(enabled),
    ) -> AgentRunListResponse:
        workflow: AgentRuns = request.app.state.agent_workflow
        return AgentRunListResponse(runs=[
            _response(run, workflow, principal.session_id)
            for run in _call(workflow.list_runs, owner=principal.session_id)
        ])

    @router.get("/agent/runs/{run_id}", response_model=AgentRunResponse)
    def get_run(
        run_id: str, request: Request,
        principal: SessionPrincipal = Depends(enabled),
    ) -> AgentRunResponse:
        workflow: AgentRuns = request.app.state.agent_workflow
        return _response(_call(workflow.get, run_id=run_id, owner=principal.session_id),
                         workflow, principal.session_id)

    @router.get(
        "/agent/runs/{run_id}/events",
        response_model=AgentSSEEvent,
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Server-sent agent events.",
                "content": {"text/event-stream": {
                    "schema": {"$ref": "#/components/schemas/AgentSSEEvent"},
                }},
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
            }
        },
    )
    async def events(
        run_id: str, request: Request,
        last_event_id: str | None = Header(
            default=None, alias="Last-Event-ID",
            description="Resume after this agent event sequence number.",
        ),
        principal: SessionPrincipal = Depends(enabled),
    ) -> StreamingResponse:
        cursor = _cursor(last_event_id)
        workflow: AgentRuns = request.app.state.agent_workflow
        _call(workflow.get, run_id=run_id, owner=principal.session_id)

        async def stream() -> Any:
            last = cursor
            while True:
                try:
                    batch, finished = workflow.events(run_id, owner=principal.session_id, after_id=last)
                except AgentWorkflowError:
                    return
                for event in batch:
                    last = event.id
                    packet = AgentSSEEvent(
                        event_id=event.id, event=event.kind, run_id=event.run_id, data=event.data,
                    ).model_dump(mode="json")
                    yield (
                        f"id: {event.id}\n"
                        f"event: {event.kind}\n"
                        f"data: {json.dumps(packet, separators=(',', ':'), ensure_ascii=True)}\n\n"
                    )
                if finished or await request.is_disconnected():
                    return
                await asyncio.sleep(0.05)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/agent/runs/{run_id}/approvals/{seq}", response_model=AgentRunResponse)
    def approval(
        run_id: str, seq: int, payload: DecideAgentApproval, request: Request,
        principal: SessionPrincipal = Depends(enabled),
    ) -> AgentRunResponse:
        workflow: AgentRuns = request.app.state.agent_workflow
        return _response(_call(
            workflow.approval, run_id=run_id, owner=principal.session_id,
            seq=seq, decision=payload.decision,
        ), workflow, principal.session_id)

    @router.post("/agent/runs/{run_id}/cancel", response_model=AgentRunResponse)
    def cancel(
        run_id: str, request: Request,
        principal: SessionPrincipal = Depends(enabled),
    ) -> AgentRunResponse:
        workflow: AgentRuns = request.app.state.agent_workflow
        return _response(_call(workflow.cancel, run_id=run_id, owner=principal.session_id),
                         workflow, principal.session_id)

    @router.get("/agent/runs/{run_id}/changeset", response_model=AgentChangesetResponse)
    def changeset(
        run_id: str, request: Request,
        principal: SessionPrincipal = Depends(enabled),
    ) -> AgentChangesetResponse:
        workflow: AgentRuns = request.app.state.agent_workflow
        changes = _call(workflow.changeset, run_id=run_id, owner=principal.session_id)
        return AgentChangesetResponse(
            run_id=run_id, changes=[
                AgentChange(
                    path=path, kind=change.kind,
                    baseline_sha256=change.baseline_sha256,
                    new_sha256=change.new_sha256, diff=change.diff,
                )
                for path, change in sorted(changes.items())
            ],
        )

    @router.post("/agent/runs/{run_id}/accept", response_model=AgentAcceptedResponse)
    def accept(
        run_id: str, payload: AcceptAgentChanges, request: Request,
        principal: SessionPrincipal = Depends(enabled),
    ) -> AgentAcceptedResponse:
        if not payload.confirmed:
            raise _error("consent_required")
        workflow: AgentRuns = request.app.state.agent_workflow
        paths: tuple[str, ...] = _call(
            workflow.accept, run_id=run_id, owner=principal.session_id,
            paths=payload.paths, confirmed=True,
        )
        return AgentAcceptedResponse(paths=list(paths))

    @router.post("/agent/runs/{run_id}/undo", response_model=AgentUndoneResponse)
    def undo(
        run_id: str, payload: UndoAgentChanges, request: Request,
        principal: SessionPrincipal = Depends(enabled),
    ) -> AgentUndoneResponse:
        if not payload.confirmed:
            raise _error("consent_required")
        workflow: AgentRuns = request.app.state.agent_workflow
        paths: tuple[str, ...] = _call(
            workflow.undo, run_id=run_id, owner=principal.session_id, confirmed=True,
        )
        return AgentUndoneResponse(paths=list(paths))

    @router.post("/agent/runs/{run_id}/discard", response_model=AgentDiscardedResponse)
    def discard(
        run_id: str, request: Request,
        principal: SessionPrincipal = Depends(enabled),
    ) -> AgentDiscardedResponse:
        workflow: AgentRuns = request.app.state.agent_workflow
        _call(workflow.discard, run_id=run_id, owner=principal.session_id)
        return AgentDiscardedResponse(discarded=True)

    @router.post("/agent/runs/{run_id}/resume", response_model=AgentRunResponse)
    def resume(
        run_id: str, request: Request,
        principal: SessionPrincipal = Depends(enabled),
    ) -> AgentRunResponse:
        workflow: AgentRuns = request.app.state.agent_workflow
        return _response(_call(workflow.resume, run_id=run_id, owner=principal.session_id),
                         workflow, principal.session_id)
