"""Regression coverage for atomic generation admission."""

from __future__ import annotations

import asyncio
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import json
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
import pytest

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.api.jobs import JobConflict, JobRegistry
from cortex_backend.testing.fake_ollama import FakeOllamaState
from support import session_headers as _session


THREAD_ID = "generation-admission-race"



def _events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


@dataclass(frozen=True, slots=True)
class _ScratchCall:
    request_id: str
    expression: str


class _YieldingScratchCoordinator:
    """Hold automatic compute open at the former check-then-mutate race window."""

    scratch_available = True

    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self._lock = Lock()
        self._calls: list[_ScratchCall] = []

    @property
    def calls(self) -> tuple[_ScratchCall, ...]:
        with self._lock:
            return tuple(self._calls)

    def start_scratch(self, request):
        with self._lock:
            self._calls.append(
                _ScratchCall(
                    request_id=request.request_id,
                    expression=request.expression,
                )
            )
            self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release automatic compute")
        return SimpleNamespace(job_id=request.request_id)

    @staticmethod
    def wait(job_id: str, *, timeout: float):
        del job_id, timeout
        return SimpleNamespace(status="succeeded", result={"value": "4"})

    def shutdown(self) -> None:
        self.release.set()


def _concurrent_posts(
    client: TestClient,
    *,
    headers: dict[str, str],
    coordinator: _YieldingScratchCoordinator,
    payloads: tuple[dict[str, Any], dict[str, Any]],
):
    start = Barrier(len(payloads) + 1)

    def post(payload: dict[str, Any]):
        start.wait(timeout=2)
        return client.post("/api/v1/generations", json=payload, headers=headers)

    with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
        futures = [executor.submit(post, payload) for payload in payloads]
        start.wait(timeout=2)
        try:
            assert coordinator.entered.wait(timeout=2), (
                "the winning request did not reach automatic compute"
            )
            # A correctly rejected or replayed request can finish while the
            # winner is suspended. On the vulnerable path both requests enter
            # automatic compute and remain blocked here before either mutation.
            wait(futures, timeout=0.25, return_when=FIRST_COMPLETED)
        finally:
            coordinator.release.set()
        return [future.result(timeout=5) for future in futures]


def _race_app():
    dependencies = build_demo_dependencies()
    coordinator = _YieldingScratchCoordinator()
    app = create_app(dependencies, allowed_hosts=("testserver",))
    # Automatic compute is optional application state. Injecting the narrow
    # fake after construction avoids enabling unrelated execution API routes.
    app.state.execution_coordinator = coordinator
    return app, coordinator


def _finish_and_read_chat(
    client: TestClient,
    *,
    headers: dict[str, str],
    accepted: dict[str, Any],
) -> dict[str, Any]:
    with client.stream(
        "GET",
        f"/api/v1/generations/{accepted['job_id']}/events",
        headers=headers,
    ) as response:
        events = _events("".join(response.iter_text()))
    assert response.status_code == 200
    assert events[-1]["event"] == "generation.completed"

    chat = client.get(
        f"/api/v1/chats/{accepted['thread_id']}",
        headers=headers,
    )
    assert chat.status_code == 200
    return chat.json()


def _assert_only_winner_persisted(
    chat: dict[str, Any],
    *,
    winner_input: str,
    user_message_id: str,
) -> None:
    user_messages = [
        message for message in chat["messages"] if message["role"] == "user"
    ]
    assert [message["content"] for message in user_messages] == [winner_input]
    assert user_messages[0]["id"] == user_message_id
    assert [message["role"] for message in chat["messages"]] == [
        "user",
        "assistant",
    ]
    assert chat["revision"] == 2


def test_sequential_exact_generation_replay_retains_acceptance_metadata():
    app, coordinator = _race_app()
    coordinator.release.set()
    payload = {
        "request_id": "sequential-exact-replay",
        "thread_id": THREAD_ID,
        "user_input": "calculate 2 + 2",
        "base_revision": 0,
    }

    with TestClient(app) as client:
        headers = _session(client, app)
        original = client.post(
            "/api/v1/generations",
            json=payload,
            headers=headers,
        )
        replay = client.post(
            "/api/v1/generations",
            json=payload,
            headers=headers,
        )

        assert original.status_code == replay.status_code == 202
        assert replay.json()["job_id"] == original.json()["job_id"]
        assert replay.json()["thread_id"] == original.json()["thread_id"] == THREAD_ID
        assert replay.json()["user_message_id"] == original.json()["user_message_id"]
        assert original.json()["user_message_id"] is not None
        assert len(coordinator.calls) == 1

        chat = _finish_and_read_chat(
            client,
            headers=headers,
            accepted=original.json(),
        )
        _assert_only_winner_persisted(
            chat,
            winner_input=payload["user_input"],
            user_message_id=original.json()["user_message_id"],
        )


