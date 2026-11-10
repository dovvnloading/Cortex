"""Authenticated, ephemeral local coding workspace endpoints.

Deliberately excluded from the generated public v1 contract while this
consent-gated desktop feature is experimental. No route is anonymously callable.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ConfigDict, BaseModel, Field

from cortex_backend.api.security import SessionPrincipal
from cortex_backend.services.workspace_context import WorkspaceAccessError
from cortex_backend.services.workspace_patch import WorkspacePatchError
from cortex_backend.services.workspace_workflow import WorkspaceWorkflowError


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenWorkspace(RequestModel):
    root_path: str = Field(min_length=1, max_length=4096)
    confirmed: bool = False


class SearchWorkspace(RequestModel):
    query: str = Field(min_length=2, max_length=120)


class ReadWorkspace(RequestModel):
    path: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(default=1, ge=1)


class AskWorkspace(RequestModel):
    question: str = Field(min_length=1, max_length=20000)
    model: str = Field(min_length=1, max_length=256)
    preview_edits: bool = False


class ReplaceWorkspace(RequestModel):
    path: str = Field(min_length=1, max_length=1024)
    expected_sha256: str = Field(min_length=64, max_length=64)
    old_text: str = Field(min_length=1, max_length=8000)
    new_text: str = Field(max_length=8000)


class ConfirmWorkspace(RequestModel):
    confirmed: bool = False


class CheckWorkspace(ConfirmWorkspace):
    command: str = Field(min_length=1, max_length=64)


_ERRORS = {
    "consent_required": (403, "Explicit approval is required."),
    "unavailable": (404, "That workspace has expired or was closed. Open it again."),
    "busy": (409, "This workspace already has an operation running."),
    "stale_source": (409, "A source file changed. Reopen the workspace before editing."),
    "stale_root": (409, "The project folder changed. Reopen the workspace."),
    "linked_path": (409, "A source path is now linked or unsafe."),
    "reopen_required": (409, "Reopen the workspace to snapshot updated files."),
    "no_patch": (409, "There is no proposed patch to apply."),
    "no_checkpoint": (409, "There are no applied changes to undo."),
    "partial_rollback": (409, "Some edits could not be rolled back. Review the checkout immediately."),
    "write_failed": (409, "The patch could not be written; completed writes were rolled back."),
    "restore_failed": (409, "Undo stopped partway through. Review the remaining checkpoint."),
    "containment_unavailable": (409, "The Windows process limit could not be established."),
    "runner_unavailable": (409, "The chosen test/build runner is not installed on PATH."),
    "invalid_command": (422, "Only the listed test and build commands are permitted."),
}


def _call(function: Callable[..., Any], **kwargs: Any) -> Any:
    try:
        return function(**kwargs)
    except (WorkspaceAccessError, WorkspacePatchError, WorkspaceWorkflowError) as exc:
        status, message = _ERRORS.get(exc.code, (422, "That workspace request was rejected."))
        raise HTTPException(
            status_code=status, detail={"code": exc.code, "message": message}
        ) from None


def register(router: APIRouter, *, require_session, dependencies) -> None:
    del dependencies
    # All methods and requests use the same bearer-session dependency as chat.
    @router.post("/workspaces", include_in_schema=False)
    def open_workspace(
        payload: OpenWorkspace, request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Any:
        return _call(
            request.app.state.workspace_workflow.open,
            owner=principal.installation_principal_id,
            root_path=payload.root_path,
            confirmed=payload.confirmed,
        )

    @router.get("/workspaces/{session_id}/paths", include_in_schema=False)
    def paths(
        session_id: str, request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Any:
        return {"paths": _call(
            request.app.state.workspace_workflow.paths,
            owner=principal.installation_principal_id, session_id=session_id,
        )}

    @router.post("/workspaces/{session_id}/search", include_in_schema=False)
    def search(
        session_id: str, payload: SearchWorkspace, request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Any:
        return {"matches": _call(
            request.app.state.workspace_workflow.search,
            owner=principal.installation_principal_id, session_id=session_id,
            query=payload.query,
        )}

    @router.post("/workspaces/{session_id}/read", include_in_schema=False)
    def read(
        session_id: str, payload: ReadWorkspace, request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Any:
        return _call(
            request.app.state.workspace_workflow.read,
            owner=principal.installation_principal_id, session_id=session_id,
            path=payload.path, start_line=payload.start_line,
        )

    @router.post("/workspaces/{session_id}/ask", include_in_schema=False)
    def ask(
        session_id: str, payload: AskWorkspace, request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Any:
        chat_client = request.app.state.workspace_chat_client
        if chat_client is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "model_unavailable", "message": "A local Ollama model connection is required."},
            )
        return _call(
            request.app.state.workspace_workflow.ask,
            owner=principal.installation_principal_id, session_id=session_id,
            model=payload.model, question=payload.question,
            preview_edits=payload.preview_edits, chat_client=chat_client,
        )

    @router.post("/workspaces/{session_id}/replace", include_in_schema=False)
    def replace(
        session_id: str, payload: ReplaceWorkspace, request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Any:
        return _call(
            request.app.state.workspace_workflow.replace,
            owner=principal.installation_principal_id, session_id=session_id,
            path=payload.path, expected_sha256=payload.expected_sha256,
            old_text=payload.old_text, new_text=payload.new_text,
        )

    @router.get("/workspaces/{session_id}/preview", include_in_schema=False)
    def preview(
        session_id: str, request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Any:
        return _call(
            request.app.state.workspace_workflow.preview,
            owner=principal.installation_principal_id, session_id=session_id,
        )

    @router.post("/workspaces/{session_id}/apply", include_in_schema=False)
    def apply(
        session_id: str, payload: ConfirmWorkspace, request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Any:
        return _call(
            request.app.state.workspace_workflow.apply,
            owner=principal.installation_principal_id, session_id=session_id,
            confirmed=payload.confirmed,
        )

    @router.post("/workspaces/{session_id}/undo", include_in_schema=False)
    def undo(
        session_id: str, payload: ConfirmWorkspace, request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Any:
        return _call(
            request.app.state.workspace_workflow.undo,
            owner=principal.installation_principal_id, session_id=session_id,
            confirmed=payload.confirmed,
        )

    @router.post("/workspaces/{session_id}/checks", include_in_schema=False)
    def check(
        session_id: str, payload: CheckWorkspace, request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Any:
        return _call(
            request.app.state.workspace_workflow.check,
            owner=principal.installation_principal_id, session_id=session_id,
            confirmed=payload.confirmed, command=payload.command,
        )

    @router.post("/workspaces/{session_id}/cancel", include_in_schema=False)
    def cancel(
        session_id: str, request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Any:
        return _call(
            request.app.state.workspace_workflow.cancel,
            owner=principal.installation_principal_id, session_id=session_id,
        )

    @router.delete("/workspaces/{session_id}", include_in_schema=False)
    def revoke(
        session_id: str, request: Request,
        principal: SessionPrincipal = Depends(require_session),
    ) -> Any:
        _call(
            request.app.state.workspace_workflow.revoke,
            owner=principal.installation_principal_id, session_id=session_id,
        )
        return {"revoked": True}
