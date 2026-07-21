"""Health-gated execution lifecycle orchestration.

This module owns the production application's control-plane lifecycle without
authorizing a runtime provider. A provider factory is injected only by an
explicitly qualified build; unavailable or failed health checks leave the app
usable for ordinary chat while execution remains absent from the API surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Callable, Literal, Protocol

from .repository import ExecutionRepository


LifecycleState = Literal["disabled", "stopped", "starting", "ready", "blocked", "stopping"]
_SAFE_HEALTH_CODE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_LOGGER = logging.getLogger("cortex.execution.lifecycle")


class LifecycleCoordinator(Protocol):
    """Minimal coordinator surface required by the app lifecycle."""

    repository: ExecutionRepository

    def startup_recover(self) -> list[str]:
        """Claim the supervisor lease and recover safe persisted work."""

    def shutdown(self) -> None:
        """Cancel owned work and release lifecycle resources."""


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    """Safe health result returned by a qualified execution runtime probe."""

    available: bool
    code: str
    message: str

    def __post_init__(self) -> None:
        if _SAFE_HEALTH_CODE.fullmatch(self.code) is None:
            raise ValueError("runtime health code must be a bounded lowercase identifier")
        if not self.message or len(self.message) > 240:
            raise ValueError("runtime health message must be bounded and non-empty")

    @classmethod
    def ready(cls, message: str = "Execution runtime is ready.") -> "RuntimeHealth":
        return cls(available=True, code="ready", message=message)

    @classmethod
    def blocked(
        cls,
        code: str = "runtime_unavailable",
        message: str = "Execution runtime is unavailable.",
    ) -> "RuntimeHealth":
        return cls(available=False, code=code, message=message)


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    """Public-safe lifecycle state for diagnostics and tests."""

    state: LifecycleState
    health: RuntimeHealth
    recovered_job_ids: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.state == "ready" and self.health.available


class ExecutionLifecycle:
    """Start, recover, and stop a qualified coordinator behind a health gate."""

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        coordinator_factory: Callable[[ExecutionRepository], LifecycleCoordinator],
        health_check: Callable[[], RuntimeHealth],
        enabled: bool = False,
    ) -> None:
        self.repository = repository
        self._coordinator_factory = coordinator_factory
        self._health_check = health_check
        self._enabled = enabled
        self._coordinator: LifecycleCoordinator | None = None
        self._recovered_job_ids: tuple[str, ...] = ()
        self._health = (
            RuntimeHealth.blocked(
                code="runtime_disabled",
                message="Execution runtime is disabled in this build.",
            )
            if not enabled
            else RuntimeHealth.blocked(
                code="runtime_not_started",
                message="Execution runtime has not started.",
            )
        )
        self._state: LifecycleState = "disabled" if not enabled else "stopped"

    @property
    def snapshot(self) -> LifecycleSnapshot:
        return LifecycleSnapshot(
            state=self._state,
            health=self._health,
            recovered_job_ids=self._recovered_job_ids,
        )

    @property
    def coordinator(self) -> LifecycleCoordinator | None:
        """Expose a coordinator only while the lifecycle is fully ready."""
        return self._coordinator if self.snapshot.available else None

    def start(self) -> LifecycleSnapshot:
        """Run health, construction, and recovery; never downgrade on failure."""
        if not self._enabled:
            return self.snapshot
        if self._state == "ready":
            return self.snapshot
        if self._state == "starting":
            return self.snapshot
        self._state = "starting"
        self._recovered_job_ids = ()
        coordinator: LifecycleCoordinator | None = None
        try:
            health = self._health_check()
            self._health = health
            if not health.available:
                self._state = "blocked"
                return self.snapshot
            coordinator = self._coordinator_factory(self.repository)
            if coordinator.repository is not self.repository:
                raise RuntimeError("execution coordinator repository mismatch")
            recovered = coordinator.startup_recover()
            self._coordinator = coordinator
            self._recovered_job_ids = tuple(recovered)
            self._state = "ready"
            return self.snapshot
        except Exception as exc:
            self._coordinator = None
            self._recovered_job_ids = ()
            if coordinator is not None:
                try:
                    coordinator.shutdown()
                except Exception as cleanup_exc:
                    _LOGGER.error(
                        "Execution lifecycle startup cleanup failed (%s).",
                        type(cleanup_exc).__name__,
                    )
            self._health = RuntimeHealth.blocked(
                code="runtime_start_failed",
                message="Execution runtime could not start safely.",
            )
            self._state = "blocked"
            _LOGGER.warning("Execution lifecycle start failed (%s).", type(exc).__name__)
            return self.snapshot

    def stop(self) -> LifecycleSnapshot:
        """Stop the coordinator exactly once and leave execution unavailable."""
        if self._state in {"disabled", "stopped"}:
            return self.snapshot
        self._state = "stopping"
        coordinator = self._coordinator
        self._coordinator = None
        if coordinator is None:
            self._state = "stopped"
            return self.snapshot
        try:
            coordinator.shutdown()
        except Exception as exc:
            self._health = RuntimeHealth.blocked(
                code="runtime_stop_failed",
                message="Execution runtime stopped with an incomplete cleanup.",
            )
            self._state = "blocked"
            _LOGGER.error("Execution lifecycle stop failed (%s).", type(exc).__name__)
            return self.snapshot
        self._state = "stopped"
        self._health = RuntimeHealth.blocked(
            code="runtime_stopped",
            message="Execution runtime is stopped.",
        )
        return self.snapshot
