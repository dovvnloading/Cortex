"""HTTP contract coverage for approval-gated local code."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.repository import ExecutionRepository
from support import session_headers as _session


ALLOWED_HOSTS = ("testserver", "127.0.0.1", "localhost", "::1")



def test_code_api_is_pending_until_approved_and_source_is_owner_scoped(tmp_path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        preview=True,
        execution_coordinator=coordinator,
    )
    with TestClient(app) as client:
        headers = _session(client, app)
        system = client.get("/api/v1/system", headers=headers)
        assert system.status_code == 200
        assert system.json()["code_execution_available"] is True
        accepted = client.post(
            "/api/v1/execution/code",
            headers=headers,
            json={
                "request_id": "api-code",
                "language": "python",
                "source": "print('api')\n_result = 7 * 6",
                "intent_summary": "Run a local verification.",
                "capabilities": {"filesystem": False, "process": False, "network": False},
            },
        )
        assert accepted.status_code == 202
        body = accepted.json()
        assert body["approval_state"] == "pending"
        job_id = body["job_id"]

        source = client.get(f"/api/v1/execution/{job_id}/source", headers=headers)
        assert source.status_code == 200
        assert source.json()["source"].startswith("print")
        assert source.json()["source_digest"] == body["source_digest"]

        approved = client.post(
            f"/api/v1/execution/{job_id}/approval",
            headers=headers,
            json={"decision": "approved"},
        )
        assert approved.status_code == 200
        for _ in range(200):
            status = client.get(f"/api/v1/execution/{job_id}", headers=headers)
            if status.json()["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert status.json()["status"] == "succeeded"
        assert status.json()["result"]["value"] == 42
        assert status.json()["result"]["stdout"] == "api\n"


def test_code_api_rejects_process_access_without_native_sandboxing(tmp_path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        preview=True,
        execution_coordinator=coordinator,
    )
    with TestClient(app) as client:
        headers = _session(client, app)
        response = client.post(
            "/api/v1/execution/code",
            headers=headers,
            json={
                "request_id": "api-process-rejected",
                "language": "python",
                "source": "_result = cortex.process.run(['cmd', '/c', 'echo', 'unsafe'])",
                "intent_summary": "Attempt an unsandboxed process.",
                "capabilities": {
                    "filesystem": False,
                    "process": True,
                    "network": False,
                },
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"] == (
            "Process access is unavailable until native sandbox isolation is enabled."
        )
        assert repository.list_jobs(
            owner=repository.installation_principal_id,
            include_terminal=True,
        ) == []
