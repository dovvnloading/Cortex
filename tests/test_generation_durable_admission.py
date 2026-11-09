"""Crash-safe admission replay when a shutdown overlaps the committed user turn."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from cortex_backend.api import create_app
from cortex_backend.repositories.chats import (
    InMemoryChatRepository,
    LegacyDatabaseChatRepository,
)
from cortex_backend.repositories.storage import DatabaseManager, PersistenceError
from cortex_backend.testing import build_demo_dependencies
from support import parse_sse_events, session_headers


OWNER = "a" * 64


def _app_with_chats(chats):
    deps = build_demo_dependencies()
    deps.chats = chats
    return create_app(
        deps, allowed_hosts=("testserver",), installation_principal_id=OWNER,
    )


def _chat_repository(kind: str, path: Path):
    if kind == "sqlite":
        return LegacyDatabaseChatRepository(DatabaseManager(db_path=str(path)))
    return InMemoryChatRepository()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_replay_after_shutdown_between_user_commit_and_job_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    """The same request must recover the first user turn, never create another."""
    path = tmp_path / "history.sqlite"
    chats = _chat_repository(kind, path)
    payload = {
        "request_id": "admission-interrupted-after-commit",
        "thread_id": None,
        "user_input": "Answer this once.",
        "base_revision": 0,
    }
    committed, release = Event(), Event()
    original_append = chats.append_generation_turn

    def paused_append(*args, **kwargs):
        message_id = original_append(*args, **kwargs)
        committed.set()  # The message AND mapping are durable at this instant.
        assert release.wait(timeout=10), "test did not release the admission"
        return message_id

    monkeypatch.setattr(chats, "append_generation_turn", paused_append)
    app = _app_with_chats(chats)
    try:
        with TestClient(app) as client:
            headers = session_headers(client, app)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    client.post, "/api/v1/generations", json=payload, headers=headers,
                )
                try:
                    assert committed.wait(timeout=5), "the user turn was not committed"
                    record = chats.get_generation_admission(OWNER, payload["request_id"])
                    assert record is not None
                    assert record.user_message_id
                    assert len(chats.get_chat(record.thread_id)["messages"]) == 1
                    app.state.jobs.begin_shutdown()
                finally:
                    release.set()
                original = future.result(timeout=10)
                assert original.status_code == 503
    finally:
        release.set()
        monkeypatch.setattr(chats, "append_generation_turn", original_append)

    # A new registry has no knowledge of the old job. SQLite is reopened,
    # not merely re-read through the original connection or repository object.
    restarted = _chat_repository(kind, path) if kind == "sqlite" else chats
    restored = restarted.get_generation_admission(OWNER, payload["request_id"])
    assert restored is not None
    app_after_restart = _app_with_chats(restarted)
    with TestClient(app_after_restart) as client:
        headers = session_headers(client, app_after_restart)
        replay = client.post("/api/v1/generations", json=payload, headers=headers)
        assert replay.status_code == 202, replay.text
        assert replay.json()["thread_id"] == restored.thread_id
        assert replay.json()["user_message_id"] == restored.user_message_id

        with client.stream(
            "GET", f"/api/v1/generations/{replay.json()['job_id']}/events",
            headers=headers,
        ) as response:
            events = parse_sse_events("".join(response.iter_text()))
        assert response.status_code == 200
        assert events[-1]["event"] == "generation.completed"
        messages = client.get(
            f"/api/v1/chats/{restored.thread_id}", headers=headers,
        ).json()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["id"] == restored.user_message_id
        assert messages[0]["content"] == payload["user_input"]


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_replay_after_answer_saved_returns_original_answer_without_generating_again(
    tmp_path: Path, kind: str,
) -> None:
    path = tmp_path / "history.sqlite"
    chats = _chat_repository(kind, path)
    payload = {
        "request_id": "admission-already-answered",
        "user_input": "Respond once.",
    }
    app = _app_with_chats(chats)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        first = client.post("/api/v1/generations", json=payload, headers=headers)
        assert first.status_code == 202
        with client.stream(
            "GET", f"/api/v1/generations/{first.json()['job_id']}/events",
            headers=headers,
        ) as response:
            events = parse_sse_events("".join(response.iter_text()))
        assert events[-1]["event"] == "generation.completed"
        thread_id = first.json()["thread_id"]
        original_chat = client.get(
            f"/api/v1/chats/{thread_id}", headers=headers,
        ).json()

    restarted = _chat_repository(kind, path) if kind == "sqlite" else chats
    new_app = _app_with_chats(restarted)
    with TestClient(new_app) as client:
        headers = session_headers(client, new_app)
        replay = client.post("/api/v1/generations", json=payload, headers=headers)
        assert replay.status_code == 202
        assert replay.json()["thread_id"] == thread_id
        assert replay.json()["user_message_id"] == first.json()["user_message_id"]

        with client.stream(
            "GET", f"/api/v1/generations/{replay.json()['job_id']}/events",
            headers=headers,
        ) as response:
            events = parse_sse_events("".join(response.iter_text()))
        assert events[-1]["event"] == "generation.completed"
        assert events[-1]["data"]["recovered"] is True
        assert client.get(f"/api/v1/chats/{thread_id}", headers=headers).json() == original_chat

        changed = client.post(
            "/api/v1/generations",
            json={**payload, "user_input": "A different prompt"},
            headers=headers,
        )
        assert changed.status_code == 409
        assert client.get(f"/api/v1/chats/{thread_id}", headers=headers).json() == original_chat


def test_existing_terminal_replay_does_not_prune_itself_during_reservation() -> None:
    """A replay of a retained record must not force the pruning that erases it."""
    from cortex_backend.api.jobs import JobRegistry

    chats = InMemoryChatRepository()
    app = _app_with_chats(chats)
    app.state.jobs = JobRegistry(max_terminal_jobs=1)
    payloads = [
        {"request_id": "terminal-before-prune", "user_input": "Original answer"},
        {"request_id": "different-terminal", "user_input": "Other answer"},
    ]
    original = None
    with TestClient(app) as client:
        headers = session_headers(client, app)
        for payload in payloads:
            started = client.post("/api/v1/generations", json=payload, headers=headers)
            assert started.status_code == 202
            if original is None:
                original = started.json()
            with client.stream(
                "GET", f"/api/v1/generations/{started.json()['job_id']}/events",
                headers=headers,
            ) as response:
                events = parse_sse_events("".join(response.iter_text()))
            assert events[-1]["event"] == "generation.completed"

        assert original is not None
        # Only the THIRD reserve triggers pruning at a one-terminal limit;
        # a plain lookup still sees the first completed job before replay.
        assert app.state.jobs.request_snapshot(
            kind="generation", owner=OWNER, request_id=payloads[0]["request_id"],
        ) is not None
        before = client.get(
            f"/api/v1/chats/{original['thread_id']}", headers=headers,
        ).json()

        replay = client.post("/api/v1/generations", json=payloads[0], headers=headers)
        assert replay.status_code == 202
        assert replay.json()["job_id"] == original["job_id"]
        assert replay.json()["thread_id"] == original["thread_id"]
        assert replay.json()["user_message_id"] == original["user_message_id"]
        assert client.get(
            f"/api/v1/chats/{original['thread_id']}", headers=headers,
        ).json() == before


def test_pruning_a_completed_job_does_not_expire_the_durable_replay_key() -> None:
    """A bounded in-memory job log must not turn a replay into a second chat."""
    from cortex_backend.api.jobs import JobRegistry

    chats = InMemoryChatRepository()
    app = _app_with_chats(chats)
    app.state.jobs = JobRegistry(max_terminal_jobs=1)
    original = None
    payloads = [
        {"request_id": "original-pruned", "user_input": "Answer the original only."},
        {"request_id": "other-one", "user_input": "Second chat"},
        {"request_id": "other-two", "user_input": "Third chat"},
    ]
    with TestClient(app) as client:
        headers = session_headers(client, app)
        for payload in payloads:
            accepted = client.post("/api/v1/generations", json=payload, headers=headers)
            assert accepted.status_code == 202
            if original is None:
                original = accepted.json()
            with client.stream(
                "GET", f"/api/v1/generations/{accepted.json()['job_id']}/events",
                headers=headers,
            ) as response:
                events = parse_sse_events("".join(response.iter_text()))
            assert events[-1]["event"] == "generation.completed"

        assert original is not None
        assert app.state.jobs.request_snapshot(
            kind="generation", owner=OWNER, request_id=payloads[0]["request_id"],
        ) is None, "fixture must evict the original in-memory registry record"

        saved = client.get(
            f"/api/v1/chats/{original['thread_id']}", headers=headers,
        ).json()
        replay = client.post("/api/v1/generations", json=payloads[0], headers=headers)
        assert replay.status_code == 202
        assert replay.json()["thread_id"] == original["thread_id"]
        assert replay.json()["user_message_id"] == original["user_message_id"]
        with client.stream(
            "GET", f"/api/v1/generations/{replay.json()['job_id']}/events",
            headers=headers,
        ) as response:
            events = parse_sse_events("".join(response.iter_text()))
        assert events[-1]["event"] == "generation.completed"
        assert events[-1]["data"]["recovered"] is True
        assert client.get(
            f"/api/v1/chats/{original['thread_id']}", headers=headers,
        ).json() == saved


def test_sqlite_duplicate_admission_key_rolls_back_the_entire_message_write(
    tmp_path: Path,
) -> None:
    db = DatabaseManager(db_path=str(tmp_path / "history.sqlite"))
    original_id = db.add_message(
        "thread", "user", "First", thread_title="New Chat",
        expected_revision=0,
        generation_admission=(OWNER, "unique-request", "fingerprint"),
    )
    assert db.get_generation_admission(OWNER, "unique-request") == {
        "owner": OWNER,
        "request_id": "unique-request",
        "fingerprint": "fingerprint",
        "thread_id": "thread",
        "user_message_id": original_id,
    }
    with pytest.raises(PersistenceError):
        db.add_message(
            "thread", "user", "A duplicate", expected_revision=1,
            generation_admission=(OWNER, "unique-request", "fingerprint"),
        )
    assert [m["content"] for m in db.load_chat("thread")["messages"]] == ["First"]
    assert db.load_chat_overview("thread")["revision"] == 1
