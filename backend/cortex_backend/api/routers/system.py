"""System capability reporting, shutdown, and diagnostics.

Registered by cortex_backend.api.routers.build_router. Shared helpers live
in cortex_backend.api.routes.
"""

from __future__ import annotations

from fastapi import APIRouter
from cortex_backend.api.app_types import BackendDependenciesProtocol
from cortex_backend.api.routes import (
    _llamacpp_status,
    _load_settings_result,
    _migration_response,
    _model_sets,
)
from cortex_backend.api.schemas import (
    DiagnosticsResponse,
    ShutdownResponse,
    SystemResponse,
)
from cortex_backend.api.security import SessionPrincipal
from fastapi import (
    Depends,
    HTTPException,
    Request,
)


def register(router: APIRouter, *, require_session, dependencies) -> None:
    """Attach the system routes to ``router``."""


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
