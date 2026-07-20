"""Headless API, session, job, and SSE contract tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.api.jobs import JobConflict, JobOwnershipError, JobRegistry
from cortex_backend.api.security import SessionManager, SessionSecurityError
from cortex_backend.services.progress import ProgressEvent, ProgressSink
from cortex_backend.testing.fake_ollama import FakeOllamaState, create_fake_ollama_app


ALLOWED_HOSTS = ("testserver", "127.0.0.1", "localhost", "::1")


def _client(state: FakeOllamaState | None = None):
    app = create_app(
        build_demo_dependencies(ollama_state=state),
        allowed_hosts=ALLOWED_HOSTS,
    )
    return app, TestClient(app)


def _session(client: TestClient, app) -> dict[str, str]:
    response = client.post(
        "/api/v1/session/exchange",
        json={"bootstrap_token": app.state.session_manager.bootstrap_token},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['session_token']}"}


def _events(body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_api_factory_is_headless_and_session_exchange_is_one_time():
    app, client = _client()
    with client:
        result = subprocess.run(
            [
                "python",
                "-c",
                "import sys; from cortex_backend.api import create_app; assert 'PySide6' not in sys.modules",
            ],
            env={**os.environ, "PYTHONPATH": "backend"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert client.get("/api/v1/health").json() == {"status": "ok"}
        assert client.get("/api/v1/system").status_code == 401
        assert (
            client.post("/api/v1/memories", json={"memo": "blocked"}).status_code == 401
        )

        token = app.state.session_manager.bootstrap_token
        first = client.post("/api/v1/session/exchange", json={"bootstrap_token": token})
        second = client.post(
            "/api/v1/session/exchange", json={"bootstrap_token": token}
        )
        assert first.status_code == 200
        assert second.status_code == 401


def test_security_rejects_non_loopback_host_and_origin():
    app, client = _client()
    default_app = create_app()
    assert default_app.state.session_manager.allowed_hosts == frozenset(
        {"127.0.0.1", "localhost", "::1"}
    )
    with client:
        headers = _session(client, app)
        assert (
            client.get(
                "/api/v1/system", headers={**headers, "Host": "evil.example"}
            ).status_code
            == 400
        )
        assert (
            client.get(
                "/api/v1/system",
                headers={**headers, "Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/v1/system",
                headers={**headers, "Origin": "http://127.0.0.1:5173"},
            ).status_code
            == 200
        )


def test_expired_session_is_rejected_without_exposing_token_details():
    manager = SessionManager(
        bootstrap_token="bootstrap",
        allowed_hosts=("testserver",),
    )
    exchanged = manager.exchange("bootstrap")
    digest = manager._digest(exchanged.token)
    manager._sessions[digest] = replace(
        exchanged.principal,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    try:
        manager.authenticate(exchanged.token)
    except SessionSecurityError:
        pass
    else:
        raise AssertionError("expired session was accepted")


def test_resource_routes_persist_and_require_confirmation_for_clear():
    app, client = _client()
    with client:
        headers = _session(client, app)
        settings = client.get("/api/v1/settings", headers=headers)
        assert settings.status_code == 200
        updated = settings.json()["settings"]
        updated["appearance"]["theme"] = "dark"
        saved = client.put(
            "/api/v1/settings", json={"settings": updated}, headers=headers
        )
        assert saved.status_code == 200
        assert saved.json()["settings"]["appearance"]["theme"] == "dark"

        chat = client.post("/api/v1/chats", json={"title": "New Chat"}, headers=headers)
        thread_id = chat.json()["id"]
        message = client.post(
            f"/api/v1/chats/{thread_id}/messages",
            json={"role": "user", "content": "hello"},
            headers=headers,
        )
        assert message.status_code == 200
        assert len(message.json()["messages"]) == 1

        assert (
            client.post(
                "/api/v1/memories", json={"memo": "Alice"}, headers=headers
            ).status_code
            == 200
        )
        assert client.post(
            "/api/v1/memories", json={"memo": " alice "}, headers=headers
        ).json() == {"memos": ["Alice"]}
        assert (
            client.put(
                "/api/v1/memories", json={"memos": ["one", "two"]}, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.post("/api/v1/memories/clear", json={}, headers=headers).status_code
            == 409
        )
        assert client.post(
            "/api/v1/memories/clear", json={"confirm": True}, headers=headers
        ).json() == {"memos": []}

        models = client.get("/api/v1/models", headers=headers)
        assert models.status_code == 200
        assert "qwen3:8b" in models.json()["installed_models"]


def test_generation_sse_is_ordered_replayable_and_redacts_failures(caplog):
    app, client = _client()
    with client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/jobs/generation",
            json={
                "request_id": "request-1",
                "thread_id": "thread-1",
                "user_input": "hello",
            },
            headers=headers,
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        duplicate = client.post(
            "/api/v1/jobs/generation",
            json={
                "request_id": "request-1",
                "thread_id": "thread-1",
                "user_input": "hello",
            },
            headers=headers,
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["job_id"] == job_id
        with client.stream(
            "GET", f"/api/v1/jobs/{job_id}/events", headers=headers
        ) as response:
            body = "".join(response.iter_text())
        events = _events(body)
        assert [event["id"] for event in events] == sorted(
            event["id"] for event in events
        )
        assert events[0]["status"] == "queued"
        assert events[-1]["kind"] == "completed"
        assert events[-1]["data"]["response"] == "Echo: hello"

        replay = client.get(
            f"/api/v1/jobs/{job_id}/events",
            headers={**headers, "Last-Event-ID": "2"},
        )
        replay_events = _events(replay.text)
        assert replay_events and all(event["id"] > 2 for event in replay_events)
        assert (
            client.get(
                f"/api/v1/jobs/{job_id}/events",
                headers={**headers, "Last-Event-ID": "bad"},
            ).status_code
            == 400
        )

        failed = client.post(
            "/api/v1/jobs/generation",
            json={"thread_id": "thread-1", "user_input": "!fail"},
            headers=headers,
        )
        failed_id = failed.json()["job_id"]
        with client.stream(
            "GET", f"/api/v1/jobs/{failed_id}/events", headers=headers
        ) as response:
            failed_events = _events("".join(response.iter_text()))
        assert failed_events[-1]["kind"] == "error"
        assert (
            failed_events[-1]["data"]["message"]
            == "Generation failed. Please try again."
        )
        assert "hello" not in caplog.text
        assert "!fail" not in caplog.text


def test_generation_conflict_and_cancellation_are_explicit():
    state = FakeOllamaState(generation_delay_seconds=0.2)
    app, client = _client(state)
    with client:
        headers = _session(client, app)
        first = client.post(
            "/api/v1/jobs/generation",
            json={"thread_id": "thread-1", "user_input": "slow"},
            headers=headers,
        )
        second = client.post(
            "/api/v1/jobs/generation",
            json={"thread_id": "thread-1", "user_input": "blocked"},
            headers=headers,
        )
        assert first.status_code == 202
        assert second.status_code == 409
        cancelled = client.post(
            f"/api/v1/jobs/{first.json()['job_id']}/cancel",
            headers=headers,
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"


def test_fake_ollama_server_and_model_failures_are_deterministic():
    fake = create_fake_ollama_app(FakeOllamaState(malformed_list=True))
    with TestClient(fake) as client:
        response = client.get("/api/tags")
        assert response.status_code == 200
        assert response.json() == {"unexpected": "payload"}

    malformed_stream = create_fake_ollama_app(FakeOllamaState(malformed_stream=True))
    with TestClient(malformed_stream) as client:
        response = client.post("/api/generate", json={"prompt": "hello"})
        assert response.status_code == 200
        assert response.text == '{"response":\n'

    state = FakeOllamaState(fail_list=True)
    app, client = _client(state)
    with client:
        headers = _session(client, app)
        check = client.post("/api/v1/jobs/models", headers=headers)
        assert check.status_code == 202
        with client.stream(
            "GET",
            f"/api/v1/jobs/{check.json()['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))
        assert events[-1]["kind"] == "completed"
        assert events[-1]["data"]["connection"]["status"] == "error"
        assert any(event["phase"] == "model_check" for event in events)

    pull_failure = FakeOllamaState(installed_models=set(), fail_pull=True)
    app, client = _client(pull_failure)
    with client:
        headers = _session(client, app)
        check = client.post("/api/v1/jobs/models", headers=headers)
        with client.stream(
            "GET",
            f"/api/v1/jobs/{check.json()['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))
        assert events[-1]["data"]["connection"]["status"] == "error"
        assert any(event["phase"] == "model_pull" for event in events)


def test_job_registry_enforces_ownership_and_one_active_job():
    async def exercise():
        registry = JobRegistry(poll_seconds=0.001)
        captured: dict[str, ProgressSink] = {}

        def runner(sink, cancel_event):
            captured["sink"] = sink
            while not cancel_event.is_set():
                time.sleep(0.001)
            return {"done": True}

        first = await registry.start(
            kind="generation",
            owner="owner-a",
            thread_id="thread-1",
            runner=runner,
        )
        await asyncio.sleep(0.01)
        try:
            await registry.start(
                kind="generation",
                owner="owner-a",
                thread_id="thread-1",
                runner=runner,
            )
        except JobConflict:
            pass
        else:
            raise AssertionError("second active generation was accepted")
        try:
            registry.status(first.job_id, owner="owner-b")
        except JobOwnershipError:
            pass
        else:
            raise AssertionError("foreign job access was accepted")
        registry.cancel(first.job_id, owner="owner-a")
        before = registry.status(first.job_id, owner="owner-a").sequence
        captured["sink"].publish(
            ProgressEvent(
                job_id=first.job_id,
                thread_id="thread-1",
                phase="analysis",
                message="stale callback",
            )
        )
        assert registry.status(first.job_id, owner="owner-a").sequence == before
        await registry.shutdown()

    asyncio.run(exercise())
