"""Explicit local qualification-profile lifecycle composition.

The normal Cortex application does not construct a runnable recipe lifecycle.
This module provides the deliberate development/CI seam for callers that have
already built the fixed worker, installed it with disposable qualification
trust, and supplied a coordinator that owns the worker boundary.

The profile is intentionally narrower than an official release profile:

* ``disabled`` is the default and never calls a health probe or coordinator;
* ``qualification`` requires the qualification release gate, an injected
  coordinator factory, and a provider-health probe; and
* missing or malformed qualification wiring becomes a blocked lifecycle, not a
  host-process fallback or an implicitly enabled provider.

This module does not create a process, bind a broker, load a provider, or read a
worker path. Those actions remain responsibilities of the injected controls.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from collections.abc import Callable
from typing import Literal

from .lifecycle import ExecutionLifecycle, LifecycleCoordinator, RuntimeHealth
from .release_gate import RecipeRuntimeReleaseGate
from .repository import ExecutionRepository


ExecutionProfile = Literal["disabled", "qualification"]
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
    lifecycle. Selecting ``qualification`` is explicit and still fails closed
    when its controls are absent or unhealthy. The returned lifecycle carries a
    safe profile label for diagnostics.
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
    "parse_execution_profile",
]
