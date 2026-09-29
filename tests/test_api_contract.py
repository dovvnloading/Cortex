"""Headless API, session, job, and SSE contract tests."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from fastapi.testclient import TestClient
import pytest

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.api.routes import _generation_snapshot, _model_sets
from cortex_backend.api.jobs import JobConflict, JobOwnershipError, JobRegistry
from cortex_backend.api.security import SessionManager, SessionSecurityError
from cortex_backend.api.schemas import GenerationRequest
from cortex_backend.core.settings import CortexSettings, MemorySettings, TranslationSettings
from cortex_backend.repositories.settings import InMemorySettingsRepository
from cortex_backend.services.chat import ChatDomainError
from cortex_backend.services.progress import ProgressEvent, ProgressSink
from cortex_backend.testing.fake_ollama import FakeOllamaState, create_fake_ollama_app
from support import parse_sse_events as _events
from support import session_headers as _session
from support import wait_until


ALLOWED_HOSTS = ("testserver", "127.0.0.1", "localhost", "::1")


def _client(state: FakeOllamaState | None = None):
    app = create_app(
        build_demo_dependencies(ollama_state=state),
        allowed_hosts=ALLOWED_HOSTS,
    )
    return app, TestClient(app)




def test_generation_stream_openapi_declares_sse_media_type():
    app = create_app(build_demo_dependencies(), allowed_hosts=ALLOWED_HOSTS)
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
    app = create_app(build_demo_dependencies(), allowed_hosts=ALLOWED_HOSTS)
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
                sys.executable,
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
    default_app = create_app(build_demo_dependencies())
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


def test_trusted_host_middleware_accepts_ipv6_loopback_like_the_session_guard():
    """Starlette's TrustedHostMiddleware reduces the IPv6 loopback Host header
    ("[::1]" or "[::1]:PORT") to "[" before 1.7 (a naive
    ``host.split(":")[0]``) and to "[::1]" from 1.7 (``parse_host_header``).
    Each version once rejected every IPv6-loopback request with a 400 --
    even though SessionManager.validate_request_context (security.py)
    parses the same header itself and accepts "::1" as configured. Both
    guards must agree on what counts as valid loopback, on every Starlette
    the requirements range admits; the compatibility matrix installs the
    newest one, the lockfile pins an older one.
    """
    app, client = _client()
    with client:
        headers = _session(client, app)
        for host_header in ("[::1]:51173", "[::1]"):
            response = client.get(
                "/api/v1/system", headers={**headers, "Host": host_header}
            )
            assert response.status_code == 200, host_header


def test_a_re_exchanged_session_still_owns_the_jobs_it_started():
    """A job outlives the bearer session that started it.

    ``SessionManager.exchange`` mints a fresh random ``session_id`` every
    time a session is re-exchanged -- an app restart, a token refresh, the
    ``/session/handoff`` flow -- while the installation principal stays
    fixed. Registry jobs (generation, models, gguf_download) are therefore
    owned by the installation principal, not the session: owning them by
    session id left a re-exchanged session unable to inspect or cancel work
    it had started itself, even though the registry's
    one-active-job-per-kind rule still counted that work against it.
    """
    app, client = _client()
    with client:
        first = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "durable-owner-generation", "user_input": "hello"},
            headers=first,
        )
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        with client.stream(
            "GET", f"/api/v1/generations/{job_id}/events", headers=first
        ) as response:
            assert _events("".join(response.iter_text()))

        # Re-exchange: a brand new bearer session for the same installation.
        bootstrap_token, _expires_at = app.state.session_manager.issue_bootstrap_token()
        exchanged = client.post(
            "/api/v1/session/exchange", json={"bootstrap_token": bootstrap_token}
        )
        assert exchanged.status_code == 200, exchanged.text
        second = {"Authorization": f"Bearer {exchanged.json()['session_token']}"}
        assert second != first

        status_response = client.get(f"/api/v1/generations/{job_id}", headers=second)
        assert status_response.status_code == 200, status_response.text
        assert status_response.json()["job_id"] == job_id
        assert status_response.json()["status"] == "succeeded"

        # The event stream performs the same ownership check before it opens.
        with client.stream(
            "GET", f"/api/v1/generations/{job_id}/events", headers=second
        ) as replay:
            assert replay.status_code == 200, replay.text


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


def test_expired_sessions_are_removed_in_bounded_cleanup_batches():
    manager = SessionManager(
        bootstrap_token="bootstrap",
        ttl_seconds=60,
        allowed_hosts=("testserver",),
    )
    exchanged = manager.exchange("bootstrap")
    digest = manager._digest(exchanged.token)
    manager._sessions[digest] = replace(
        exchanged.principal,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    assert manager.cleanup_expired(limit=1) == 1
    assert digest not in manager._sessions
    assert manager.cleanup_expired(limit=1) == 0

    with pytest.raises(ValueError, match="cleanup limit"):
        manager.cleanup_expired(limit=0)


def test_session_cleanup_caps_large_caller_limits_and_rotates_live_entries():
    manager = SessionManager(
        bootstrap_token="bootstrap",
        ttl_seconds=60,
        allowed_hosts=("testserver",),
    )
    exchanged = manager.exchange("bootstrap")
    live = replace(exchanged.principal, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    for index in range(manager._EXPIRY_CLEANUP_BATCH):
        manager._sessions[f"live-{index}"] = live
    expired_digest = "expired-after-live"
    manager._sessions[expired_digest] = replace(
        live,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    assert manager.cleanup_expired(limit=10_000) == 0
    assert expired_digest in manager._sessions
    assert manager.cleanup_expired(limit=10_000) == 1
    assert expired_digest not in manager._sessions

    with pytest.raises(ValueError, match="cleanup limit"):
        manager.cleanup_expired(limit=True)


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
            == 422
        )
        assert client.post(
            "/api/v1/memories/clear", json={"confirm": True}, headers=headers
        ).json() == {"memos": []}

        models = client.get("/api/v1/models", headers=headers)
        assert models.status_code == 200
        assert "qwen3:8b" in models.json()["installed_models"]


def test_add_message_rejects_malformed_new_chat_thread_id():
    """A client-chosen thread_id only becomes a new chat's id if it is safe.

    ``POST /chats/{thread_id}/messages`` creates a brand new chat using the
    literal path segment as its permanent id whenever no chat with that id
    exists yet. A pathological id (whitespace, a slash-like sequence, a
    control character, ...) must be rejected with 422 before that happens.
    """

    dependencies = build_demo_dependencies()
    app = create_app(dependencies, allowed_hosts=ALLOWED_HOSTS)
    with TestClient(app) as client:
        headers = _session(client, app)
        for bad_thread_id in ("has space", "control\x07char", "semi;colon"):
            encoded = quote(bad_thread_id, safe="")
            response = client.post(
                f"/api/v1/chats/{encoded}/messages",
                json={"role": "user", "content": "hello"},
                headers=headers,
            )
            assert response.status_code == 422, bad_thread_id
            assert "thread_id" in response.json()["detail"]
            assert dependencies.chats.get_chat(bad_thread_id) is None


def test_add_message_with_valid_new_thread_id_creates_chat():
    """A well-formed client-chosen thread_id may still create a brand new chat."""

    dependencies = build_demo_dependencies()
    app = create_app(dependencies, allowed_hosts=ALLOWED_HOSTS)
    with TestClient(app) as client:
        headers = _session(client, app)
        new_thread_id = "Client-Chosen_Thread-123"
        response = client.post(
            f"/api/v1/chats/{new_thread_id}/messages",
            json={"role": "user", "content": "hello"},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["id"] == new_thread_id
        chat = dependencies.chats.get_chat(new_thread_id)
        assert chat is not None
        assert chat["messages"][0]["content"] == "hello"


def test_add_message_to_preexisting_nonconforming_chat_id_still_works():
    """A chat id that predates this check keeps working unconditionally.

    The new format check only ever gates chat *creation*. Looking up or
    appending to an already-existing chat -- however it got its id -- must
    keep succeeding for backward compatibility with any local database
    populated before this validation existed.
    """

    dependencies = build_demo_dependencies()
    legacy_thread_id = "legacy chat id!"
    dependencies.chats.create_chat(legacy_thread_id, "Legacy Chat")
    app = create_app(dependencies, allowed_hosts=ALLOWED_HOSTS)
    with TestClient(app) as client:
        headers = _session(client, app)
        encoded = quote(legacy_thread_id, safe="")
        response = client.post(
            f"/api/v1/chats/{encoded}/messages",
            json={"role": "user", "content": "hello"},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["id"] == legacy_thread_id
        chat = dependencies.chats.get_chat(legacy_thread_id)
        assert chat["messages"][0]["content"] == "hello"


def test_memory_clear_without_confirmation_is_a_client_validation_error():
    """A missing `confirm` flag is an invalid request body, not a state conflict."""

    app, client = _client()
    with client:
        headers = _session(client, app)
        client.post("/api/v1/memories", json={"memo": "Alice"}, headers=headers)

        missing = client.post("/api/v1/memories/clear", json={}, headers=headers)
        assert missing.status_code == 422
        assert missing.json()["detail"] == (
            "Clearing permanent memories requires explicit confirmation."
        )

        unconfirmed = client.post(
            "/api/v1/memories/clear", json={"confirm": False}, headers=headers
        )
        assert unconfirmed.status_code == 422
        assert isinstance(unconfirmed.json()["detail"], str)


def test_generation_input_is_trimmed_and_rejects_invisible_text():
    assert GenerationRequest(user_input="  hello\n").user_input == "hello"
    with pytest.raises(ValueError, match="visible text"):
        GenerationRequest(user_input="\u200b")


def test_generation_sse_is_ordered_replayable_and_redacts_failures(caplog):
    app, client = _client()
    with client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
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
            "/api/v1/generations",
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
            "/api/v1/generations",
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
            "/api/v1/generations",
            json={"thread_id": "thread-1", "user_input": "slow"},
            headers=headers,
        )
        second = client.post(
            "/api/v1/generations",
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
                "/api/v1/generations",
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
            # Bounded wait: a cancel that never arrives fails the job (and
            # the assertions below) instead of parking this thread forever.
            assert cancel_event.wait(timeout=5.0), "the job was never cancelled"
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
            self.closed = False

        def stop(self):
            self.stopped = True

        def close(self):
            self.closed = True

    class _FakeLlamaChatClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    fake_manager = _FakeLlamaManager()
    fake_chat_client = _FakeLlamaChatClient()
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        llamacpp_manager=fake_manager,
        llamacpp_chat_client=fake_chat_client,
    )
    app.state.jobs = _RaisingJobs()

    with TestClient(app):
        pass

    assert fake_manager.closed is True
    assert fake_manager.stopped is False
    assert fake_chat_client.closed is True


def test_lifespan_closes_llama_resources_when_execution_shutdown_raises():
    class _RaisingCoordinator:
        class _Repository:
            installation_principal_id = None

        repository = _Repository()

        def shutdown(self):
            raise RuntimeError("synthetic coordinator failure")

    class _FakeLlamaManager:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class _FakeLlamaChatClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    fake_manager = _FakeLlamaManager()
    fake_chat_client = _FakeLlamaChatClient()
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        execution_coordinator=_RaisingCoordinator(),
        llamacpp_manager=fake_manager,
        llamacpp_chat_client=fake_chat_client,
    )

    with TestClient(app):
        pass

    assert fake_manager.closed is True
    assert fake_chat_client.closed is True


def test_lifespan_closes_llama_resources_when_execution_start_raises():
    class _RaisingLifecycle:
        class _Repository:
            installation_principal_id = None

        repository = _Repository()
        coordinator = None

        def start(self):
            raise RuntimeError("synthetic startup failure")

        def stop(self):
            self.stop_called = True

    class _FakeLlamaManager:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class _FakeLlamaChatClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    fake_manager = _FakeLlamaManager()
    fake_chat_client = _FakeLlamaChatClient()
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        execution_lifecycle=_RaisingLifecycle(),
        llamacpp_manager=fake_manager,
        llamacpp_chat_client=fake_chat_client,
    )

    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        with TestClient(app):
            pass

    assert fake_manager.closed is True
    assert fake_chat_client.closed is True


def test_lifespan_closes_owned_clients_and_keeps_going_when_one_raises():
    """The Ollama client is closed at shutdown, and one bad close stops nothing.

    An abandoned worker blocked in an Ollama read is otherwise joined by the
    interpreter's exit hook, which can wait out the 600 second read timeout.
    """

    class _Closeable:
        def __init__(self, *, raises: bool = False) -> None:
            self.closed = False
            self._raises = raises

        def close(self) -> None:
            self.closed = True
            if self._raises:
                raise RuntimeError("synthetic close failure")

    failing = _Closeable(raises=True)
    healthy = _Closeable()
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        closeables=(failing, healthy, object()),
    )

    with TestClient(app):
        assert not failing.closed and not healthy.closed

    assert failing.closed is True
    assert healthy.closed is True


def test_build_app_hands_the_ollama_client_to_the_lifespan_to_close(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    import app_factory

    closed: list[object] = []

    class _RecordingClient(app_factory.ollama.Client):
        def close(self) -> None:
            closed.append(self)
            super().close()

    monkeypatch.setattr(app_factory.ollama, "Client", _RecordingClient)
    app = app_factory.build_app(data_dir=tmp_path / "app-data", serve_frontend=False)

    with TestClient(app):
        assert closed == []

    assert len(closed) == 1
    assert isinstance(closed[0], _RecordingClient)


class _HoldsUntilCancelledEngine:
    """A generation that stays open until it is asked to stop."""

    def __init__(self, inner, *, started: Event) -> None:
        self._inner = inner
        self._started = started

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def generate(self, *, cancellation_event=None, **_kwargs):
        from cortex_backend.core.generation import ModelOperationError

        self._started.set()
        assert cancellation_event is not None
        if not cancellation_event.wait(10):
            raise AssertionError("the generation was never cancelled")
        raise ModelOperationError("Generation was cancelled.", operation="generation")


def _read_stream_in_thread(client: TestClient, url: str, headers: dict[str, str]):
    """Open ``url`` on a thread; return (thread, finished event, body holder)."""

    finished = Event()
    body: list[str] = []

    def read() -> None:
        try:
            with client.stream("GET", url, headers=headers) as response:
                body.append("".join(response.iter_text()))
        finally:
            finished.set()

    thread = Thread(target=read, name="test-open-stream", daemon=True)
    thread.start()
    return thread, finished, body


def test_system_shutdown_ends_an_open_generation_stream_promptly():
    """Quitting mid-generation must not wait on a stream that waits on the quit.

    uvicorn drains open responses before it runs the lifespan teardown that
    cancels jobs, and a generation stream only ends when its job does -- so
    with the stream attached, nothing ever finished. Shutdown now cancels the
    jobs first, and the stream both delivers that outcome and ends.
    """

    started = Event()
    dependencies = build_demo_dependencies()
    inner_factory = dependencies.generation._engine_factory
    dependencies.generation._engine_factory = lambda snapshot: _HoldsUntilCancelledEngine(
        inner_factory(snapshot), started=started
    )
    app = create_app(dependencies, allowed_hosts=ALLOWED_HOSTS)
    server_stops: list[bool] = []
    app.state.shutdown_callback = lambda: server_stops.append(True)

    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "shutdown-1", "user_input": "explain it"},
            headers=headers,
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        assert started.wait(10)

        thread, finished, body = _read_stream_in_thread(
            client, f"/api/v1/generations/{job_id}/events", headers
        )
        # The stream is open once the registry counts it as an attached reader.
        wait_until(
            lambda: app.state.jobs._records[job_id].live_cursors,
            describe="the event stream to attach",
        )
        assert not finished.is_set()

        began = time.monotonic()
        shutdown = client.post("/api/v1/system/shutdown", headers=headers)
        assert shutdown.status_code == 200
        assert finished.wait(5), "the open generation stream outlived the shutdown request"
        elapsed = time.monotonic() - began
        thread.join(timeout=5)

    assert server_stops == [True]
    assert elapsed < 3.0
    names = [event["event"] for event in _events(body[0])]
    assert names[-2:] == ["generation.cancelling", "generation.cancelled"]


def test_system_shutdown_ends_an_open_execution_stream_promptly(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A job parked on an approval emits nothing, so its stream had no end at all."""

    from cortex_backend.api.routers import execution as execution_routes
    from cortex_backend.execution.repository import ExecutionRepository
    from cortex_backend.testing import DurableFakeCoordinator, install_execution_preview

    # Without the fix the stream ends on this cap; make it long enough that the
    # assertion below can only be met by observing the shutdown.
    monkeypatch.setattr(execution_routes, "EXECUTION_STREAM_IDLE_TIMEOUT_SECONDS", 8.0)
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    app = install_execution_preview(
        create_app(
            build_demo_dependencies(),
            allowed_hosts=ALLOWED_HOSTS,
            execution_coordinator=DurableFakeCoordinator(repository),
        )
    )
    app.state.shutdown_callback = lambda: None

    with TestClient(app) as client:
        headers = _session(client, app)
        token = headers["Authorization"].removeprefix("Bearer ")
        owner = app.state.session_manager.authenticate(token).installation_principal_id
        repository.create_job(
            job_id="parked-job",
            owner=owner,
            request_id="parked-request",
            profile="artifact.extended.v1",
            payload={},
        )
        repository.request_approval(
            "parked-job",
            owner=owner,
            scope_digest="server-bound-scope",
            reason="Create a larger staged image preview.",
            ttl_seconds=60.0,
        )

        thread, finished, body = _read_stream_in_thread(
            client, "/api/v1/execution/parked-job/events", headers
        )
        assert not finished.wait(0.5), "the stream ended before shutdown was requested"

        began = time.monotonic()
        assert client.post("/api/v1/system/shutdown", headers=headers).status_code == 200
        assert finished.wait(5), "the open execution stream outlived the shutdown request"
        elapsed = time.monotonic() - began
        thread.join(timeout=5)

    assert elapsed < 3.0
    assert "execution.queued" in body[0]


