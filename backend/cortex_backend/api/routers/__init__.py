"""The versioned local API, assembled from one module per resource.

``build_router`` used to be a single 1,592-line function holding all 53 routes
and 65 nested definitions. Nothing could be read, tested, or reviewed in
isolation, and every endpoint change touched the same function.

Ordering note: FastAPI matches routes in declaration order, so a split could in
principle change which handler answers a request. Exactly one pair in this API
is order-sensitive -- ``GET /execution/tasks`` and ``GET /execution/{job_id}``,
where the literal must be declared first -- and both live in ``execution``, so
their relative order is preserved by construction. ``test_router_composition``
pins that, and the full route table is asserted to be unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cortex_backend.api.app_types import BackendDependenciesProtocol
from cortex_backend.api.security import SessionPrincipal

from . import chats, execution, generations, models, session, settings, system


# Registration order is the declaration order FastAPI matches on.
_RESOURCES = (session, system, chats, settings, models, execution, generations)


def build_router() -> APIRouter:
    router = APIRouter()
    session_bearer = HTTPBearer(
        auto_error=False,
        scheme_name="CortexSession",
        description=(
            "Short-lived bearer session token returned by /session/exchange. "
            "Requests remain restricted to the local API host."
        ),
    )

    def require_session(
        request: Request,
        _credentials: HTTPAuthorizationCredentials | None = Security(session_bearer),  # noqa: B008
    ) -> SessionPrincipal:
        return request.app.state.session_manager.require(request)

    def dependencies(request: Request) -> BackendDependenciesProtocol:
        return request.app.state.dependencies

    for resource in _RESOURCES:
        resource.register(router, require_session=require_session, dependencies=dependencies)
    return router
