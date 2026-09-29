"""Authenticated execution API and SSE contract tests.

Driven through the deterministic preview coordinator so owner scoping,
idempotent replay, durable cancel, and the SSE stream can be asserted without
running real work.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import threading
import time

from fastapi.testclient import TestClient

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.testing import DurableFakeCoordinator, install_execution_preview
from cortex_backend.execution.repository import ExecutionRepository
from support import session_headers as _session


ALLOWED_HOSTS = ("testserver", "127.0.0.1", "localhost", "::1")



def _app(tmp_path, *, preview: bool = True):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = DurableFakeCoordinator(repository)
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        preview=preview,
        execution_coordinator=coordinator,
    )
    return install_execution_preview(app)


def _owner(app, headers: dict[str, str]) -> str:
    token = headers["Authorization"].removeprefix("Bearer ")
    return app.state.session_manager.authenticate(token).installation_principal_id


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
        assert tasks.json()["tasks"][0]["result"] == {
            "provider": "fake-v1",
            "steps": 3,
            "value": 42,
        }


def test_preview_api_reuses_installation_owner_across_sessions_and_cancels_durably(tmp_path):
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

        shared = client.get(f"/api/v1/execution/{job_id}", headers=second_headers)
        assert shared.status_code == 200
        cancelled = client.post(
            f"/api/v1/execution/{job_id}/cancel", headers=second_headers
        )
        assert cancelled.status_code == 200
        for _ in range(200):
            status = client.get(f"/api/v1/execution/{job_id}", headers=first_headers)
            if status.json()["status"] == "cancelled":
                break
            time.sleep(0.005)
        assert status.json()["status"] == "cancelled"
        assert status.json()["can_cancel"] is False


def test_task_list_keeps_code_failure_reason_for_diagnosis(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _session(client, app)
        owner = _owner(app, headers)
        repository = app.state.execution_coordinator.repository
        repository.create_job(
            job_id="code-failure-reason",
            owner=owner,
            request_id="request-code-failure-reason",
            profile="code.exec.v1",
            payload={
                "schema_version": "code.execution.v1",
                "language": "python",
                "source": "print('diagnose')",
                "intent_summary": "Diagnose a failed worker.",
                "source_digest": "a" * 64,
                "capabilities": {"filesystem": False, "process": False, "network": False},
            },
        )
        repository.transition(
            "code-failure-reason",
            status="failed",
            event="code.failed",
            phase="failed",
            data={"message": "Local code execution failed safely."},
            error="worker_timeout",
        )

        tasks = client.get(
            "/api/v1/execution/tasks?include_terminal=true",
            headers=headers,
        )

        assert tasks.status_code == 200
        task = next(item for item in tasks.json()["tasks"] if item["job_id"] == "code-failure-reason")
        assert task["error"] == "worker_timeout"


def test_task_list_prioritizes_pending_approval_over_terminal_history(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _session(client, app)
        owner = _owner(app, headers)
        repository = _pending_approval(
            app,
            owner=owner,
            job_id="older-actionable-approval",
        )
        for index in range(20):
            job_id = f"newer-terminal-{index:02d}"
            repository.create_job(
                job_id=job_id,
                owner=owner,
                request_id=f"request-{job_id}",
                profile="fake.v1",
                payload={},
            )
            repository.transition(
                job_id,
                status="succeeded",
                event="completed",
                phase="completed",
                data={"message": "Terminal history."},
                result={"index": index},
            )

        tasks = client.get(
            "/api/v1/execution/tasks?include_terminal=true&limit=20",
            headers=headers,
        )

        assert tasks.status_code == 200
        task_ids = [item["job_id"] for item in tasks.json()["tasks"]]
        assert len(task_ids) == 20
        assert task_ids[0] == "older-actionable-approval"
        assert "newer-terminal-19" in task_ids


def test_task_list_does_not_prioritize_expired_approval_rows(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _session(client, app)
        owner = _owner(app, headers)
        repository = _pending_approval(
            app,
            owner=owner,
            job_id="older-actionable-approval",
        )
        for index in range(20):
            job_id = f"newer-expired-approval-{index:02d}"
            _pending_approval(app, owner=owner, job_id=job_id)
            with repository.connect() as connection:
                connection.execute(
                    "UPDATE execution_approvals SET expires_at = ? WHERE job_id = ?",
                    ("2000-01-01T00:00:00+00:00", job_id),
                )

        tasks = client.get(
            "/api/v1/execution/tasks?include_terminal=true&limit=20",
            headers=headers,
        )

        assert tasks.status_code == 200
        task_ids = [item["job_id"] for item in tasks.json()["tasks"]]
        assert len(task_ids) == 20
        assert task_ids[0] == "older-actionable-approval"
        assert "newer-expired-approval-19" in task_ids


def _count_connections(repository, monkeypatch) -> list[str]:
    """Record every connection the repository opens off a ``cortex-*`` background thread.

    Each ``connect()`` opens a fresh SQLite connection and re-issues its pragmas,
    so the count is the cost of a request. Supervisor threads (retention cleanup,
    leases) share the repository and would add unrelated connections while a
    request runs, so only the request's own threads are counted.
    """

    opened: list[str] = []
    real_connect = repository.connect

    @contextmanager
    def counting_connect():
        if not threading.current_thread().name.startswith("cortex-"):
            opened.append(threading.current_thread().name)
        with real_connect() as connection:
            yield connection

    monkeypatch.setattr(repository, "connect", counting_connect)
    return opened


def _seed_jobs_in_every_state(app, owner: str, repository) -> list[str]:
    """One job in each state the tray has to describe; returns their ids."""

    def make(job_id: str, profile: str = "fake.v1"):
        repository.create_job(
            job_id=job_id,
            owner=owner,
            request_id=f"request-{job_id}",
            profile=profile,
            payload={},
        )

    make("state-queued")
    make("state-running")
    repository.transition(
        "state-running",
        status="running",
        event="started",
        phase="running",
        data={"message": "Working on it."},
    )
    make("state-succeeded")
    repository.transition(
        "state-succeeded",
        status="succeeded",
        event="completed",
        phase="completed",
        data={"message": "All done."},
        result={"value": 42},
    )
    make("state-failed")
    repository.transition(
        "state-failed",
        status="failed",
        event="failed",
        phase="failed",
        data={"message": "It broke."},
        error="worker_timeout",
    )
    make("state-cancelling")
    repository.request_cancel("state-cancelling")
    _pending_approval(app, owner=owner, job_id="state-approval-pending")
    _pending_approval(app, owner=owner, job_id="state-approval-approved")
    repository.decide_approval("state-approval-approved", owner=owner, decision="approved")
    _pending_approval(app, owner=owner, job_id="state-approval-expired")
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_approvals SET expires_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00+00:00", "state-approval-expired"),
        )
    return [
        "state-queued",
        "state-running",
        "state-succeeded",
        "state-failed",
        "state-cancelling",
        "state-approval-pending",
        "state-approval-approved",
        "state-approval-expired",
    ]


def test_listing_tasks_opens_a_bounded_number_of_connections(tmp_path, monkeypatch):
    """The tray refresh cost two extra connections per task.

    Each job's newest event and approval were looked up with their own
    connection, so twenty jobs cost forty-one and ``include_terminal`` lists of
    two hundred cost four hundred and one. They now come back with the list.
    """

    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _session(client, app)
        owner = _owner(app, headers)
        repository = _pending_approval(app, owner=owner, job_id="pending-approval")
        for index in range(19):
            job_id = f"terminal-{index:02d}"
            repository.create_job(
                job_id=job_id,
                owner=owner,
                request_id=f"request-{job_id}",
                profile="fake.v1",
                payload={},
            )
            repository.transition(
                job_id,
                status="succeeded",
                event="completed",
                phase="completed",
                data={"message": "Terminal history."},
                result={"index": index},
            )
        opened = _count_connections(repository, monkeypatch)

        tasks = client.get(
            "/api/v1/execution/tasks?include_terminal=true&limit=50", headers=headers
        )

        assert tasks.status_code == 200
        assert len(tasks.json()["tasks"]) == 20
        assert 1 <= len(opened) <= 2, opened


def test_the_task_list_describes_each_job_exactly_as_its_own_status_does(tmp_path):
    """Batching the reads must not change what the tray says about a job.

    Compared field by field against ``GET /execution/{id}``, which still looks
    the event and approval up per request, across every state a job can be in.
    """

    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _session(client, app)
        owner = _owner(app, headers)
        repository = app.state.execution_coordinator.repository
        job_ids = _seed_jobs_in_every_state(app, owner, repository)

        listed = {
            task["job_id"]: task
            for task in client.get(
                "/api/v1/execution/tasks?include_terminal=true&limit=50", headers=headers
            ).json()["tasks"]
        }
        assert sorted(listed) == sorted(job_ids)

        compared = (
            "profile",
            "status",
            "sequence",
            "phase",
            "message",
            "approval_state",
            "approval_reason",
            "approval_expires_at",
            "can_cancel",
            "error",
        )
        for job_id in job_ids:
            single = client.get(f"/api/v1/execution/{job_id}", headers=headers).json()
            assert {key: listed[job_id][key] for key in compared} == {
                key: single[key] for key in compared
            }, job_id

        # And the states are the ones this test set out to cover.
        assert listed["state-running"]["message"] == "Working on it."
        assert listed["state-running"]["can_cancel"] is True
        assert listed["state-succeeded"]["message"] == "All done."
        assert listed["state-failed"]["error"] == "worker_timeout"
        assert listed["state-cancelling"]["status"] == "cancelling"
        assert listed["state-approval-pending"]["approval_state"] == "pending"
        assert listed["state-approval-pending"]["approval_reason"] == (
            "Create a larger staged image preview."
        )
        assert listed["state-approval-pending"]["can_cancel"] is False
        assert listed["state-approval-approved"]["approval_state"] == "approved"
        assert listed["state-approval-expired"]["approval_state"] == "expired"


def test_approval_api_is_owner_scoped_exactly_once_and_redacted(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        first_headers = _session(client, app)
        first_owner = _owner(app, first_headers)
        repository = _pending_approval(app, owner=first_owner, job_id="approval-owner")

        app.state.session_manager.issue_bootstrap_token()
        second_headers = _session(client, app)
        foreign_job = "approval-foreign"
        repository.create_job(
            job_id=foreign_job,
            owner="f" * 64,
            request_id="request-approval-foreign",
            profile="artifact.extended.v1",
            payload={"private": "must-not-leak"},
        )
        repository.request_approval(
            foreign_job,
            owner="f" * 64,
            scope_digest="foreign-scope",
            reason="Foreign approval.",
        )
        foreign = client.post(
            f"/api/v1/execution/{foreign_job}/approval",
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
            headers=second_headers,
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


def test_event_stream_survives_an_idle_approval_wait_and_stays_off_the_event_loop(
    tmp_path, monkeypatch
):
    """A job parked on an approval emits nothing, and that is the normal case.

    The stream used to close after 600 ten-millisecond ticks -- six seconds --
    so the one situation it exists to report on was also the one it gave up
    on first. Approvals are valid for up to MAX_APPROVAL_TTL_SECONDS, and the
    idle cap now clears that with a keep-alive comment in the meantime.

    The same loop reads SQLite on every tick. Those reads have to happen off
    the event loop, or the stream stalls every other request while it polls.

    Stream time belongs to the test, not the wall clock: each repository poll
    advances it by a fixed step. The test used to race a 0.15 s real-time
    cap, and on a loaded CI runner the first idle poll alone could outlast
    it, so the stream closed before it sent a single keep-alive.
    """

    # The route body lives here, so this is where its module-level names are
    # looked up -- patching cortex_backend.api.routes would have no effect.
    from cortex_backend.api.routers import execution as execution_routes

    now = [0.0]
    monkeypatch.setattr(execution_routes, "_stream_clock", lambda: now[0])
    monkeypatch.setattr(execution_routes, "EXECUTION_STREAM_POLL_SECONDS", 0.0)
    monkeypatch.setattr(execution_routes, "EXECUTION_STREAM_HEARTBEAT_SECONDS", 2.0)
    monkeypatch.setattr(execution_routes, "EXECUTION_STREAM_IDLE_TIMEOUT_SECONDS", 7.5)

    reads_on_event_loop: list[bool] = []
    real_poll = execution_routes._poll_execution_stream
    # The first poll stands in for a minute of activity before the job parks,
    # so the idle window must be measured from the last event rather than
    # from when the stream opened. Every later poll is one second.
    steps = iter([60.0])

    def timed_poll(*args, **kwargs):
        # A running loop on the reading thread means the read stalls every
        # other request. Comparing thread ids against the test's own thread
        # proved nothing: TestClient serves the app from a thread of its own,
        # so that check passed even with every read inline on the loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            reads_on_event_loop.append(False)
        else:
            reads_on_event_loop.append(True)
        if len(reads_on_event_loop) > 50:
            raise AssertionError("the stream never closed on its idle cap")
        now[0] += next(steps, 1.0)
        return real_poll(*args, **kwargs)

    monkeypatch.setattr(execution_routes, "_poll_execution_stream", timed_poll)

    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _session(client, app)
        owner = _owner(app, headers)
        repository = _pending_approval(
            app, owner=owner, job_id="idle-approval", ttl_seconds=120.0
        )

        response = client.get("/api/v1/execution/idle-approval/events", headers=headers)

    assert response.status_code == 200
    # The first poll (t=60) delivers the queued and approval events. Idle from
    # then on, a keep-alive falls due every 2 s -- at t=62, 64 and 66 -- and
    # the poll at t=68 finds 8 s of silence, past the 7.5 s cap, and closes.
    assert "execution.queued" in response.text
    assert response.text.count(": keep-alive") == 3
    assert response.text.endswith(": keep-alive\n\n" * 3)
    assert "execution.completed" not in response.text
    # Still parked on its approval, so the stream closed on the idle cap
    # rather than on completion.
    job = repository.get_job("idle-approval", owner=owner)
    assert job is not None
    assert (job.status, job.approval_state) == ("queued", "pending")

    assert reads_on_event_loop, "the stream never polled the repository"
    assert not any(reads_on_event_loop), (
        "repository reads ran on the event loop instead of a worker thread"
    )
