"""Compare-and-append chat revision boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.repositories.chats import (
    ChatRevisionConflict,
    InMemoryChatRepository,
    LegacyDatabaseChatRepository,
)
from cortex_backend.repositories.storage import DatabaseManager
from cortex_backend.testing.fake_ollama import FakeOllamaState
from support import session_headers as _session


def test_inmemory_append_is_compare_and_swap_and_atomic():
    repository = InMemoryChatRepository()
    repository.create_chat("thread", "Thread")
    start = Barrier(3)

    def append(content: str):
        start.wait(timeout=2)
        try:
            return repository.add_message(
                "thread",
                "user",
                content,
                expected_revision=0,
            )
        except ChatRevisionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(append, content) for content in ("one", "two")]
        start.wait(timeout=2)
        results = [future.result(timeout=2) for future in futures]

    assert sum(result is not None for result in results) == 1
    assert len(repository.get_chat("thread")["messages"]) == 1


def test_sqlite_chat_revision_conflict_is_atomic(tmp_path: Path):
    database = DatabaseManager(db_path=str(tmp_path / "chats.sqlite"))
    repository = LegacyDatabaseChatRepository(database)
    repository.create_chat("thread", "Thread")
    repository.add_message("thread", "user", "one", expected_revision=0)

    with pytest.raises(ChatRevisionConflict):
        repository.add_message("thread", "user", "stale", expected_revision=0)

    chat = repository.get_chat("thread")
    assert [message["content"] for message in chat["messages"]] == ["one"]



def _events(body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_message_route_rejects_a_stale_base_revision():
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        created = client.post(
            "/api/v1/chats", json={"title": "Thread"}, headers=headers
        ).json()
        thread_id = created["id"]
        assert client.post(
            f"/api/v1/chats/{thread_id}/messages",
            json={"role": "user", "content": "first"},
            headers=headers,
        ).status_code == 200

        stale = client.post(
            f"/api/v1/chats/{thread_id}/messages",
            json={"role": "user", "content": "stale", "base_revision": 0},
            headers=headers,
        )

        assert stale.status_code == 409
        assert "revision changed" in stale.json()["detail"].lower()


def test_generation_does_not_append_an_assistant_after_a_concurrent_chat_mutation():
    state = FakeOllamaState(generation_delay_seconds=0.2)
    app = create_app(
        build_demo_dependencies(ollama_state=state), allowed_hosts=("testserver",)
    )
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={
                "request_id": "cas-generation",
                "thread_id": "cas-thread",
                "user_input": "first",
                "base_revision": 0,
            },
            headers=headers,
        )
        assert accepted.status_code == 202
        accepted_payload = accepted.json()

        for _ in range(100):
            status = client.get(
                f"/api/v1/generations/{accepted_payload['job_id']}", headers=headers
            ).json()
            if status["status"] == "running":
                break
        else:
            raise AssertionError("generation did not begin running")

        concurrent = client.post(
            "/api/v1/chats/cas-thread/messages",
            json={"role": "user", "content": "concurrent"},
            headers=headers,
        )
        assert concurrent.status_code == 200

        with client.stream(
            "GET",
            f"/api/v1/generations/{accepted_payload['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))

        assert events[-1]["event"] == "generation.failed"
        chat = client.get("/api/v1/chats/cas-thread", headers=headers).json()
        assert [message["role"] for message in chat["messages"]] == ["user", "user"]


class _RaceInjectingChatRepository:
    """Land a genuinely concurrent chat write mid ``add_message``.

    Mimics an independent ``/chats/{id}/messages`` request that lands between
    the coarse admission-revision check in ``prepare()`` and the actual
    compare-and-append it performs, so the real ``add_message`` call's own
    CAS observes a stale ``expected_revision`` and raises
    ``ChatRevisionConflict`` -- even though the earlier check inside
    ``prepare()`` already passed.
    """

    def __init__(self, inner, *, thread_id: str):
        self._inner = inner
        self._thread_id = thread_id
        self._injected = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def add_message(self, thread_id, role, content, **kwargs):
        if not self._injected and thread_id == self._thread_id and role == "user":
            self._injected = True
            self._inner.add_message(
                thread_id,
                "user",
                "a genuinely concurrent message",
                thread_title="New Chat",
            )
        return self._inner.add_message(thread_id, role, content, **kwargs)


def test_generation_route_maps_a_concurrent_chat_write_race_to_409():
    thread_id = "revision-race-thread"
    dependencies = build_demo_dependencies()
    dependencies.chats = _RaceInjectingChatRepository(
        dependencies.chats, thread_id=thread_id
    )
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        response = client.post(
            "/api/v1/generations",
            json={
                "request_id": "revision-race-generation",
                "thread_id": thread_id,
                "user_input": "hello",
            },
            headers=headers,
        )

    assert response.status_code == 409
    assert "revision changed" in response.json()["detail"].lower()