def test_job_registry_begin_shutdown_cancels_jobs_without_waiting_for_them():
    async def exercise():
        registry = JobRegistry(poll_seconds=0.001)
        started = Event()
        release = Event()

        def runner(_sink, cancel_event):
            started.set()
            release.wait(timeout=5)
            return {"cancelled": cancel_event.is_set()}

        job = await registry.start(
            kind="generation", owner="owner", thread_id="thread-1", runner=runner
        )
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("worker did not start")

        registry.begin_shutdown()
        registry.begin_shutdown()  # a second call, as the lifespan teardown makes, is inert

        snapshot = registry.status(job.job_id, owner="owner")
        assert snapshot.status == "cancelling"
        events = [event.status for event in registry._records[job.job_id].events]
        assert events.count("cancelling") == 1
        try:
            await registry.start(
                kind="generation", owner="owner", thread_id="thread-2", runner=runner
            )
        except JobConflict:
            pass
        else:
            raise AssertionError("a registry that began shutting down accepted new work")

        release.set()
        await registry.shutdown()
        assert registry.status(job.job_id, owner="owner").status == "cancelled"

    asyncio.run(exercise())


def test_job_event_stream_ends_when_told_to_stop_even_if_its_job_never_finishes(
    monkeypatch: pytest.MonkeyPatch,
):
    from cortex_backend.api import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "STREAM_STOP_FLUSH_SECONDS", 0.05)

    async def exercise():
        registry = JobRegistry(poll_seconds=0.001, shutdown_grace_seconds=0.05)
        started = Event()
        release = Event()

        def stuck_runner(_sink, _cancel_event):
            started.set()
            release.wait(timeout=5)  # ignores cancellation, like a blocked model read
            return {}

        job = await registry.start(
            kind="generation", owner="owner", thread_id="thread-1", runner=stuck_runner
        )
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("worker did not start")

        stopping = Event()
        seen: list[str] = []

        async def consume():
            async for event in registry.events(
                job.job_id, owner="owner", stop=stopping.is_set
            ):
                seen.append(event.status)

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        assert not consumer.done(), "the stream must stay open until it is told to stop"

        registry.begin_shutdown()
        stopping.set()
        await asyncio.wait_for(consumer, timeout=2.0)

        # It delivered what had been published, then ended without a terminal event.
        assert seen[-1] == "cancelling"
        assert registry.status(job.job_id, owner="owner").status == "cancelling"
        release.set()
        await registry.shutdown()

    asyncio.run(exercise())


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


