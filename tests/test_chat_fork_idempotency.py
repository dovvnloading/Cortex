"""A committed chat fork must replay safely after its HTTP response is lost."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortex_backend.api import create_app
from cortex_backend.repositories.chats import LegacyDatabaseChatRepository
from cortex_backend.repositories.storage import DatabaseManager
from cortex_backend.testing import build_demo_dependencies
from support import session_headers


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_fork_request_replay_returns_one_committed_copy_after_post_commit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str,
) -> None:
    """A 500 after the copy commits is not permission to copy the chat again."""
    dependencies = build_demo_dependencies()
    if backend == "sqlite":
        manager = DatabaseManager(db_path=str(tmp_path / "chat.sqlite"))
        dependencies.chats = LegacyDatabaseChatRepository(manager)

    chats = dependencies.chats
    chats.create_chat("source", "Important conversation")
    first_message = chats.add_message("source", "user", "Keep this lengthy history")
    chats.add_message("source", "assistant", "An important persisted answer")
    assert chats.get_chat("source") is not None

    original_get_chat = chats.get_chat
    fault = {"raised": False}

    def fail_only_first_post_commit_read(thread_id: str):
        if thread_id != "source" and not fault["raised"]:
            fault["raised"] = True
            raise RuntimeError("injected post-commit reply read failure")
        return original_get_chat(thread_id)

    app = create_app(dependencies, allowed_hosts=("testserver",))
    request = {"message_id": first_message, "request_id": "same-client-fork-request"}
    url = "/api/v1/chats/source/forks"
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = session_headers(client, app)
        monkeypatch.setattr(chats, "get_chat", fail_only_first_post_commit_read)

        first = client.post(url, json=request, headers=headers)
        assert first.status_code == 500
        assert fault["raised"] is True

        after_error = chats.list_summaries()
        assert len(after_error) == 2
        committed_fork_id = next(chat["id"] for chat in after_error if chat["id"] != "source")
        committed = original_get_chat(committed_fork_id)
        assert committed is not None
        assert [msg["content"] for msg in committed["messages"]] == [
            "Keep this lengthy history"
        ]

        # The exact same POST replays the already-committed fork, including its
        # stable messages and ID, and does not perform a second copy.
        replay = client.post(url, json=request, headers=headers)
        assert replay.status_code == 201
        assert replay.json()["id"] == committed_fork_id
        assert [msg["content"] for msg in replay.json()["messages"]] == [
            msg["content"] for msg in committed["messages"]
        ]
        assert [msg["id"] for msg in replay.json()["messages"]] == [
            str(msg["id"]) for msg in committed["messages"]
        ]
        assert len(chats.list_summaries()) == 2

        again = client.post(url, json=request, headers=headers)
        assert again.status_code == 201
        assert again.json()["id"] == committed_fork_id
        assert len(chats.list_summaries()) == 2

        # A genuinely new explicit user action may still create another fork
        # with a new request identity.
        second = client.post(
            url,
            json={**request, "request_id": "a-second-explicit-fork"},
            headers=headers,
        )
        assert second.status_code == 201
        assert second.json()["id"] != committed_fork_id
        assert len(chats.list_summaries()) == 3


def test_fork_request_without_a_retry_key_cannot_copy_data(tmp_path: Path) -> None:
    dependencies = build_demo_dependencies()
    chats = dependencies.chats
    chats.create_chat("source", "Source")
    first_message = chats.add_message("source", "user", "Question")
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = session_headers(client, app)
        response = client.post(
            "/api/v1/chats/source/forks",
            json={"message_id": first_message},
            headers=headers,
        )
        assert response.status_code == 422
        assert len(chats.list_summaries()) == 1
