"""Installed models, pulls, and GGUF acquisition.

Registered by cortex_backend.api.routers.build_router. Shared helpers live
in cortex_backend.api.routes.
"""

from __future__ import annotations

from fastapi import APIRouter
from cortex_backend.api.app_types import BackendDependenciesProtocol
from cortex_backend.api.jobs import (
    JobConflict,
    JobRegistryClosed,
)
from cortex_backend.api.routes import (
    _accepted,
    _durable_owner,
    _gguf_directory,
    _load_settings,
    _model_response,
    _model_sets,
)
from cortex_backend.api.schemas import (
    HuggingFaceFileListResponse,
    JobAccepted,
    ModelDownloadRequest,
    ModelPullRequest,
    ModelResponse,
)
from cortex_backend.api.security import SessionPrincipal
from cortex_backend.llamacpp.download import (
    DownloadSource,
    GGUFDownloadError,
    download_gguf,
    list_huggingface_gguf_files,
    resolve_download_url,
)
from cortex_backend.services.models import ModelPullProgress
from dataclasses import asdict
from fastapi import (
    Depends,
    HTTPException,
    Request,
    status,
)


def register(router: APIRouter, *, require_session, dependencies) -> None:
    """Attach the models routes to ``router``."""


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
                owner=_durable_owner(principal),
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
                owner=_durable_owner(principal),
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
                owner=_durable_owner(principal),
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