def test_every_generation_event_the_api_can_emit_is_a_schema_event_name():
    """Three hand-kept lists meet here: the service's progress phases, the
    API's phase-to-event mapping, and the event schema's allowed names.

    A phase whose mapped name the schema did not allow ("translation_failed"
    once) made building its event raise inside the SSE generator, killing the
    live stream. Nothing linked the lists, so check every path out of the
    mapping, including phases added later.
    """
    from typing import get_args

    from cortex_backend.api.routes import (
        _GENERATION_PHASE_EVENTS,
        _generation_event_name,
    )
    from cortex_backend.api.schemas import GenerationEventName
    from cortex_backend.services.progress import ProgressPhase

    allowed = set(get_args(GenerationEventName))

    unknown = set(_GENERATION_PHASE_EVENTS.values()) - allowed
    assert not unknown, f"mapped to names the schema rejects: {sorted(unknown)}"

    phases = {*get_args(ProgressPhase), *_GENERATION_PHASE_EVENTS, None}
    emitted = {_generation_event_name("progress", "running", phase) for phase in phases}
    emitted |= {
        _generation_event_name("state", status, None)
        for status in ("queued", "running", "cancelling", "cancelled")
    }
    emitted |= {
        _generation_event_name("completed", "succeeded", None),
        _generation_event_name("error", "failed", None),
    }
    assert emitted <= allowed, f"emits names the schema rejects: {sorted(emitted - allowed)}"


