"""Liveness, readiness, and the launcher session exchange.

Registered by cortex_backend.api.routers.build_router. Shared helpers live
in cortex_backend.api.routes.
"""

from __future__ import annotations

from fastapi import APIRouter
import hmac
from cortex_backend.api.routes import _runtime_is_ready
from cortex_backend.api.schemas import (
    HandoffResponse,
    HealthResponse,
    SessionExchangeRequest,
    SessionExchangeResponse,
)
from cortex_backend.api.security import SessionSecurityError
from fastapi import (
    HTTPException,
    Header,
    Request,
    status,
)


def register(router: APIRouter, *, require_session, dependencies) -> None:
    """Attach the session routes to ``router``."""


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
    def handoff(
        request: Request,
        handoff_secret: str | None = Header(
            default=None,
            alias="X-Cortex-Handoff",
            description="One-time local launcher handoff secret.",
        ),
    ) -> HandoffResponse:
        manager = request.app.state.session_manager
        manager.validate_request_context(request)
        supplied = handoff_secret or ""
        expected = request.app.state.handoff_secret
        if not expected or not hmac.compare_digest(
            supplied.encode("latin-1"), expected.encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="Cortex handoff unavailable.")
        token, expires_at = manager.issue_bootstrap_token()
        return HandoffResponse(bootstrap_token=token, expires_at=expires_at)