def test_legacy_generation_retry_replays_after_model_inventory_changes():
    ollama_state = FakeOllamaState()
    app = create_app(
        build_demo_dependencies(ollama_state=ollama_state),
        allowed_hosts=("testserver",),
    )
    payload = {
        "request_id": "legacy-replay-after-model-change",
        "user_input": "hello",
    }

    with TestClient(app) as client:
        headers = _session(client, app)
        original = client.post(
            "/api/v1/jobs/generation",
            json=payload,
            headers=headers,
        )
        assert original.status_code == 202
        with client.stream(
            "GET",
            f"/api/v1/jobs/{original.json()['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))
        assert response.status_code == 200
        assert events[-1]["status"] == "succeeded"
        ollama_state.installed_models.clear()

        replay = client.post(
            "/api/v1/jobs/generation",
            json=payload,
            headers=headers,
        )

        assert replay.status_code == 202
        assert replay.json()["job_id"] == original.json()["job_id"]


def test_concurrent_exact_generation_retries_share_one_admission_and_user_turn():
    app, coordinator = _race_app()
    payload = {
        "request_id": "same-request-same-payload",
        "user_input": "calculate 2 + 2",
        "base_revision": 0,
    }

    with TestClient(app) as client:
        headers = _session(client, app)
        responses = _concurrent_posts(
            client,
            headers=headers,
            coordinator=coordinator,
            payloads=(payload, dict(payload)),
        )

        assert [response.status_code for response in responses] == [202, 202]
        accepted = [response.json() for response in responses]
        assert len({item["job_id"] for item in accepted}) == 1
        thread_ids = {item["thread_id"] for item in accepted}
        assert len(thread_ids) == 1
        assert None not in thread_ids
        assert len({item["user_message_id"] for item in accepted}) == 1
        assert accepted[0]["user_message_id"] is not None
        assert len(coordinator.calls) == 1

        chat = _finish_and_read_chat(
            client,
            headers=headers,
            accepted=accepted[0],
        )
        _assert_only_winner_persisted(
            chat,
            winner_input=payload["user_input"],
            user_message_id=accepted[0]["user_message_id"],
        )


def test_exact_regeneration_retry_replays_after_chat_advances():
    app, coordinator = _race_app()
    coordinator.release.set()
    thread_id = "regeneration-replay"

    with TestClient(app) as client:
        headers = _session(client, app)
        initial = client.post(
            "/api/v1/generations",
            json={
                "request_id": "regeneration-replay-setup",
                "thread_id": thread_id,
                "user_input": "calculate 2 + 2",
                "base_revision": 0,
            },
            headers=headers,
        )
        assert initial.status_code == 202
        chat = _finish_and_read_chat(
            client,
            headers=headers,
            accepted=initial.json(),
        )
        assistant_id = chat["messages"][-1]["id"]
        payload = {
            "request_id": "regeneration-exact-replay",
            "message_id": assistant_id,
        }

        original = client.post(
            f"/api/v1/chats/{thread_id}/regenerations",
            json=payload,
            headers=headers,
        )
        assert original.status_code == 202
        _finish_and_read_chat(
            client,
            headers=headers,
            accepted=original.json(),
        )
        for role, content in (
            ("user", "a later turn"),
            ("assistant", "a later answer"),
        ):
            advanced = client.post(
                f"/api/v1/chats/{thread_id}/messages",
                json={"role": role, "content": content},
                headers=headers,
            )
            assert advanced.status_code == 200
        calls_before_replay = len(coordinator.calls)

        replay = client.post(
            f"/api/v1/chats/{thread_id}/regenerations",
            json=payload,
            headers=headers,
        )

        assert replay.status_code == 202
        assert replay.json()["job_id"] == original.json()["job_id"]
        assert replay.json()["thread_id"] == thread_id
        assert len(coordinator.calls) == calls_before_replay


def test_concurrent_reuse_of_generation_request_id_with_new_payload_is_rejected():
    app, coordinator = _race_app()
    payloads = (
        {
            "request_id": "same-request-different-payload",
            "thread_id": THREAD_ID,
            "user_input": "calculate 2 + 2",
            "base_revision": 0,
        },
        {
            "request_id": "same-request-different-payload",
            "thread_id": THREAD_ID,
            "user_input": "calculate 3 + 3",
            "base_revision": 0,
        },
    )

    with TestClient(app) as client:
        headers = _session(client, app)
        responses = _concurrent_posts(
            client,
            headers=headers,
            coordinator=coordinator,
            payloads=payloads,
        )

        assert sorted(response.status_code for response in responses) == [202, 409]
        winner_index = next(
            index
            for index, response in enumerate(responses)
            if response.status_code == 202
        )
        accepted = responses[winner_index].json()
        assert accepted["user_message_id"] is not None
        assert len(coordinator.calls) == 1

        chat = _finish_and_read_chat(
            client,
            headers=headers,
            accepted=accepted,
        )
        _assert_only_winner_persisted(
            chat,
            winner_input=payloads[winner_index]["user_input"],
            user_message_id=accepted["user_message_id"],
        )


def test_aborted_reservation_releases_active_kind_and_request_id():
    registry = JobRegistry()
    reservation = registry.reserve(
        kind="generation",
        owner="owner",
        thread_id=THREAD_ID,
        request_id="retry-after-abort",
        request_fingerprint="payload-a",
    )
    assert reservation.created is True

    aborted = registry.abort_reservation(
        reservation,
        owner="owner",
    )

    assert aborted.status == "failed"
    assert registry.active_snapshot(kind="generation") is None
    assert (
        registry.request_snapshot(
            kind="generation",
            owner="owner",
            request_id="retry-after-abort",
        )
        is None
    )
    retry = registry.reserve(
        kind="generation",
        owner="owner",
        thread_id=THREAD_ID,
        request_id="retry-after-abort",
        request_fingerprint="payload-a",
    )
    assert retry.created is True
    assert retry.snapshot.job_id != reservation.snapshot.job_id

    registry.abort_reservation(retry, owner="owner")


def test_pruning_aborted_reservation_preserves_newer_retry_index():
    registry = JobRegistry(max_terminal_jobs=1)
    original = registry.reserve(
        kind="generation",
        owner="owner",
        thread_id=THREAD_ID,
        request_id="retry-survives-pruning",
        request_fingerprint="payload-a",
    )
    registry.abort_reservation(original, owner="owner")
    retry = registry.reserve(
        kind="generation",
        owner="owner",
        thread_id=THREAD_ID,
        request_id="retry-survives-pruning",
        request_fingerprint="payload-a",
    )
    terminal_model = registry.reserve(
        kind="models",
        owner="owner",
        thread_id=None,
    )
    registry.abort_reservation(terminal_model, owner="owner")

    pruning_trigger = registry.reserve(
        kind="models",
        owner="owner",
        thread_id=None,
    )

    indexed = registry.request_snapshot(
        kind="generation",
        owner="owner",
        request_id="retry-survives-pruning",
    )
    assert indexed is not None
    assert indexed.job_id == retry.snapshot.job_id

    registry.abort_reservation(retry, owner="owner")
    registry.abort_reservation(pruning_trigger, owner="owner")


def test_shutdown_closes_admission_and_prevents_starting_reserved_work():
    async def scenario() -> None:
        registry = JobRegistry()
        reservation = registry.reserve(
            kind="generation",
            owner="owner",
            thread_id=THREAD_ID,
            request_id="reserved-at-shutdown",
            request_fingerprint="payload-a",
        )

        await registry.shutdown()

        with pytest.raises(JobConflict):
            registry.start_reserved(
                reservation,
                owner="owner",
                runner=lambda sink, cancel_event: {
                    "unexpected": not cancel_event.is_set()
                },
            )
        with pytest.raises(JobConflict):
            registry.reserve(
                kind="generation",
                owner="owner",
                thread_id=THREAD_ID,
                request_id="after-shutdown",
                request_fingerprint="payload-b",
            )

    asyncio.run(scenario())


def test_generation_admission_reports_shutdown_as_service_unavailable():
    app, _ = _race_app()

    with TestClient(app) as client:
        headers = _session(client, app)
        asyncio.run(app.state.jobs.shutdown())

        response = client.post(
            "/api/v1/generations",
            json={
                "request_id": "after-shutdown",
                "user_input": "calculate 2 + 2",
            },
            headers=headers,
        )

        assert response.status_code == 503
        assert response.json() == {"detail": "Cortex is shutting down."}


def test_concurrent_distinct_generation_requests_admit_only_one_active_job():
    app, coordinator = _race_app()
    payloads = (
        {
            "request_id": "different-request-a",
            "thread_id": THREAD_ID,
            "user_input": "calculate 2 + 2",
            "base_revision": 0,
        },
        {
            "request_id": "different-request-b",
            "thread_id": THREAD_ID,
            "user_input": "calculate 5 + 5",
            "base_revision": 0,
        },
    )

    with TestClient(app) as client:
        headers = _session(client, app)
        responses = _concurrent_posts(
            client,
            headers=headers,
            coordinator=coordinator,
            payloads=payloads,
        )

        assert sorted(response.status_code for response in responses) == [202, 409]
        winner_index = next(
            index
            for index, response in enumerate(responses)
            if response.status_code == 202
        )
        accepted = responses[winner_index].json()
        assert accepted["user_message_id"] is not None
        assert len(coordinator.calls) == 1

        chat = _finish_and_read_chat(
            client,
            headers=headers,
            accepted=accepted,
        )
        _assert_only_winner_persisted(
            chat,
            winner_input=payloads[winner_index]["user_input"],
            user_message_id=accepted["user_message_id"],
        )