# --- Model-proposed memories reach the user and are never saved by the model ---


def _run_generation(client: TestClient, headers: dict[str, str], **body) -> list[dict]:
    accepted = client.post("/api/v1/generations", json=body, headers=headers)
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]
    with client.stream(
        "GET", f"/api/v1/generations/{job_id}/events", headers=headers
    ) as response:
        return _events("".join(response.iter_text()))


def test_completed_generation_carries_proposed_memory_additions():
    """The model's memory suggestion reaches the client and nothing is saved.

    The event and the job result both carry the suggestion and the id of the
    saved answer it belongs to, so a client can put it under that answer. The
    store stays empty until the user saves it through the memories API.
    """
    dependencies = build_demo_dependencies()
    app = create_app(dependencies, allowed_hosts=ALLOWED_HOSTS)
    with TestClient(app) as client:
        headers = _session(client, app)
        events = _run_generation(
            client,
            headers,
            request_id="propose-1",
            user_input="!remember User likes tea.",
        )
        names = [event["event"] for event in events]
        completed = events[-1]
        assert completed["event"] == "generation.completed"
        assert names.count("generation.memory_proposed") == 1
        assert names.index("generation.memory_proposed") < len(names) - 1

        proposed = next(
            event for event in events if event["event"] == "generation.memory_proposed"
        )
        assert proposed["data"]["proposed_memories"] == ["User likes tea."]
        assert proposed["data"]["clear_requested"] is False
        assert proposed["data"]["assistant_message_id"] == (
            completed["data"]["assistant_message_id"]
        )
        assert completed["data"]["proposed_memories"] == ["User likes tea."]
        assert completed["data"]["clear_requested"] is False

        # Nothing was written on the model's say-so.
        assert dependencies.memories.get_memos() == []
        assert client.get("/api/v1/memories", headers=headers).json() == {"memos": []}

        # Accepting is the user's explicit call to the existing memories API.
        saved = client.post(
            "/api/v1/memories", json={"memo": "User likes tea."}, headers=headers
        )
        assert saved.status_code == 200
        assert saved.json() == {"memos": ["User likes tea."]}


