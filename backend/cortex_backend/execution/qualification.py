"""Explicit execution-profile lifecycle composition.

The normal application selects the checked-in ``local`` profile, which builds
the small process-backed scratch and fixed-image workers. ``qualification``
remains a deliberate development/CI seam for callers that have already built
the signed native worker and supplied its release controls.

The profiles are intentionally distinct:

* ``disabled`` never calls a health probe or coordinator;
* ``local`` has no external signer, reviewer, or broker requirement; and
* ``qualification`` requires its release gate, injected coordinator factory,
  and provider-health probe.

Missing or malformed qualification wiring becomes a blocked lifecycle, not a
host-process fallback. The qualification helpers themselves do not create a
process, bind a broker, load a provider, or read a worker path; the local
profile owns its checked-in worker composition separately.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from collections.abc import Callable
from typing import Literal

from .artifact_boundary import ArtifactBoundary
from .lifecycle import ExecutionLifecycle, LifecycleCoordinator, RuntimeHealth
from .recipe_coordinator import (
    RecipeExecutionCoordinator,
    RecipeWorkerAttemptFactory,
)
from .native_launcher import NativeProcessFactory, NativeWorkerPolicy
from .native_recipe_attempt import build_native_recipe_worker_attempt_factory
from .bundle_installer import SignedBundleInstaller
from .release_gate import RecipeRuntimeReleaseGate
from .repository import ExecutionRepository


ExecutionProfile = Literal["disabled", "local", "qualification"]
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class QualificationProfileError(ValueError):
    """Stable configuration error for an explicit local profile selection."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid qualification profile code")
        self.code = code
        super().__init__("The local qualification profile configuration is invalid.")


ProviderHealthProbe = Callable[[RuntimeHealth], RuntimeHealth]
CoordinatorFactory = Callable[[ExecutionRepository], LifecycleCoordinator]


def build_recipe_coordinator_factory(
    worker_attempt_factory: RecipeWorkerAttemptFactory,
    *,
    artifact_boundary_factory: Callable[[ExecutionRepository], ArtifactBoundary] | None = None,
    lease_seconds: float = 30.0,
    supervisor_lease_seconds: float = 30.0,
) -> CoordinatorFactory:
    """Bind a qualified worker-attempt seam to the durable recipe coordinator.

    The caller must provide the already-qualified attempt factory. This helper
    does not launch a process, bind a broker, load a provider, or fall back to
    host execution. It only creates a coordinator for the repository supplied
    by :class:`ExecutionLifecycle`, keeping lifecycle ownership explicit.
    """

    if not callable(worker_attempt_factory):
        raise TypeError("worker_attempt_factory must be callable")
    if artifact_boundary_factory is not None and not callable(artifact_boundary_factory):
        raise TypeError("artifact_boundary_factory must be callable")
    if lease_seconds <= 0 or supervisor_lease_seconds <= 0:
        raise ValueError("lease durations must be positive")

    def factory(repository: ExecutionRepository) -> LifecycleCoordinator:
        artifact_boundary = (
            artifact_boundary_factory(repository)
            if artifact_boundary_factory is not None
            else None
        )
        if artifact_boundary is not None and not isinstance(artifact_boundary, ArtifactBoundary):
            raise TypeError("artifact_boundary_factory returned an invalid boundary")
        if artifact_boundary is not None and artifact_boundary.repository is not repository:
            raise ValueError("artifact boundary repository mismatch")
        return RecipeExecutionCoordinator(
            repository,
            worker_attempt_factory,
            artifact_boundary=artifact_boundary,
            lease_seconds=lease_seconds,
            supervisor_lease_seconds=supervisor_lease_seconds,
            auto_recover=False,
        )

    return factory


def build_native_recipe_coordinator_factory(
    installer: SignedBundleInstaller,
    *,
    allowed_user_sids: frozenset[str],
    process_factory_factory: Callable[[], NativeProcessFactory],
    policy: NativeWorkerPolicy | None = None,
    accept_timeout_seconds: float = 15.0,
    worker_timeout_seconds: float = 120.0,
    cancel_grace_seconds: float = 5.0,
    artifact_boundary_factory: Callable[[ExecutionRepository], ArtifactBoundary] | None = None,
    lease_seconds: float = 30.0,
    supervisor_lease_seconds: float = 30.0,
) -> CoordinatorFactory:
    """Bind the signed/native attempt factory to the lifecycle repository.

    The process factory is deliberately required as an explicit composition
    input.  This helper creates no process while configuring the lifecycle and
    has no host-process or alternate-transport fallback.
    """

    worker_factory = build_native_recipe_worker_attempt_factory(
        installer,
        allowed_user_sids=allowed_user_sids,
        process_factory_factory=process_factory_factory,
        policy=policy,
        accept_timeout_seconds=accept_timeout_seconds,
        worker_timeout_seconds=worker_timeout_seconds,
        cancel_grace_seconds=cancel_grace_seconds,
    )
    return build_recipe_coordinator_factory(
        worker_factory,
        artifact_boundary_factory=artifact_boundary_factory,
        lease_seconds=lease_seconds,
        supervisor_lease_seconds=supervisor_lease_seconds,
    )


