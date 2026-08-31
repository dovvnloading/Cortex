"""Headless API, session, job, and SSE contract tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.api.routes import _generation_snapshot, _model_sets
from cortex_backend.api.jobs import JobConflict, JobOwnershipError, JobRegistry
from cortex_backend.api.security import SessionManager, SessionSecurityError
from cortex_backend.api.schemas import GenerationRequest
from cortex_backend.core.settings import CortexSettings, TranslationSettings
from cortex_backend.services.chat import ChatDomainError
from cortex_backend.services.progress import ProgressEvent, ProgressSink
from cortex_backend.testing.fake_ollama import FakeOllamaState, create_fake_ollama_app
from support import session_headers as _session


ALLOWED_HOSTS = ("testserver", "127.0.0.1", "localhost", "::1")


def _client(state: FakeOllamaState | None = None):
    app = create_app(
        build_demo_dependencies(ollama_state=state),
        allowed_hosts=ALLOWED_HOSTS,
    )
    return app, TestClient(app)



def _events(body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_generation_stream_openapi_declares_sse_media_type():
    app = create_app(allowed_hosts=ALLOWED_HOSTS)
    response = app.openapi()["paths"]["/api/v1/generations/{job_id}/events"][
        "get"
    ]["responses"]["200"]

    assert response["description"] == "Server-sent generation events."
    assert response["content"] == {
        "text/event-stream": {
            "schema": {"$ref": "#/components/schemas/GenerationEvent"}
        }
    }


def test_authenticated_openapi_declares_bearer_security_and_execution_sse_contract():
    app = create_app(allowed_hosts=ALLOWED_HOSTS)
    specification = app.openapi()
    execution_events = specification["paths"]["/api/v1/execution/{job_id}/events"]["get"]
    handoff = specification["paths"]["/api/v1/session/handoff"]["post"]

    assert specification["components"]["securitySchemes"]["CortexSession"] == {
        "type": "http",
        "description": (
            "Short-lived bearer session token returned by /session/exchange. "
            "Requests remain restricted to the local API host."
        ),
        "scheme": "bearer",
    }
    assert execution_events["security"] == [{"CortexSession": []}]
    assert {
        parameter["name"]: parameter
        for parameter in execution_events["parameters"]
    }["Last-Event-ID"] == {
        "name": "Last-Event-ID",
        "in": "header",
        "required": False,
        "schema": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Resume after this event sequence number.",
            "title": "Last-Event-Id",
        },
        "description": "Resume after this event sequence number.",
    }
    assert execution_events["responses"]["200"]["content"] == {
        "text/event-stream": {
            "schema": {"$ref": "#/components/schemas/ExecutionSSEEvent"}
        }
    }
    assert execution_events["responses"]["200"]["headers"] == {
        "Cache-Control": {
            "description": "Prevent intermediary caching of the live event stream.",
            "schema": {"type": "string"},
        },
        "X-Accel-Buffering": {
            "description": "Disable proxy buffering for incremental events.",
            "schema": {"type": "string", "enum": ["no"]},
        },
    }
    handoff_header = handoff["parameters"][0]
    assert handoff_header["name"] == "X-Cortex-Handoff"
    assert handoff_header["in"] == "header"
    assert handoff_header["required"] is False


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


def test_session_exchange_rejects_non_ascii_bootstrap_token_cleanly():
    app, client = _client()
    with client:
        response = client.post(
            "/api/v1/session/exchange",
            json={"bootstrap_token": "café-token"},
        )
        assert response.status_code == 401


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


def test_authenticate_slides_the_session_expiry_forward():
    """Regression guard: a session's expiry used to be fixed at issuance, so
    the desktop app hard-locked after exactly one hour of continuous use --
    the frontend has no way to reach a fresh bootstrap token once its only
    credential is destroyed after the initial handoff. Every successful
    authenticate() must extend expires_at, so a session that is actually
    being used never expires mid-session.
    """
    manager = SessionManager(bootstrap_token="bootstrap", ttl_seconds=3600, allowed_hosts=("testserver",))
    exchanged = manager.exchange("bootstrap")
    digest = manager._digest(exchanged.token)
    # 50 minutes into a 60-minute TTL -- still valid, but would expire in
    # 10 more minutes without a renewal.
    stale_issued_at = datetime.now(timezone.utc) - timedelta(minutes=50)
    manager._sessions[digest] = replace(
        exchanged.principal,
        issued_at=stale_issued_at,
        expires_at=stale_issued_at + timedelta(seconds=3600),
    )
    old_expiry = manager._sessions[digest].expires_at

    principal = manager.authenticate(exchanged.token)

    assert principal.expires_at > old_expiry
    assert manager._sessions[digest].expires_at == principal.expires_at
    # And the renewed session is genuinely usable well past the original
    # one-hour mark, not just nominally not-yet-expired.
    assert principal.expires_at > datetime.now(timezone.utc) + timedelta(minutes=55)


def test_authenticate_caps_the_sliding_expiry_at_the_absolute_max_lifetime():
    """A session cannot renew itself forever -- continuous use still hits
    an absolute lifetime cap rather than sliding indefinitely."""
    manager = SessionManager(
        bootstrap_token="bootstrap",
        ttl_seconds=3600,
        max_lifetime_seconds=7200,
        allowed_hosts=("testserver",),
    )
    exchanged = manager.exchange("bootstrap")
    digest = manager._digest(exchanged.token)
    issued_at = datetime.now(timezone.utc) - timedelta(hours=1, minutes=55)  # close to the 2h cap
    manager._sessions[digest] = replace(
        exchanged.principal,
        issued_at=issued_at,
        # Not yet expired, but well below the eventual ~5-minute-away cap --
        # a realistic pre-renewal state, unlike setting it past the cap.
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    principal = manager.authenticate(exchanged.token)

    assert principal.expires_at <= issued_at + timedelta(hours=2)
    assert principal.expires_at > datetime.now(timezone.utc) + timedelta(minutes=1)


def test_generation_selects_a_live_local_model_and_translation_is_opt_in():
    settings = CortexSettings()
    snapshot = _generation_snapshot(
        "job-1",
        GenerationRequest(user_input="hello"),
        settings,
        ("local-chat:9b",),
    )

    assert snapshot.model == "local-chat:9b"
    assert snapshot.title_model == "local-chat:9b"
    assert _model_sets(settings) == ((), ())

    translation_enabled = settings.model_copy(
        update={"translation": TranslationSettings(enabled=True)}
    )
    assert _model_sets(translation_enabled) == ((), ("translategemma:4b",))
    with pytest.raises(ChatDomainError):
        _generation_snapshot(
            "job-2",
            GenerationRequest(user_input="hello"),
            translation_enabled,
            ("local-chat:9b",),
        )


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


def test_generation_input_is_trimmed_and_rejects_invisible_text():
    assert GenerationRequest(user_input="  hello\n").user_input == "hello"
    with pytest.raises(ValueError, match="visible text"):
        GenerationRequest(user_input="\u200b")


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
        assert cancelled.json()["status"] == "cancelling"
        assert (
            client.post(
                "/api/v1/jobs/generation",
                json={"thread_id": "thread-1", "user_input": "still blocked"},
                headers=headers,
            ).status_code
            == 409
        )
        with client.stream(
            "GET", f"/api/v1/jobs/{first.json()['job_id']}/events", headers=headers
        ) as response:
            events = _events("".join(response.iter_text()))
        assert [event["status"] for event in events][-2:] == [
            "cancelling",
            "cancelled",
        ]


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
        assert events[-1]["data"]["connection"]["status"] == "connected"
        assert not any(event["phase"] == "model_pull" for event in events)


def test_job_registry_enforces_ownership_and_one_active_job():
    async def exercise():
        registry = JobRegistry(poll_seconds=0.001)
        captured: dict[str, ProgressSink] = {}
        worker_started = Event()
        release_worker = Event()

        def runner(sink, cancel_event):
            captured["sink"] = sink
            worker_started.set()
            while not cancel_event.is_set():
                time.sleep(0.001)
            release_worker.wait(timeout=1)
            return {"done": True}

        try:
            first = await registry.start(
                kind="generation",
                owner="owner-a",
                thread_id="thread-1",
                runner=runner,
            )
            for _ in range(100):
                if worker_started.is_set():
                    break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError("generation worker did not start")

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

            requested = registry.cancel(first.job_id, owner="owner-a")
            assert requested.status == "cancelling"
            assert requested.error is None
            assert registry.active_snapshot(kind="generation") == requested

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
                raise AssertionError("cancelling generation released the active slot")

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

            release_worker.set()
            for _ in range(100):
                finished = registry.status(first.job_id, owner="owner-a")
                if finished.status == "cancelled":
                    break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError("cancelling generation did not finish")
            assert finished.error == "Job cancelled."
            assert registry.active_snapshot(kind="generation") is None
            events = [
                event
                async for event in registry.events(first.job_id, owner="owner-a")
            ]
            assert [event.status for event in events][-2:] == [
                "cancelling",
                "cancelled",
            ]
        finally:
            release_worker.set()
            await registry.shutdown()

    asyncio.run(exercise())


def test_job_registry_commit_barrier_linearizes_cancellation():
    async def wait_for_event(event: Event):
        for _ in range(200):
            if event.is_set():
                return
            await asyncio.sleep(0.001)
        raise AssertionError("worker did not reach its synchronization point")

    async def wait_for_status(
        registry: JobRegistry, job_id: str, expected: str
    ):
        for _ in range(200):
            snapshot = registry.status(job_id, owner="owner")
            if snapshot.status == expected:
                return snapshot
            await asyncio.sleep(0.001)
        raise AssertionError(f"job did not reach {expected}")

    async def exercise():
        registry = JobRegistry(poll_seconds=0.001)
        before_barrier = Event()
        release_before_barrier = Event()
        after_barrier = Event()
        release_after_barrier = Event()
        barrier_results: list[bool] = []

        def cancellable_runner(sink, _cancel_event):
            before_barrier.set()
            release_before_barrier.wait(timeout=1)
            barrier_results.append(
                sink.begin_commit("persisting", "Saving the response.")
            )
            return {"persisted": barrier_results[-1]}

        def committed_runner(sink, _cancel_event):
            barrier_results.append(
                sink.begin_commit("persisting", "Saving the response.")
            )
            after_barrier.set()
            release_after_barrier.wait(timeout=1)
            return {"persisted": True}

        try:
            cancellable = await registry.start(
                kind="generation",
                owner="owner",
                thread_id="thread-before",
                runner=cancellable_runner,
            )
            await wait_for_event(before_barrier)
            assert registry.cancel(cancellable.job_id, owner="owner").status == "cancelling"
            release_before_barrier.set()
            cancelled = await wait_for_status(
                registry, cancellable.job_id, "cancelled"
            )
            assert cancelled.result is None
            assert barrier_results == [False]
            cancelled_events = [
                event
                async for event in registry.events(cancellable.job_id, owner="owner")
            ]
            assert not any(event.phase == "persisting" for event in cancelled_events)

            committed = await registry.start(
                kind="generation",
                owner="owner",
                thread_id="thread-after",
                runner=committed_runner,
            )
            await wait_for_event(after_barrier)
            too_late = registry.cancel(committed.job_id, owner="owner")
            assert too_late.status == "running"
            release_after_barrier.set()
            succeeded = await wait_for_status(registry, committed.job_id, "succeeded")
            assert succeeded.result == {"persisted": True}
            assert barrier_results == [False, True]
            committed_events = [
                event
                async for event in registry.events(committed.job_id, owner="owner")
            ]
            assert any(event.phase == "persisting" for event in committed_events)
            assert not any(event.status == "cancelling" for event in committed_events)
            assert not any(event.status == "cancelled" for event in committed_events)
        finally:
            release_before_barrier.set()
            release_after_barrier.set()
            await registry.shutdown()

    asyncio.run(exercise())


def test_job_registry_shutdown_cancels_only_before_commit():
    async def exercise():
        async def wait_for_event(event: Event):
            for _ in range(200):
                if event.is_set():
                    return
                await asyncio.sleep(0.001)
            raise AssertionError("worker did not reach its synchronization point")

        before_registry = JobRegistry(poll_seconds=0.001)
        before_started = Event()

        def before_runner(_sink, cancel_event):
            before_started.set()
            cancel_event.wait(timeout=1)
            return {"persisted": False}

        before = await before_registry.start(
            kind="generation",
            owner="owner",
            thread_id="thread-before-shutdown",
            runner=before_runner,
        )
        await wait_for_event(before_started)
        await before_registry.shutdown()
        assert before_registry.status(before.job_id, owner="owner").status == "cancelled"

        after_registry = JobRegistry(poll_seconds=0.001)
        after_barrier = Event()
        release_after_barrier = Event()

        def after_runner(sink, _cancel_event):
            assert sink.begin_commit("persisting", "Saving the response.")
            after_barrier.set()
            release_after_barrier.wait(timeout=1)
            return {"persisted": True}

        after = await after_registry.start(
            kind="generation",
            owner="owner",
            thread_id="thread-after-shutdown",
            runner=after_runner,
        )
        await wait_for_event(after_barrier)
        shutdown = asyncio.create_task(after_registry.shutdown())
        await asyncio.sleep(0.01)
        assert not shutdown.done()
        assert after_registry.status(after.job_id, owner="owner").status == "running"
        release_after_barrier.set()
        await shutdown
        assert after_registry.status(after.job_id, owner="owner").status == "succeeded"

    asyncio.run(exercise())


def test_job_registry_shutdown_is_bounded_for_a_worker_that_never_observes_cancellation():
    """Regression guard: shutdown() used to await every pending worker with
    no bound at all, including one stuck inside a synchronous call that
    never polls cancel_event (a model HTTP request with no read deadline,
    for example). That hung app shutdown -- and the llama-server child
    process behind it -- for as long as that call took, sometimes forever.
    A worker that has not begun committing its result must now be
    abandoned once the grace period elapses so shutdown always completes
    in bounded time.
    """
    async def exercise():
        registry = JobRegistry(poll_seconds=0.001, shutdown_grace_seconds=0.05)
        started = Event()
        never_released = Event()

        def stuck_runner(_sink, _cancel_event):
            # Never checks _cancel_event -- simulates a blocking call (e.g.
            # a socket read with no deadline) that ignores cancellation.
            started.set()
            never_released.wait(timeout=5)
            return {"persisted": False}

        job = await registry.start(
            kind="generation",
            owner="owner",
            thread_id="thread-stuck",
            runner=stuck_runner,
        )
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("worker did not start")

        loop = asyncio.get_event_loop()
        started_at = loop.time()
        await asyncio.wait_for(registry.shutdown(), timeout=1.0)
        elapsed = loop.time() - started_at

        assert elapsed < 0.5, f"shutdown() took {elapsed:.2f}s, expected it bounded near the 0.05s grace period"
        assert registry.status(job.job_id, owner="owner").status == "cancelling"
        never_released.set()

    asyncio.run(exercise())


def test_lifespan_runtime_teardown_runs_even_if_job_shutdown_raises():
    """Regression guard: the lifespan finally block used to await job
    shutdown unconditionally before tearing down the runtime, so an
    exception there (or, before the bounded-shutdown fix, an indefinite
    hang) would skip llamacpp_manager.stop() entirely and leave the
    llama-server child process orphaned. Runtime teardown must run
    regardless of whether job shutdown succeeds.
    """
    class _RaisingJobs:
        async def shutdown(self):
            raise RuntimeError("boom")

    class _FakeLlamaManager:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    fake_manager = _FakeLlamaManager()
    app = create_app(allowed_hosts=ALLOWED_HOSTS, llamacpp_manager=fake_manager)
    app.state.jobs = _RaisingJobs()

    with TestClient(app):
        pass

    assert fake_manager.stopped is True


def test_concurrent_settings_updates_have_one_winner_and_no_lost_overwrite():
    app, client = _client()
    with client:
        headers = _session(client, app)
        baseline = client.get("/api/v1/settings", headers=headers).json()["settings"]
        payloads = []
        for theme in ("light", "system"):
            changed = dict(baseline)
            changed["appearance"] = {**baseline["appearance"], "theme": theme}
            payloads.append({"settings": changed, "expected_revision": baseline["revision"]})

        def put(payload):
            return client.put("/api/v1/settings", json=payload, headers=headers)

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(put, payloads))

        assert sorted(response.status_code for response in responses) == [200, 409]
        saved = client.get("/api/v1/settings", headers=headers).json()["settings"]
        assert saved["revision"] == baseline["revision"] + 1
        assert saved["appearance"]["theme"] in {"light", "system"}