def test_a_turn_without_memory_suggestions_announces_none():
    app = create_app(build_demo_dependencies(), allowed_hosts=ALLOWED_HOSTS)
    with TestClient(app) as client:
        headers = _session(client, app)
        events = _run_generation(
            client, headers, request_id="no-propose-1", user_input="hello"
        )
    assert "generation.memory_proposed" not in [event["event"] for event in events]
    assert events[-1]["data"]["proposed_memories"] == []
    assert events[-1]["data"]["clear_requested"] is False


def test_a_clear_request_is_announced_but_never_applied():
    dependencies = build_demo_dependencies()
    dependencies.memories.add_memo("keep this fact")
    app = create_app(dependencies, allowed_hosts=ALLOWED_HOSTS)
    with TestClient(app) as client:
        headers = _session(client, app)
        events = _run_generation(
            client, headers, request_id="clear-1", user_input="!clear-memory"
        )
    proposed = next(
        event for event in events if event["event"] == "generation.memory_proposed"
    )
    assert proposed["data"]["clear_requested"] is True
    assert proposed["data"]["proposed_memories"] == []
    assert events[-1]["data"]["clear_requested"] is True
    assert dependencies.memories.get_memos() == ["keep this fact"]


def test_a_suggestion_the_store_already_holds_is_not_offered_again():
    dependencies = build_demo_dependencies()
    dependencies.memories.add_memo("User likes tea.")
    app = create_app(dependencies, allowed_hosts=ALLOWED_HOSTS)
    with TestClient(app) as client:
        headers = _session(client, app)
        events = _run_generation(
            client,
            headers,
            request_id="known-1",
            user_input="!remember user likes TEA.",
        )
    assert "generation.memory_proposed" not in [event["event"] for event in events]
    assert events[-1]["data"]["proposed_memories"] == []


