"""Authenticated fake-only execution API and SSE contract tests."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.execution.coordinator import DurableFakeCoordinator
from cortex_backend.execution.repository import ExecutionRepository


ALLOWED_HOSTS = ("testserver", "127.0.0.1", "localhost", "::1")


def _session(client: TestClient, app) -> dict[str, str]:
    response = client.post(
        "/api/v1/session/exchange",
        json={"bootstrap_token": app.state.session_manager.bootstrap_token},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['session_token']}"}


def _app(tmp_path, *, preview: bool = True):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = DurableFakeCoordinator(repository)
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        preview=preview,
        execution_coordinator=coordinator,
    )
    return app


def test_preview_route_requires_explicit_injected_coordinator(tmp_path):
    app = create_app(build_demo_dependencies(), allowed_hosts=ALLOWED_HOSTS)
    with TestClient(app) as client:
        headers = _session(client, app)
        assert client.get("/api/v1/system", headers=headers).json()["execution_preview_available"] is False
        response = client.post(
            "/api/v1/execution/preview/fake",
            headers=headers,
            json={"request_id": "not-enabled"},
        )
        assert response.status_code == 404
        assert "provider" not in response.text.lower()


def test_preview_lifecycle_is_owner_scoped_idempotent_and_replayable(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _session(client, app)
        assert client.get("/api/v1/system", headers=headers).json()["execution_preview_available"] is True
        accepted = client.post(
            "/api/v1/execution/preview/fake",
            headers=headers,
            json={"request_id": "api-success", "steps": 3, "step_delay_seconds": 0.01},
        )
        assert accepted.status_code == 202
        body = accepted.json()
        duplicate = client.post(
            "/api/v1/execution/preview/fake",
            headers=headers,
            json={"request_id": "api-success", "steps": 20},
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["job_id"] == body["job_id"]

        job_id = body["job_id"]
        for _ in range(200):
            status = client.get(f"/api/v1/execution/{job_id}", headers=headers)
            if status.json()["status"] == "succeeded":
                break
            time.sleep(0.005)
        assert status.status_code == 200
        assert status.json()["status"] == "succeeded"
        assert status.json()["approval_state"] == "not_required"
        assert status.json()["result"] == {"provider": "fake-v1", "steps": 3, "value": 42}
        assert "path" not in json.dumps(status.json()).lower()

        replay = client.get(f"/api/v1/execution/{job_id}/events", headers=headers)
        assert replay.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in replay.text.splitlines()
            if line.startswith("data: ")
        ]
        assert events
        assert events[0]["event"] == "execution.queued"
        assert events[-1]["event"] == "execution.completed"
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))

        resumed = client.get(
            f"/api/v1/execution/{job_id}/events",
            headers={**headers, "Last-Event-ID": str(events[-2]["sequence"])},
        )
        assert [
            json.loads(line.removeprefix("data: "))["sequence"]
            for line in resumed.text.splitlines()
            if line.startswith("data: ")
        ] == [events[-1]["sequence"]]

        tasks = client.get(
            "/api/v1/execution/tasks?include_terminal=true",
            headers=headers,
        )
        assert tasks.status_code == 200
        assert tasks.json()["tasks"][0]["job_id"] == job_id
        assert tasks.json()["tasks"][0]["approval_state"] == "not_required"


def test_preview_api_rejects_foreign_owner_and_cancels_durably(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        first_headers = _session(client, app)
        app.state.session_manager.issue_bootstrap_token()
        second_headers = _session(client, app)
        accepted = client.post(
            "/api/v1/execution/preview/fake",
            headers=first_headers,
            json={"request_id": "api-cancel", "steps": 20, "step_delay_seconds": 0.05},
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]

        foreign = client.get(f"/api/v1/execution/{job_id}", headers=second_headers)
        assert foreign.status_code == 404
        cancelled = client.post(
            f"/api/v1/execution/{job_id}/cancel", headers=first_headers
        )
        assert cancelled.status_code == 200
        for _ in range(200):
            status = client.get(f"/api/v1/execution/{job_id}", headers=first_headers)
            if status.json()["status"] == "cancelled":
                break
            time.sleep(0.005)
        assert status.json()["status"] == "cancelled"
        assert status.json()["can_cancel"] is False