@dataclass(frozen=True, slots=True)
class QualificationLifecycleConfig:
    """Controls required before the local qualification lifecycle can start."""

    release_gate: RecipeRuntimeReleaseGate
    coordinator_factory: CoordinatorFactory
    provider_health_check: ProviderHealthProbe

    def __post_init__(self) -> None:
        if not isinstance(self.release_gate, RecipeRuntimeReleaseGate):
            raise TypeError("qualification release gate is required")
        if self.release_gate.release_profile != "qualification":
            raise QualificationProfileError("qualification_gate_required")
        if not callable(self.coordinator_factory):
            raise TypeError("qualification coordinator factory must be callable")
        if not callable(self.provider_health_check):
            raise TypeError("qualification provider health check must be callable")


def parse_execution_profile(value: str | None) -> ExecutionProfile:
    """Parse an explicit profile without accepting aliases or whitespace."""

    if value is None:
        return "disabled"
    if value == "disabled":
        return "disabled"
    if value == "local":
        return "local"
    if value == "qualification":
        return "qualification"
    raise QualificationProfileError("execution_profile_invalid")


def _unconfigured_factory(_repository: ExecutionRepository) -> LifecycleCoordinator:
    raise RuntimeError("qualification coordinator is not configured")


def _disabled_health() -> RuntimeHealth:
    return RuntimeHealth.blocked(
        code="runtime_disabled",
        message="Execution runtime is disabled in this build.",
    )


def _qualification_configuration_health() -> RuntimeHealth:
    return RuntimeHealth.blocked(
        code="qualification_configuration_missing",
        message="The local qualification runtime is not configured.",
    )


def _local_health() -> RuntimeHealth:
    """The local profile always retains its safe-compute fallback.

    Image support performs a second dependency probe in the local coordinator.
    A missing optional image codec therefore disables only image transforms,
    never normal chat or safe computation.
    """

    return RuntimeHealth.ready("Local safe-compute runtime is ready.")


def _qualification_health(config: QualificationLifecycleConfig) -> RuntimeHealth:
    """Compose release and provider health without leaking internal details."""

    try:
        release_health = config.release_gate.health()
    except Exception:
        return RuntimeHealth.blocked(
            code="qualification_gate_failed",
            message="The local qualification controls could not be verified safely.",
        )
    if not isinstance(release_health, RuntimeHealth):
        return RuntimeHealth.blocked(
            code="qualification_gate_result_invalid",
            message="The local qualification controls returned an invalid result.",
        )
    if not release_health.available:
        return release_health
    try:
        provider_health = config.provider_health_check(release_health)
    except Exception:
        return RuntimeHealth.blocked(
            code="qualification_provider_health_failed",
            message="The fixed-function provider could not be verified safely.",
        )
    if not isinstance(provider_health, RuntimeHealth):
        return RuntimeHealth.blocked(
            code="qualification_provider_health_invalid",
            message="The fixed-function provider returned an invalid health result.",
        )
    if not provider_health.available:
        return provider_health
    return RuntimeHealth.ready("Local recipe qualification controls are ready.")


def build_execution_lifecycle(
    repository: ExecutionRepository,
    *,
    profile: str | None = None,
    qualification: QualificationLifecycleConfig | None = None,
) -> ExecutionLifecycle:
    """Build the only supported local profile boundary.

    ``profile=None`` and ``profile="disabled"`` construct the same inert
    lifecycle. ``local`` starts the checked-in bounded worker profiles with no
    external signing or reviewer service. Selecting ``qualification`` remains
    explicit and still fails closed when its additional controls are absent or
    unhealthy. The returned lifecycle carries a safe profile label for
    diagnostics.
    """

    selected = parse_execution_profile(profile)
    if selected == "disabled":
        if qualification is not None:
            raise QualificationProfileError("qualification_config_with_disabled_profile")
        return ExecutionLifecycle(
            repository,
            coordinator_factory=_unconfigured_factory,
            health_check=_disabled_health,
            enabled=False,
            profile="disabled",
        )

    if selected == "local":
        if qualification is not None:
            raise QualificationProfileError("qualification_config_with_local_profile")
        # Import only when the normal local profile is requested. The disabled
        # and qualification seams stay lightweight for documentation and CI.
        from .local_runtime import LocalExecutionCoordinator

        return ExecutionLifecycle(
            repository,
            coordinator_factory=lambda target: LocalExecutionCoordinator(target),
            health_check=_local_health,
            enabled=True,
            profile="local",
        )

    if qualification is None:
        return ExecutionLifecycle(
            repository,
            coordinator_factory=_unconfigured_factory,
            health_check=_qualification_configuration_health,
            enabled=True,
            profile="qualification",
        )
    return ExecutionLifecycle(
        repository,
        coordinator_factory=qualification.coordinator_factory,
        health_check=lambda: _qualification_health(qualification),
        enabled=True,
        profile="qualification",
    )


__all__ = [
    "CoordinatorFactory",
    "ExecutionProfile",
    "ProviderHealthProbe",
    "QualificationLifecycleConfig",
    "QualificationProfileError",
    "build_execution_lifecycle",
    "build_native_recipe_coordinator_factory",
    "build_recipe_coordinator_factory",
    "parse_execution_profile",
]
