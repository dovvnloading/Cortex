"""The capability protocols and the shipped coordinators stay in agreement.

The execution routes decide whether a capability is reachable by testing the
coordinator against a protocol in ``execution/lifecycle.py``. Those protocols
are the only place the member names are written down, and a mismatch fails the
quiet way: the route stops matching and answers 404, as if the feature were
switched off.

These tests make the mismatch loud. If a coordinator method is renamed without
updating its protocol -- or the reverse -- one of them fails here rather than a
capability silently disappearing from a shipped build.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex_backend.execution.lifecycle import (
    CodeCapable,
    RecipeCapable,
    ScratchCapable,
)
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.recipe_coordinator import RecipeExecutionCoordinator
from cortex_backend.execution.repository import ExecutionRepository


@pytest.fixture
def repository(tmp_path: Path) -> ExecutionRepository:
    return ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")


def test_the_local_runtime_satisfies_every_capability_protocol(repository):
    coordinator = LocalExecutionCoordinator(repository)
    try:
        assert isinstance(coordinator, ScratchCapable)
        assert isinstance(coordinator, CodeCapable)
        assert isinstance(coordinator, RecipeCapable)
    finally:
        coordinator.shutdown()


def test_the_recipe_coordinator_is_recipe_capable_and_nothing_else(repository):
    coordinator = RecipeExecutionCoordinator(repository, lambda _job: None)
    try:
        assert isinstance(coordinator, RecipeCapable)
        # It has no scratch or code surface, so those routes must refuse it.
        assert not isinstance(coordinator, ScratchCapable)
        assert not isinstance(coordinator, CodeCapable)
    finally:
        coordinator.shutdown()


def test_a_coordinator_missing_one_member_fails_its_protocol():
    class HalfScratch:
        scratch_available = True
        # start_scratch deliberately absent -- a rename looks exactly like this.

    assert not isinstance(HalfScratch(), ScratchCapable)


def _declared_members(protocol: type) -> set[str]:
    """The protocol's own surface, without relying on a CPython internal.

    ``__protocol_attrs__`` would say this in one line, but it only exists from
    3.12 and this project supports 3.10. Annotations cover the data members and
    the class body covers the methods.
    """

    members = set(getattr(protocol, "__annotations__", {}))
    members |= {
        name
        for name, value in vars(protocol).items()
        if callable(value) and not name.startswith("_")
    }
    return members


@pytest.mark.parametrize(
    ("protocol", "members"),
    [
        (ScratchCapable, ("scratch_available", "start_scratch")),
        (CodeCapable, ("code_execution_available", "start_code")),
        (RecipeCapable, ("artifact_boundary", "start_image_transform")),
    ],
)
def test_each_protocol_pins_the_members_the_routes_rely_on(protocol, members):
    """Pin the surface itself, so widening a protocol is a deliberate edit."""

    assert _declared_members(protocol) == set(members)