def test_no_memory_suggestion_is_announced_when_memory_is_turned_off():
    dependencies = replace(
        build_demo_dependencies(),
        settings=InMemorySettingsRepository(
            CortexSettings(memory=MemorySettings(enabled=False))
        ),
    )
    app = create_app(dependencies, allowed_hosts=ALLOWED_HOSTS)
    with TestClient(app) as client:
        headers = _session(client, app)
        events = _run_generation(
            client,
            headers,
            request_id="memory-off-1",
            user_input="!remember User likes tea.",
        )
    assert "generation.memory_proposed" not in [event["event"] for event in events]
    assert events[-1]["data"]["proposed_memories"] == []
    assert dependencies.memories.get_memos() == []


def test_the_api_bounds_what_an_engine_proposes():
    """An engine that hands back an oversized command still cannot flood the user."""
    from cortex_backend.core.generation import MemoryCommand
    from cortex_backend.testing.fake_ollama import FakeGenerationEngine

    class OverEagerEngine(FakeGenerationEngine):
        def generate(self, **kwargs):
            response, thoughts, _command, stats = super().generate(**kwargs)
            command = MemoryCommand(
                additions=tuple(f"Fact number {number}." for number in range(20))
            )
            return response, thoughts, command, stats

    dependencies = build_demo_dependencies()
    dependencies.generation._engine_factory = lambda snapshot: OverEagerEngine(
        FakeOllamaState()
    )
    app = create_app(dependencies, allowed_hosts=ALLOWED_HOSTS)
    with TestClient(app) as client:
        headers = _session(client, app)
        events = _run_generation(
            client, headers, request_id="flood-1", user_input="hello"
        )
    proposals = events[-1]["data"]["proposed_memories"]
    assert proposals == [f"Fact number {number}." for number in range(5)]
    assert dependencies.memories.get_memos() == []


def _stub_deps(memos=None, *, unreadable: bool = False):
    class _Memories:
        def get_memos(self):
            if unreadable:
                raise RuntimeError("store unavailable")
            return list(memos or [])

    class _Deps:
        memories = _Memories()

    return _Deps()


@pytest.mark.parametrize(
    ("additions", "stored", "expected"),
    [
        # Trimmed, and blank entries dropped.
        (("  keep me  ", "   ", ""), [], ["keep me"]),
        # Duplicates within the suggestion, ignoring case.
        (("Likes tea", "likes TEA", "Likes coffee"), [], ["Likes tea", "Likes coffee"]),
        # Already saved, ignoring case and outer whitespace.
        (("likes tea", "Likes coffee"), ["Likes Tea"], ["Likes coffee"]),
        # One oversized entry is dropped without hiding the others.
        (("x" * 501, "y" * 500), [], ["y" * 500]),
        # Non-text entries never reach the user.
        ((None, 7, ["nested"], "ok"), [], ["ok"]),
        # At most five.
        (tuple(f"fact {n}" for n in range(9)), [], [f"fact {n}" for n in range(5)]),
    ],
)
def test_proposed_memories_are_bounded_distinct_and_new(additions, stored, expected):
    from cortex_backend.api.routes import _proposed_memories
    from cortex_backend.core.generation import MemoryCommand

    command = MemoryCommand(additions=additions)  # type: ignore[arg-type]
    assert _proposed_memories(_stub_deps(stored), command) == expected


