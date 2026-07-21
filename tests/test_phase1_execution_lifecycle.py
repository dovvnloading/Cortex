"""Health-gated production lifecycle and recovery integration tests."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.execution.coordinator import DurableFakeCoordinator
from cortex_backend.execution.lifecycle import ExecutionLifecycle, RuntimeHealth
from cortex_backend.execution.repository import ExecutionRepository


ALLOWED_HOSTS = ("testserver", "127.0.0.1", "localhost", "::1")


def _session(client: TestClient, app) -> dict[str, str]:
    response = client.post(
        "/api/v1/session/exchange",
        json={"bootstrap_token": app.state.session_manager.bootstrap_token},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['session_token']}"}


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
