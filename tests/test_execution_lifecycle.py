"""Health-gated production lifecycle and recovery integration tests."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.testing import DurableFakeCoordinator
from cortex_backend.execution.lifecycle import ExecutionLifecycle, RuntimeHealth
from cortex_backend.execution.repository import ExecutionRepository
from support import session_headers as _session


ALLOWED_HOSTS = ("testserver", "127.0.0.1", "localhost", "::1")



def _app(tmp_path, lifecycle: ExecutionLifecycle):
    return create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        execution_lifecycle=lifecycle,
        installation_principal_id=lifecycle.repository.installation_principal_id,
    )


def test_disabled_lifecycle_keeps_execution_unavailable_without_calling_factory(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    factory_calls: list[bool] = []
    lifecycle = ExecutionLifecycle(
        repository,
        coordinator_factory=lambda repo: factory_calls.append(True) or DurableFakeCoordinator(repo),
        health_check=RuntimeHealth.ready,
        enabled=False,
    )
    app = _app(tmp_path, lifecycle)

    with TestClient(app) as client:
        headers = _session(client, app)
        assert client.get("/api/v1/system", headers=headers).json()[
            "execution_preview_available"
        ] is False
        assert client.post(
            "/api/v1/execution/preview/fake",
            headers=headers,
            json={"request_id": "disabled"},
        ).status_code == 404
        assert lifecycle.snapshot.state == "disabled"
    assert factory_calls == []


def test_health_blocked_lifecycle_fails_closed_but_chat_readiness_remains_available(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    factory_calls: list[bool] = []
    lifecycle = ExecutionLifecycle(
        repository,
        coordinator_factory=lambda repo: factory_calls.append(True) or DurableFakeCoordinator(repo),
        health_check=lambda: RuntimeHealth.blocked(
            "runtime_unavailable", "Qualified execution runtime is unavailable."
        ),
        enabled=True,
    )
    app = _app(tmp_path, lifecycle)

    with TestClient(app) as client:
        headers = _session(client, app)
        assert client.get("/api/v1/health/ready").status_code == 200
        assert client.get("/api/v1/system", headers=headers).json()[
            "execution_preview_available"
        ] is False
        assert lifecycle.snapshot.state == "blocked"
        assert lifecycle.snapshot.health.code == "runtime_unavailable"
    assert factory_calls == []


def test_ready_lifecycle_owns_startup_recovery_and_shutdown(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    job, _ = repository.create_job(
        job_id="lifecycle-recovery",
        owner=repository.installation_principal_id,
        request_id="lifecycle-recovery-request",
        profile="fake.v1",
        payload={
            "provider": "fake-v1",
            "outcome": "success",
            "steps": 1,
            "step_delay_seconds": 0.0,
            "failure_message": "Deterministic fake execution failed.",
        },
    )
    repository.claim_lease(job.job_id, lease_owner="crashed-worker", ttl_seconds=0.01)
    time.sleep(0.03)
    lifecycle = ExecutionLifecycle(
        repository,
        coordinator_factory=lambda repo: DurableFakeCoordinator(repo, auto_recover=False),
        health_check=RuntimeHealth.ready,
        enabled=True,
    )
    app = _app(tmp_path, lifecycle)

    with TestClient(app) as client:
        headers = _session(client, app)
        assert client.get("/api/v1/system", headers=headers).json()[
            "execution_preview_available"
        ] is True
        assert lifecycle.snapshot.state == "ready"
        assert lifecycle.snapshot.recovered_job_ids == (job.job_id,)
        for _ in range(200):
            status = client.get(f"/api/v1/execution/{job.job_id}", headers=headers)
            if status.json()["status"] == "succeeded":
                break
            time.sleep(0.005)
        assert status.json()["status"] == "succeeded"
    assert lifecycle.snapshot.state == "stopped"
    assert lifecycle.coordinator is None


def test_factory_failure_is_redacted_and_does_not_enable_execution(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    lifecycle = ExecutionLifecycle(
        repository,
        coordinator_factory=lambda _repo: (_ for _ in ()).throw(
            RuntimeError("secret host path should not escape")
        ),
        health_check=RuntimeHealth.ready,
        enabled=True,
    )
    app = _app(tmp_path, lifecycle)

    with TestClient(app) as client:
        headers = _session(client, app)
        assert client.get("/api/v1/system", headers=headers).json()[
            "execution_preview_available"
        ] is False
        assert lifecycle.snapshot.state == "blocked"
        assert lifecycle.snapshot.health.code == "runtime_start_failed"
        assert "secret" not in lifecycle.snapshot.health.message.lower()


def test_recovery_failure_cleans_up_partial_coordinator_and_stays_blocked(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    cleanup_calls: list[bool] = []

    class FailingCoordinator:
        def __init__(self, repo):
            self.repository = repo

        def startup_recover(self) -> list[str]:
            raise RuntimeError("secret recovery detail should not escape")

        def shutdown(self) -> None:
            cleanup_calls.append(True)

    lifecycle = ExecutionLifecycle(
        repository,
        coordinator_factory=FailingCoordinator,
        health_check=RuntimeHealth.ready,
        enabled=True,
    )

    snapshot = lifecycle.start()

    assert snapshot.state == "blocked"
    assert snapshot.health.code == "runtime_start_failed"
    assert snapshot.available is False
    assert lifecycle.coordinator is None
    assert cleanup_calls == [True]
    assert "secret" not in snapshot.health.message.lower()


def test_lifecycle_can_restart_after_clean_stop_without_reusing_stale_recovery_state(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    factory_calls = 0

    def factory(repo):
        nonlocal factory_calls
        factory_calls += 1
        return DurableFakeCoordinator(repo, auto_recover=False)

    lifecycle = ExecutionLifecycle(
        repository,
        coordinator_factory=factory,
        health_check=RuntimeHealth.ready,
        enabled=True,
    )
    assert lifecycle.start().state == "ready"
    assert lifecycle.stop().state == "stopped"
    assert lifecycle.start().state == "ready"
    assert lifecycle.snapshot.recovered_job_ids == ()
    assert factory_calls == 2
    lifecycle.stop()


class _StubbornCoordinator:
    """A coordinator whose shutdown fails until told otherwise."""

    def __init__(self, repo, *, recover_fails: bool = False):
        self.repository = repo
        self.recover_fails = recover_fails
        self.shutdown_fails = True
        self.shutdown_calls = 0

    def startup_recover(self) -> list[str]:
        if self.recover_fails:
            raise RuntimeError("synthetic recovery failure")
        return []

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_fails:
            raise RuntimeError("synthetic shutdown failure")


def _stubborn_lifecycle(tmp_path, *, recover_fails: bool = False):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    built: list[_StubbornCoordinator] = []

    def factory(repo):
        coordinator = _StubbornCoordinator(repo, recover_fails=recover_fails)
        built.append(coordinator)
        return coordinator

    lifecycle = ExecutionLifecycle(
        repository,
        coordinator_factory=factory,
        health_check=RuntimeHealth.ready,
        enabled=True,
    )
    return lifecycle, built


def test_a_failed_stop_keeps_the_coordinator_and_start_will_not_build_a_second(tmp_path):
    """Dropping the reference before shutdown succeeded left the old coordinator running.

    A restart from the blocked state then built a second coordinator over the
    same store; its supervisor lease claim is designed to take a live foreign
    lease, so the new one stole it from the still-running orphan and both
    launched workers and recovered the same jobs.
    """

    lifecycle, built = _stubborn_lifecycle(tmp_path)
    assert lifecycle.start().state == "ready"
    orphan = built[0]

    stopped = lifecycle.stop()

    assert stopped.state == "blocked"
    assert stopped.health.code == "runtime_stop_failed"
    assert stopped.available is False
    # Held, but never handed out while it is not fully ready.
    assert lifecycle.coordinator is None

    refused = lifecycle.start()

    assert len(built) == 1, "a second coordinator was built beside one that failed to stop"
    assert refused.state == "blocked"
    assert refused.health.code == "runtime_stop_failed"
    assert refused.available is False
    assert lifecycle.coordinator is None
    assert orphan.shutdown_calls == 1  # start() refuses; only stop() retries the shutdown


def test_a_stop_that_finally_succeeds_lets_the_lifecycle_start_again(tmp_path):
    lifecycle, built = _stubborn_lifecycle(tmp_path)
    lifecycle.start()
    lifecycle.stop()
    assert lifecycle.start().state == "blocked"

    built[0].shutdown_fails = False
    retried = lifecycle.stop()

    assert retried.state == "stopped"
    assert built[0].shutdown_calls == 2
    assert lifecycle.start().state == "ready"
    assert len(built) == 2
    built[1].shutdown_fails = False
    assert lifecycle.stop().state == "stopped"


def test_a_partial_coordinator_that_cannot_be_shut_down_is_not_abandoned(tmp_path):
    """The startup-failure cleanup has the same shape as stop(): failing to shut down is not a reason to forget."""

    lifecycle, built = _stubborn_lifecycle(tmp_path, recover_fails=True)

    first = lifecycle.start()

    assert first.state == "blocked"
    assert first.health.code == "runtime_start_failed"
    assert lifecycle.coordinator is None

    second = lifecycle.start()

    assert len(built) == 1, "a second coordinator was built beside one that could not be shut down"
    assert second.state == "blocked"
    built[0].shutdown_fails = False
    assert lifecycle.stop().state == "stopped"
    # Released, so the next attempt may build a fresh coordinator (which, in this
    # scenario, fails to recover and to shut down in just the same way).
    assert lifecycle.start().state == "blocked"
    assert len(built) == 2
    built[1].shutdown_fails = False
    assert lifecycle.stop().state == "stopped"


def test_a_clean_stop_still_drops_the_coordinator(tmp_path):
    lifecycle, built = _stubborn_lifecycle(tmp_path)
    lifecycle.start()
    built[0].shutdown_fails = False

    assert lifecycle.stop().state == "stopped"
    assert lifecycle.start().state == "ready"

    assert len(built) == 2
    built[1].shutdown_fails = False
    lifecycle.stop()
