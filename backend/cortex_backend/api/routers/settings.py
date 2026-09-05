"""Stored settings and permanent memories.

Registered by cortex_backend.api.routers.build_router. Shared helpers live
in cortex_backend.api.routes.
"""

from __future__ import annotations

from fastapi import APIRouter
from cortex_backend.api.app_types import BackendDependenciesProtocol
from cortex_backend.api.routes import (
    _migration_response,
    _raise_repository_error,
)
from cortex_backend.api.schemas import (
    AddMemoryRequest,
    ClearMemoryRequest,
    MemoryResponse,
    ReplaceMemoryRequest,
    SettingsResponse,
    SettingsUpdateRequest,
)
from cortex_backend.api.security import SessionPrincipal
from cortex_backend.repositories.settings import SettingsRevisionConflict
from fastapi import (
    Depends,
    HTTPException,
    status,
)


def register(router: APIRouter, *, require_session, dependencies) -> None:
    """Attach the settings routes to ``router``."""


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
            expected_revision = payload.expected_revision
            if expected_revision is None:
                # Older clients only sent the revision embedded in the snapshot.
                expected_revision = payload.settings.revision
            updated = payload.settings.model_copy(
                update={"revision": expected_revision + 1}
            )
            deps.settings.save(updated, expected_revision=expected_revision)
            loaded = deps.settings.load()
        except SettingsRevisionConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
        except ValueError as exc:
            # Reaching the memory limit is an expected outcome the user can
            # act on, not a server fault. Report it as one, with the reason.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Clearing permanent memories requires explicit confirmation.",
            )
        if payload.confirmation_intent not in (None, "clear_permanent_memory"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="A clear-memory confirmation intent is required.",
            )
        try:
            deps.memories.clear_memos()
            return MemoryResponse(memos=deps.memories.get_memos())
        except Exception as exc:
            _raise_repository_error("clear memories", exc)
