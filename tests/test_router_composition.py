"""The router is assembled from per-resource modules; the seams must hold.

``build_router`` used to be one 1,592-line function. Splitting it moved route
declarations between modules, and FastAPI matches routes in declaration order --
so the split could in principle change which handler answers a request. These
tests pin the properties that make the arrangement safe.
"""

from __future__ import annotations

import re

import pytest

from cortex_backend.api.routers import _RESOURCES, build_router


def _routes():
    return [route for route in build_router().routes if getattr(route, "methods", None)]


def _pattern(path: str) -> re.Pattern[str]:
    parts = [
        "[^/]+" if token.startswith("{") else re.escape(token)
        for token in re.split(r"(\{[^}]+\})", path)
    ]
    return re.compile("^" + "".join(parts) + "$")


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "SAMPLE", path)


def test_no_route_is_shadowed_by_an_earlier_one() -> None:
    """The property the split has to preserve, checked directly.

    A literal path declared after a matching parameterised one is unreachable.
    Rather than assert a fixed order, this derives the answer from the routes
    themselves, so it keeps working as routes are added.
    """
    routes = _routes()
    shadowed = []
    for index, later in enumerate(routes):
        sample = _concrete(later.path)
        for earlier in routes[:index]:
            if earlier.path == later.path or not (earlier.methods & later.methods):
                continue
            if _pattern(earlier.path).match(sample):
                shadowed.append(
                    f"{sorted(later.methods)} {later.path} is unreachable behind {earlier.path}"
                )

    assert shadowed == []


def test_the_literal_execution_route_stays_ahead_of_the_parameterised_one() -> None:
    """The single order-sensitive pair in this API, named explicitly.

    ``GET /execution/tasks`` and ``GET /execution/{job_id}`` both match
    ``/execution/tasks``. They are in the same module so their relative order
    survives any regrouping, but the constraint deserves to fail loudly rather
    than be rediscovered.
    """
    paths = [route.path for route in _routes()]

    assert paths.index("/execution/tasks") < paths.index("/execution/{job_id}")


def test_every_route_is_registered_exactly_once() -> None:
    """A module registered twice, or a path landing in two groups, is a bug."""
    seen = [(route.path, tuple(sorted(route.methods))) for route in _routes()]

    assert len(seen) == len(set(seen))


def test_each_resource_module_registers_at_least_one_route() -> None:
    """An empty module means routes were dropped rather than moved."""
    from fastapi import APIRouter

    for resource in _RESOURCES:
        router = APIRouter()
        resource.register(
            router,
            require_session=lambda: None,
            dependencies=lambda: None,
        )
        registered = [r for r in router.routes if getattr(r, "methods", None)]
        assert registered, f"{resource.__name__} registered no routes"


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/session/exchange",
        "/system",
        "/chats",
        "/settings",
        "/memories",
        "/models",
        "/execution/tasks",
        "/generations",
        "/jobs/{job_id}",
    ],
)
def test_a_representative_route_from_every_resource_is_present(path: str) -> None:
    assert path in {route.path for route in _routes()}