def test_proposed_memories_still_reach_the_user_when_the_store_is_unreadable():
    from cortex_backend.api.routes import _proposed_memories
    from cortex_backend.core.generation import MemoryCommand

    command = MemoryCommand(additions=("Likes tea",))
    assert _proposed_memories(_stub_deps(unreadable=True), command) == ["Likes tea"]


@pytest.mark.parametrize("command", [None, object(), "add", 7])
def test_proposed_memories_ignore_a_value_that_is_not_a_memory_command(command):
    from cortex_backend.api.routes import _proposed_memories

    assert _proposed_memories(_stub_deps(), command) == []


# --- request ids and failure logging ----------------------------------------


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_repository_failures_log_frames_and_request_id_but_not_the_message(
    app, client, headers, caplog, monkeypatch
):
    """A failure is findable from what the caller was told, and says nothing anyone typed.

    Repository errors quote the content that failed to save, so the log gets the
    exception class and the source locations it passed through -- never its text.
    """

    def list_summaries():
        try:
            raise ValueError("SECRET cause: the user's private prompt")
        except ValueError as cause:
            raise RuntimeError("SECRET: the user's private prompt") from cause

    monkeypatch.setattr(app.state.dependencies.chats, "list_summaries", list_summaries)

    with caplog.at_level(logging.ERROR):
        response = client.get("/api/v1/chats", headers=headers)

    assert response.status_code == 500
    request_id = response.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{12}", request_id)
    # The caller can quote it: it is in the body the UI shows, not only a header.
    assert response.json()["detail"] == f"Could not list chats. (Request ID: {request_id})"

    (record,) = _error_records(caplog)
    text = record.getMessage()
    assert record.name == "cortex_backend.api.routes"
    assert record.request_id == request_id
    assert f"request={request_id}" in text
    assert "RuntimeError" in text and "caused by ValueError" in text
    # Frames: where the route caught it and where the repository raised it.
    assert "cortex_backend/api/routers/chats.py" in text
    assert "in list_summaries" in text
    assert "SECRET" not in caplog.text and "private prompt" not in caplog.text
    assert record.exc_info is None


def test_every_response_carries_a_fresh_request_id_and_ignores_the_callers(client, headers):
    first = client.get("/api/v1/chats", headers=headers)
    second = client.get("/api/v1/chats", headers=headers)
    forged = client.get(
        "/api/v1/chats", headers={**headers, "X-Request-ID": "attacker-chosen-id"}
    )
    refused = client.get("/api/v1/chats")
    unknown = client.get("/api/v1/chats/does-not-exist", headers=headers)

    ids = [response.headers["X-Request-ID"] for response in (first, second, forged, refused, unknown)]
    assert refused.status_code == 401 and unknown.status_code == 404
    assert all(re.fullmatch(r"[0-9a-f]{12}", request_id) for request_id in ids)
    assert len(set(ids)) == len(ids)


def test_a_streamed_response_carries_the_request_id_too(client, headers):
    accepted = client.post(
        "/api/v1/generations",
        json={"request_id": "stream-id-1", "user_input": "hello"},
        headers=headers,
    )
    assert accepted.status_code == 202, accepted.text

    with client.stream(
        "GET", f"/api/v1/generations/{accepted.json()['job_id']}/events", headers=headers
    ) as stream:
        streamed_id = stream.headers["X-Request-ID"]
        "".join(stream.iter_text())

    assert re.fullmatch(r"[0-9a-f]{12}", streamed_id)
    assert streamed_id != accepted.headers["X-Request-ID"]


def test_a_worker_thread_failure_logs_the_job_and_the_request_that_started_it(
    app, client, headers, caplog, monkeypatch
):
    def generate(*args, **kwargs):
        raise RuntimeError("SECRET: text from the user's message")

    monkeypatch.setattr(app.state.dependencies.generation, "generate", generate)

    with caplog.at_level(logging.ERROR):
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "worker-failure-1", "user_input": "hello"},
            headers=headers,
        )
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        wait_until(
            lambda: client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()["status"] == "failed",
            describe="the generation job to fail",
        )

    (record,) = _error_records(caplog)
    text = record.getMessage()
    assert record.name == "cortex_backend.api.jobs"
    assert record.request_id == accepted.headers["X-Request-ID"]
    assert f"request={accepted.headers['X-Request-ID']}" in text
    assert f"job={job_id}" in text
    assert "RuntimeError" in text
    assert "SECRET" not in caplog.text and "user's message" not in caplog.text


