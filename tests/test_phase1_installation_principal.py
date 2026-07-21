"""Stable installation-principal and restart-reattachment contract tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.api.security import SessionManager
from cortex_backend.execution.coordinator import DurableFakeCoordinator
from cortex_backend.execution.repository import ExecutionRepository, ExecutionRepositoryError


ALLOWED_HOSTS = ("testserver", "127.0.0.1", "localhost", "::1")


def _session(client: TestClient, app) -> dict[str, str]:
    response = client.post(
        "/api/v1/session/exchange",
        json={"bootstrap_token": app.state.session_manager.bootstrap_token},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['session_token']}"}


def test_installation_principal_is_atomic_persistent_and_migrates_additively(tmp_path):
    database = tmp_path / "execution.sqlite"
    artifacts = tmp_path / "artifacts"
    first = ExecutionRepository(database, artifacts)
    principal = first.installation_principal_id
    assert len(principal) == 64
    assert int(principal, 16) >= 0
    first.create_job(
        job_id="legacy-job",
        owner="legacy-session",
        request_id="legacy-request",
        profile="fake.v1",
        payload={"provider": "fake-v1"},
    )

    with first.connect() as connection:
        assert connection.execute(
            "SELECT version FROM execution_schema WHERE id = 1"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT principal_id FROM execution_installation_principal WHERE id = 1"
        ).fetchone()[0] == principal
        connection.execute("UPDATE execution_schema SET version = 2 WHERE id = 1")
        connection.execute("DROP TABLE execution_installation_principal")

    migrated = ExecutionRepository(database, artifacts)
    assert migrated.installation_principal_id != ""
    assert migrated.installation_principal_id != principal
    assert migrated.get_job(
        "legacy-job", owner=migrated.installation_principal_id
    ) is not None
    with migrated.connect() as connection:
        assert connection.execute(
            "SELECT version FROM execution_schema WHERE id = 1"
        ).fetchone()[0] == 3


def test_installation_principal_creation_is_singleton_across_repository_instances(tmp_path):
    database = tmp_path / "execution.sqlite"
    artifacts = tmp_path / "artifacts"

    def load(_: int) -> str:
        return ExecutionRepository(database, artifacts).installation_principal_id

    with ThreadPoolExecutor(max_workers=6) as executor:
        principals = set(executor.map(load, range(12)))
    assert len(principals) == 1


def test_malformed_persisted_installation_principal_fails_closed(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    _ = repository.installation_principal_id
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_installation_principal SET principal_id = 'not-safe' WHERE id = 1"
        )

    restarted = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    with pytest.raises(ExecutionRepositoryError, match="Installation principal is invalid"):
        _ = restarted.installation_principal_id


def test_ambiguous_legacy_owner_migration_fails_closed_without_rewriting_jobs(tmp_path):
    database = tmp_path / "execution.sqlite"
    artifacts = tmp_path / "artifacts"
    repository = ExecutionRepository(database, artifacts)
    repository.create_job(
        job_id="legacy-a",
        owner="session-a",
        request_id="same-request",
        profile="fake.v1",
        payload={"provider": "fake-v1"},
    )
    repository.create_job(
        job_id="legacy-b",
        owner="session-b",
        request_id="same-request",
        profile="fake.v1",
        payload={"provider": "fake-v1"},
    )
    with repository.connect() as connection:
        connection.execute("UPDATE execution_schema SET version = 2 WHERE id = 1")

    with pytest.raises(ExecutionRepositoryError, match="owners are ambiguous"):
        ExecutionRepository(database, artifacts)
    with repository.connect() as connection:
        owners = {
            row[0]
            for row in connection.execute(
                "SELECT owner FROM execution_jobs ORDER BY job_id"
            ).fetchall()
        }
    assert owners == {"session-a", "session-b"}


def test_restart_reattaches_execution_to_same_installation_principal(tmp_path):
    database = tmp_path / "execution.sqlite"
    artifacts = tmp_path / "artifacts"
    first_repository = ExecutionRepository(database, artifacts)
    first_coordinator = DurableFakeCoordinator(first_repository)
    first_manager = SessionManager(
        allowed_hosts=ALLOWED_HOSTS,
        installation_principal_id=first_repository.installation_principal_id,
    )
    first_app = create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        session_manager=first_manager,
        execution_coordinator=first_coordinator,
    )
    owner = first_repository.installation_principal_id
    first_repository.create_job(
        job_id="restart-visible",
        owner=owner,
        request_id="restart-request",
        profile="fake.v1",
        payload={"provider": "fake-v1"},
    )
    with TestClient(first_app) as client:
        first_headers = _session(client, first_app)
        assert client.get("/api/v1/execution/restart-visible", headers=first_headers).status_code == 200
    first_coordinator.shutdown()

    second_repository = ExecutionRepository(database, artifacts)
    assert second_repository.installation_principal_id == owner
    second_coordinator = DurableFakeCoordinator(second_repository)
    second_manager = SessionManager(
        allowed_hosts=ALLOWED_HOSTS,
        installation_principal_id=second_repository.installation_principal_id,
    )
    second_app = create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        session_manager=second_manager,
        execution_coordinator=second_coordinator,
    )
    try:
        with TestClient(second_app) as client:
            second_headers = _session(client, second_app)
            listed = client.get("/api/v1/execution/tasks", headers=second_headers)
            assert listed.status_code == 200
            assert [task["job_id"] for task in listed.json()["tasks"]] == ["restart-visible"]
    finally:
        second_coordinator.shutdown()


def test_api_rejects_session_manager_principal_mismatch(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = DurableFakeCoordinator(repository)
    try:
        manager = SessionManager(
            allowed_hosts=ALLOWED_HOSTS,
            installation_principal_id="a" * 64,
        )
        with pytest.raises(ValueError, match="does not match installation principal"):
            create_app(
                build_demo_dependencies(),
                allowed_hosts=ALLOWED_HOSTS,
                session_manager=manager,
                execution_coordinator=coordinator,
            )
    finally:
        coordinator.shutdown()
