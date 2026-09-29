"""Compare-and-append chat revision boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
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
from support import parse_sse_events as _events
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




def test_generation_route_rejects_a_stale_base_revision():
    dependencies = build_demo_dependencies()
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        created = client.post(
            "/api/v1/chats", json={"title": "Thread"}, headers=headers
        ).json()
        thread_id = created["id"]
        dependencies.chats.add_message(thread_id, "user", "first")
        chat_before = dependencies.chats.get_chat(thread_id)

        stale = client.post(
            "/api/v1/generations",
            json={"thread_id": thread_id, "user_input": "stale", "base_revision": 0},
            headers=headers,
        )

        assert stale.status_code == 409
        assert "chat changed" in stale.json()["detail"].lower()
        assert dependencies.chats.get_chat(thread_id) == chat_before


def test_generation_does_not_append_an_assistant_after_a_concurrent_chat_mutation():
    state = FakeOllamaState(generation_delay_seconds=0.2)
    dependencies = build_demo_dependencies(ollama_state=state)
    app = create_app(dependencies, allowed_hosts=("testserver",))
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

        # Another writer lands a turn while the generation is running.
        dependencies.chats.add_message("cas-thread", "user", "concurrent")

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

    Mimics an independent write to the chat that lands between
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


class _ChangeAfterRead:
    """Change the chat right after the regenerate route has read it.

    The route reads the transcript to validate its target, and admission then
    re-checks only the revision inside ``prepare()``. Anything that lands in
    between is exactly what that re-check exists for, so this fires once, on
    the first read of the chat after it is armed, after the read has returned.
    """

    def __init__(self, inner, *, thread_id: str, change):
        self.inner = inner
        self._thread_id = thread_id
        self._change = change
        self.armed = False
        self.fired = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def get_chat(self, thread_id):
        chat = self.inner.get_chat(thread_id)
        if self.armed and not self.fired and thread_id == self._thread_id:
            self.fired = True
            self._change(self.inner, thread_id)
        return chat


def _regeneration_that_meets_a_change(change):
    """Run a regeneration of the last reply while ``change`` lands mid-flight."""
    thread_id = "regenerate-race"
    dependencies = build_demo_dependencies()
    racing = _ChangeAfterRead(dependencies.chats, thread_id=thread_id, change=change)
    dependencies.chats = racing
    racing.inner.create_chat(thread_id, "Regenerate race")
    racing.inner.add_message(thread_id, "user", "the question")
    reply_id = racing.inner.add_message(thread_id, "assistant", "the original reply")
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        racing.armed = True
        response = client.post(
            f"/api/v1/chats/{thread_id}/regenerations",
            json={"request_id": "regenerate-race-1", "message_id": reply_id},
            headers=headers,
        )
        racing.armed = False
        # The failed attempt must not have left the generation slot taken.
        follow_up = client.post(
            "/api/v1/generations",
            json={"thread_id": "another-thread", "user_input": "still works"},
            headers=headers,
        )
    assert racing.fired, "the change never landed between the route's read and prepare()"
    return response, follow_up, racing.inner, thread_id


def test_regeneration_refuses_a_chat_that_grew_between_the_read_and_prepare():
    """The route validated its target against a transcript that is no longer current.

    A message appended after the route's read and before ``prepare()`` must make
    the admission re-check answer 409, and nothing may be regenerated over it.
    """

    def append_a_message(inner, thread_id):
        inner.add_message(thread_id, "user", "a message that arrived in between")

    response, follow_up, inner, thread_id = _regeneration_that_meets_a_change(append_a_message)

    assert response.status_code == 409, response.text
    assert "chat changed" in response.json()["detail"].lower()
    messages = inner.get_chat(thread_id)["messages"]
    assert [message["content"] for message in messages] == [
        "the question",
        "the original reply",
        "a message that arrived in between",
    ]
    assert follow_up.status_code == 202


def test_regeneration_of_a_chat_deleted_between_the_read_and_prepare_is_a_404():
    """A chat that is gone is not a conflict, and must not be recreated to hold a reply."""

    def delete_the_chat(inner, thread_id):
        inner.delete_chat(thread_id)

    response, follow_up, inner, thread_id = _regeneration_that_meets_a_change(delete_the_chat)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Chat not found."
    assert inner.get_chat(thread_id) is None
    assert follow_up.status_code == 202