def test_failure_descriptions_hold_only_class_and_source_locations():
    from cortex_backend.api.observability import describe_failure

    def inner():
        raise KeyError("SECRET-KEY")

    def outer():
        try:
            inner()
        except KeyError as cause:
            raise OSError("SECRET-PATH C:/Users/someone/private.txt") from cause

    try:
        outer()
    except OSError as exc:
        description = describe_failure(exc)

    lines = description.splitlines()
    assert lines[0] == "OSError"
    assert "caused by KeyError" in lines
    assert any(line.endswith("in inner") for line in lines)
    assert "SECRET" not in description and "someone" not in description
    # Locations are file:line in function, with no directory above the package or the file name.
    assert all(re.fullmatch(r"  [\w./-]+\.py:\d+ in [\w<>]+", line) for line in lines if line.startswith("  "))


def test_failure_descriptions_are_bounded():
    from cortex_backend.api.observability import describe_failure

    def recurse(depth: int):
        if depth == 0:
            raise RuntimeError("bottom")
        recurse(depth - 1)

    try:
        recurse(200)
    except RuntimeError as exc:
        description = describe_failure(exc)

    assert len(description.splitlines()) <= 1 + 30
    # The innermost frames are the ones kept.
    assert "in recurse" in description


def test_the_request_id_is_not_visible_outside_a_request():
    from cortex_backend.api.observability import current_request_id

    assert current_request_id() is None


def _assert_fresh_request_id(response) -> str:
    request_id = response.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{12}", request_id)
    return request_id


def test_a_refused_host_carries_the_request_id(client, headers):
    """The middleware is outermost, so a refusal it never sees the route of still has an id."""
    response = client.get("/api/v1/system", headers={**headers, "Host": "evil.example"})

    assert response.status_code == 400
    _assert_fresh_request_id(response)


@pytest.mark.parametrize("how", ["declared_length", "chunked"])
def test_an_oversized_body_refusal_carries_the_request_id(client, headers, how):
    from cortex_backend.api.app import MAX_REQUEST_BODY_BYTES

    if how == "declared_length":
        # Refused on the header alone, before any of the body is read.
        request = {"content": b"x", "headers": {"Content-Length": str(MAX_REQUEST_BODY_BYTES + 1)}}
    else:
        chunk = b"x" * (1024 * 1024)
        request = {"content": (chunk for _ in range(MAX_REQUEST_BODY_BYTES // len(chunk) + 1))}

    response = client.post(
        "/api/v1/attachments",
        headers={**headers, "Content-Type": "application/json", **request.pop("headers", {})},
        **request,
    )

    assert response.status_code == 413
    _assert_fresh_request_id(response)


def test_a_cors_preflight_carries_the_request_id(client):
    """A preflight is answered by the CORS middleware without reaching the app."""
    response = client.options(
        "/api/v1/chats",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    _assert_fresh_request_id(response)


def _add_unhandled_failure_route(app) -> None:
    def unhandled_failure():
        try:
            raise ValueError("SECRET cause: the user's private prompt")
        except ValueError as cause:
            raise RuntimeError("SECRET: the user's private prompt") from cause

    app.add_api_route("/api/v1/unhandled-failure-probe", unhandled_failure, methods=["GET"])


def test_an_unhandled_exception_is_a_500_that_carries_and_logs_the_request_id(app, caplog):
    """The exception no route dealt with used to get a plain-text 500 with no id at all."""
    _add_unhandled_failure_route(app)

    with caplog.at_level(logging.ERROR), TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/unhandled-failure-probe")

    assert response.status_code == 500
    request_id = _assert_fresh_request_id(response)
    assert response.headers["content-type"] == "application/json"
    # Generic, and quotable: nothing of the exception's text is in what the caller is told.
    assert response.json() == {"detail": f"Internal server error. (Request ID: {request_id})"}

    (record,) = _error_records(caplog)
    text = record.getMessage()
    assert record.request_id == request_id
    assert f"request={request_id}" in text
    assert "RuntimeError" in text and "caused by ValueError" in text
    assert "in unhandled_failure" in text
    assert "SECRET" not in caplog.text and "private prompt" not in caplog.text
    assert "SECRET" not in response.text


def test_an_unhandled_exception_still_reaches_the_server_and_the_test_client(app):
    _add_unhandled_failure_route(app)

    with TestClient(app) as client, pytest.raises(RuntimeError):
        client.get("/api/v1/unhandled-failure-probe")


def test_a_failure_after_the_response_started_is_logged_and_raised_with_no_second_response(caplog):
    from cortex_backend.api.observability import RequestIdMiddleware

    async def broken_stream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"part", "more_body": True})
        raise RuntimeError("SECRET: broke part way through")

    sent: list[dict] = []

    async def record_sent(message):
        sent.append(message)

    async def receive():
        return {"type": "http.disconnect"}

    scope = {"type": "http", "method": "GET", "path": "/stream", "headers": []}
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        asyncio.run(RequestIdMiddleware(broken_stream)(scope, receive, record_sent))

    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    (record,) = _error_records(caplog)
    assert record.request_id == dict(sent[0]["headers"])[b"x-request-id"].decode()
    assert "SECRET" not in caplog.text
