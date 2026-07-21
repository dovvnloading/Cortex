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


def _owner(app, headers: dict[str, str]) -> str:
    token = headers["Authorization"].removeprefix("Bearer ")
    return app.state.session_manager.authenticate(token).session_id


def _pending_approval(app, *, owner: str, job_id: str, ttl_seconds: float = 60.0):
    repository = app.state.execution_coordinator.repository
    repository.create_job(
        job_id=job_id,
        owner=owner,
        request_id=f"request-{job_id}",
        profile="artifact.extended.v1",
        payload={"private": "must-not-leak"},
    )
    repository.request_approval(
        job_id,
        owner=owner,
        scope_digest="server-bound-scope",
        reason="Create a larger staged image preview.",
        ttl_seconds=ttl_seconds,
    )
    return repository


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


def test_approval_api_is_owner_scoped_exactly_once_and_redacted(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        first_headers = _session(client, app)
        first_owner = _owner(app, first_headers)
        repository = _pending_approval(app, owner=first_owner, job_id="approval-owner")

        app.state.session_manager.issue_bootstrap_token()
        second_headers = _session(client, app)
        foreign = client.post(
            "/api/v1/execution/approval-owner/approval",
            headers=second_headers,
            json={"decision": "approved"},
        )
        assert foreign.status_code == 404

        status = client.get("/api/v1/execution/approval-owner", headers=first_headers)
        assert status.status_code == 200
        assert status.json()["profile"] == "artifact.extended.v1"
        assert status.json()["approval_state"] == "pending"
        assert status.json()["approval_reason"] == "Create a larger staged image preview."
        assert status.json()["approval_expires_at"] is not None
        assert status.json()["can_cancel"] is False
        serialized = json.dumps(status.json()).lower()
        assert "server-bound-scope" not in serialized
        assert "must-not-leak" not in serialized
        assert "lease" not in serialized

        approved = client.post(
            "/api/v1/execution/approval-owner/approval",
            headers=first_headers,
            json={"decision": "approved"},
        )
        assert approved.status_code == 200
        assert approved.json()["approval_state"] == "approved"
        assert approved.json()["can_cancel"] is True
        assert repository.get_approval("approval-owner").state == "approved"

        duplicate = client.post(
            "/api/v1/execution/approval-owner/approval",
            headers=first_headers,
            json={"decision": "denied"},
        )
        assert duplicate.status_code == 409
        assert repository.get_approval("approval-owner").state == "approved"
        events = repository.events("approval-owner")
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert events[-1].data["approval_state"] == "approved"

        _pending_approval(app, owner=first_owner, job_id="approval-denied")
        denied = client.post(
            "/api/v1/execution/approval-denied/approval",
            headers=first_headers,
            json={"decision": "denied"},
        )
        assert denied.status_code == 200
        assert denied.json()["approval_state"] == "denied"
        assert denied.json()["status"] == "cancelled"
        assert denied.json()["error"] == "approval_denied"
        assert denied.json()["can_cancel"] is False


def test_approval_api_rejects_fake_malformed_and_expired_decisions(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _session(client, app)
        owner = _owner(app, headers)
        repository = _pending_approval(
            app,
            owner=owner,
            job_id="approval-expired",
            ttl_seconds=0.01,
        )
        time.sleep(0.03)

        effective = client.get(
            "/api/v1/execution/approval-expired", headers=headers
        )
        assert effective.status_code == 200
        assert effective.json()["approval_state"] == "expired"

        late = client.post(
            "/api/v1/execution/approval-expired/approval",
            headers=headers,
            json={"decision": "approved"},
        )
        assert late.status_code == 409
        assert repository.get_approval("approval-expired").state == "expired"
        expired_job = repository.get_job("approval-expired")
        assert expired_job.status == "cancelled"
        assert expired_job.error == "approval_expired"

        malformed = client.post(
            "/api/v1/execution/approval-expired/approval",
            headers=headers,
            json={"decision": "approve everything", "scope": "replacement"},
        )
        assert malformed.status_code == 422

        fake = client.post(
            "/api/v1/execution/preview/fake",
            headers=headers,
            json={"request_id": "approval-not-required", "steps": 1},
        )
        fake_decision = client.post(
            f"/api/v1/execution/{fake.json()['job_id']}/approval",
            headers=headers,
            json={"decision": "approved"},
        )
        assert fake_decision.status_code == 409
