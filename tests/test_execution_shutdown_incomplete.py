"""A shutdown that times out with work still running is not a clean stop (#618).

Shutdown joined its worker threads with a deadline and then released the
supervisor lease and returned normally whether or not they had exited, so
ExecutionLifecycle dropped the coordinator and would build a second one over
the same store while the first one's worker was still unwinding.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, Thread

import pytest

from cortex_backend.execution.lifecycle import ExecutionLifecycle, RuntimeHealth
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.recipe_coordinator import ExecutionShutdownIncomplete
from cortex_backend.execution.repository import ExecutionRepository, LeaseConflict


def _stuck_worker(coordinator: LocalExecutionCoordinator, release: Event) -> Thread:
    """A scratch worker thread that ignores Stop until ``release`` is set."""

    thread = Thread(target=lambda: release.wait(10), name="stuck-scratch-worker", daemon=True)
    with coordinator._scratch_lock:
        coordinator._scratch_threads["stuck-job"] = thread
    thread.start()
    return thread


def _supervisor_is_held(repository: ExecutionRepository) -> bool:
    try:
        repository.claim_supervisor_lease(lease_owner="someone-else", ttl_seconds=60)
    except LeaseConflict:
        return True
    repository.release_supervisor_lease(lease_owner="someone-else")
    return False


def test_shutdown_with_a_stuck_worker_keeps_ownership_and_can_be_retried(
    coordinator_factory: Callable[..., LocalExecutionCoordinator],
) -> None:
    coordinator = coordinator_factory()
    coordinator.startup_recover()
    release = Event()
    worker = _stuck_worker(coordinator, release)

    with pytest.raises(ExecutionShutdownIncomplete):
        coordinator.shutdown(timeout=0.1)

    assert _supervisor_is_held(coordinator.repository)

    release.set()
    worker.join(timeout=5)
    with coordinator._scratch_lock:
        coordinator._scratch_threads.pop("stuck-job", None)
    coordinator.shutdown(timeout=1)

    assert not _supervisor_is_held(coordinator.repository)


def test_the_lifecycle_will_not_replace_a_coordinator_that_is_still_stopping(
    execution_repository: ExecutionRepository,
) -> None:
    built: list[LocalExecutionCoordinator] = []

    def factory(repository: ExecutionRepository) -> LocalExecutionCoordinator:
        coordinator = LocalExecutionCoordinator(repository)
        built.append(coordinator)
        return coordinator

    lifecycle = ExecutionLifecycle(
        execution_repository,
        coordinator_factory=factory,
        health_check=RuntimeHealth.ready,
        enabled=True,
    )
    assert lifecycle.start().state == "ready"
    release = Event()
    worker = _stuck_worker(built[0], release)
    built[0].shutdown = lambda timeout=0.1: LocalExecutionCoordinator.shutdown(  # type: ignore[method-assign]
        built[0], timeout=timeout
    )

    try:
        assert lifecycle.stop().state == "blocked"
        assert lifecycle.start().state == "blocked"
        assert len(built) == 1  # no second coordinator over the same store
    finally:
        release.set()
        worker.join(timeout=5)
        with built[0]._scratch_lock:
            built[0]._scratch_threads.pop("stuck-job", None)

    assert lifecycle.stop().state == "stopped"
    assert lifecycle.start().state == "ready"
    assert len(built) == 2
    lifecycle.stop()
