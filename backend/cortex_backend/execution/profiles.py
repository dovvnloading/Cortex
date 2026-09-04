"""Execution-profile lifecycle composition.

Two profiles, and that is the whole set:

* ``disabled`` never calls a health probe or builds a coordinator; and
* ``local`` builds the checked-in process-backed workers -- safe computation,
  approval-gated code, and the fixed image recipe -- with no external signer,
  reviewer, or broker.

There used to be a third, ``qualification``, for callers that had already built
a code-signed native worker and could supply its release controls. Nothing ever
built that worker, so the profile could only ever fail closed, and the modules
behind it never executed. Both are gone; this module is what is left.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from .lifecycle import ExecutionLifecycle, LifecycleCoordinator, RuntimeHealth
from .repository import ExecutionRepository

ExecutionProfile = Literal["disabled", "local"]
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class ExecutionProfileError(ValueError):
    """Stable configuration error for an explicit profile selection."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid execution profile code")
        self.code = code
        super().__init__("The execution profile configuration is invalid.")


ProviderHealthProbe = Callable[[RuntimeHealth], RuntimeHealth]
CoordinatorFactory = Callable[[ExecutionRepository], LifecycleCoordinator]


def parse_execution_profile(value: str | None) -> ExecutionProfile:
    """Accept only an exact profile name; anything else fails closed."""
    if value is None:
        return "disabled"
    if not isinstance(value, str):
        raise ExecutionProfileError("execution_profile_invalid")
    if value == "disabled":
        return "disabled"
    if value == "local":
        return "local"
    raise ExecutionProfileError("execution_profile_unknown")


def _unconfigured_factory(_repository: ExecutionRepository) -> LifecycleCoordinator:
    raise ExecutionProfileError("execution_profile_unconfigured")


def _disabled_health() -> RuntimeHealth:
    return RuntimeHealth.blocked(
        code="runtime_disabled",
        message="Execution runtime is disabled in this build.",
    )


def _local_health() -> RuntimeHealth:
    """The local profile always retains its safe-compute fallback.

    Image support performs a second dependency probe in the local coordinator.
    A missing optional image codec therefore disables only image transforms,
    never normal chat or safe computation.
    """

    return RuntimeHealth.ready("Local safe-compute runtime is ready.")


def build_execution_lifecycle(
    repository: ExecutionRepository,
    *,
    profile: str | None = None,
) -> ExecutionLifecycle:
    """Build the lifecycle for the selected profile.

    ``profile=None`` and ``profile="disabled"`` construct the same inert
    lifecycle. ``local`` starts the checked-in bounded worker profiles.
    """
    selected = parse_execution_profile(profile)
    if selected == "disabled":
        return ExecutionLifecycle(
            repository,
            coordinator_factory=_unconfigured_factory,
            health_check=_disabled_health,
            enabled=False,
            profile="disabled",
        )

    # Imported here so the disabled seam stays lightweight for CI and docs.
    from .local_runtime import LocalExecutionCoordinator

    return ExecutionLifecycle(
        repository,
        coordinator_factory=lambda target: LocalExecutionCoordinator(target),
        health_check=_local_health,
        enabled=True,
        profile="local",
    )


__all__ = [
    "CoordinatorFactory",
    "ExecutionProfile",
    "ExecutionProfileError",
    "ProviderHealthProbe",
    "build_execution_lifecycle",
    "parse_execution_profile",
]
