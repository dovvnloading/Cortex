"""Chat groups (folders/projects): persistence, migration, and the HTTP surface.

The load-bearing guarantee throughout is that a group is only ever *filing*:
deleting one, or losing one to a corrupted link, must never take conversations
with it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.repositories.chats import (
    ChatGroupNotFound,
    InMemoryChatRepository,
    LegacyDatabaseChatRepository,
)
from cortex_backend.repositories.storage import DatabaseManager
from support import session_headers as _session



def _repositories(tmp_path: Path):
    """Both implementations, so the in-memory double used by API tests cannot
    silently drift from the SQLite one users actually run."""
    database = DatabaseManager(db_path=str(tmp_path / "chats.sqlite"))
    return [InMemoryChatRepository(), LegacyDatabaseChatRepository(database)]


# -- repository parity -----------------------------------------------------


def test_group_lifecycle_matches_across_repositories(tmp_path: Path) -> None:
    for repository in _repositories(tmp_path):
        repository.create_chat("t1", "Alpha")
        repository.create_chat("t2", "Beta")
        repository.create_group("g1", "Research")
        repository.create_group("g2", "Work")

        groups = repository.list_groups()
        assert [group["name"] for group in groups] == ["Research", "Work"]
        assert [group["collapsed"] for group in groups] == [False, False]

        assert repository.set_chat_group("t1", "g1") is True
        summaries = {item["id"]: item["group_id"] for item in repository.list_summaries()}
        assert summaries == {"t1": "g1", "t2": None}

        # Ungrouping is an explicit null, not a missing field.
        assert repository.set_chat_group("t1", None) is True
        assert all(item["group_id"] is None for item in repository.list_summaries())


def test_rename_and_collapse_share_one_update_path(tmp_path: Path) -> None:
    for repository in _repositories(tmp_path):
        repository.create_group("g1", "Research")

        assert repository.update_group("g1", name="Deep Research") is True
        assert repository.list_groups()[0]["name"] == "Deep Research"
        assert repository.list_groups()[0]["collapsed"] is False

        assert repository.update_group("g1", collapsed=True) is True
        group = repository.list_groups()[0]
        assert group["collapsed"] is True
        assert group["name"] == "Deep Research"  # untouched by a collapse

        assert repository.update_group("missing", name="x") is False


def test_deleting_a_group_keeps_its_chats(tmp_path: Path) -> None:
    """The whole point: a group is filing, not a container that owns chats."""
    for repository in _repositories(tmp_path):
        repository.create_chat("t1", "Alpha")
        repository.create_group("g1", "Research")
        repository.set_chat_group("t1", "g1")

        repository.delete_group("g1")

        summaries = repository.list_summaries()
        assert [item["id"] for item in summaries] == ["t1"]
        assert summaries[0]["group_id"] is None
        assert repository.list_groups() == []
        assert repository.get_chat("t1") is not None


def test_moving_a_chat_into_an_unknown_group_is_rejected(tmp_path: Path) -> None:
    for repository in _repositories(tmp_path):
        repository.create_chat("t1", "Alpha")
        with pytest.raises(ChatGroupNotFound):
            repository.set_chat_group("t1", "ghost")
        assert repository.list_summaries()[0]["group_id"] is None


def test_moving_an_unknown_chat_reports_miss_without_raising(tmp_path: Path) -> None:
    for repository in _repositories(tmp_path):
        repository.create_group("g1", "Research")
        assert repository.set_chat_group("missing-thread", "g1") is False


# -- schema migration ------------------------------------------------------


def test_a_v3_database_upgrades_in_place_without_losing_chats(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL, timestamp TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL, "
        "role TEXT NOT NULL, content TEXT NOT NULL, sources TEXT, thoughts TEXT, attachments TEXT, "
        "timestamp TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO threads VALUES ('old-1', 'Existing chat', '2026-01-01T00:00:00Z')"
    )
    connection.execute(
        "INSERT INTO messages (thread_id, role, content, timestamp) "
        "VALUES ('old-1', 'user', 'hello', '2026-01-01T00:00:00Z')"
    )
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()

    database = DatabaseManager(db_path=str(path))

    summaries = database.get_all_chats_summary()
    assert [item["id"] for item in summaries] == ["old-1"]
    assert summaries[0]["group_id"] is None
    assert len(database.load_chat("old-1")["messages"]) == 1
    assert database.list_groups() == []

    probe = sqlite3.connect(path)
    try:
        assert probe.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        probe.close()


def test_a_chat_pointing_at_a_vanished_group_is_returned_to_ungrouped(tmp_path: Path) -> None:
    """There is no FOREIGN KEY on threads.group_id (SQLite cannot add one via
    ALTER TABLE), so a chat could outlive its group after an interrupted
    delete. Such a chat would be filed under a group the sidebar never renders
    and would look deleted -- startup must repair it."""
    path = tmp_path / "chats.sqlite"
    database = DatabaseManager(db_path=str(path))
    database.create_chat("t1", "Alpha")
    database.create_group("g1", "Research")
    database.set_chat_group("t1", "g1")

    corrupt = sqlite3.connect(path)
    try:
        corrupt.execute("DELETE FROM chat_groups WHERE id = 'g1'")
        corrupt.commit()
    finally:
        corrupt.close()

    repaired = DatabaseManager(db_path=str(path))

    assert repaired.get_all_chats_summary()[0]["group_id"] is None
    assert len(repaired.load_chat("t1")["messages"]) == 0
    assert repaired.get_all_chats_summary()[0]["id"] == "t1"


# -- HTTP surface ----------------------------------------------------------


def test_group_routes_round_trip_over_http() -> None:
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)

        assert client.get("/api/v1/chat-groups", headers=headers).json() == []

        created = client.post(
            "/api/v1/chat-groups", json={"name": "Research"}, headers=headers
        )
        assert created.status_code == 201
        group = created.json()
        assert group["name"] == "Research"
        assert group["collapsed"] is False
        assert group["position"] == 0

        chat = client.post(
            "/api/v1/chats", json={"title": "Alpha"}, headers=headers
        ).json()

        moved = client.patch(
            f"/api/v1/chats/{chat['id']}/group",
            json={"group_id": group["id"]},
            headers=headers,
        )
        assert moved.status_code == 200
        assert moved.json()["group_id"] == group["id"]

        collapsed = client.patch(
            f"/api/v1/chat-groups/{group['id']}",
            json={"collapsed": True},
            headers=headers,
        )
        assert collapsed.status_code == 200
        assert collapsed.json()["collapsed"] is True
        assert collapsed.json()["name"] == "Research"

        # Deleting the group must leave the chat, now ungrouped.
        assert client.delete(
            f"/api/v1/chat-groups/{group['id']}", headers=headers
        ).status_code == 204
        assert client.get("/api/v1/chat-groups", headers=headers).json() == []
        summaries = client.get("/api/v1/chats", headers=headers).json()
        assert [item["id"] for item in summaries] == [chat["id"]]
        assert summaries[0]["group_id"] is None


def test_group_routes_report_missing_targets_as_404() -> None:
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        chat = client.post(
            "/api/v1/chats", json={"title": "Alpha"}, headers=headers
        ).json()

        assert client.patch(
            "/api/v1/chat-groups/ghost", json={"name": "x"}, headers=headers
        ).status_code == 404
        assert client.patch(
            f"/api/v1/chats/{chat['id']}/group",
            json={"group_id": "ghost"},
            headers=headers,
        ).status_code == 404

        group = client.post(
            "/api/v1/chat-groups", json={"name": "Research"}, headers=headers
        ).json()
        assert client.patch(
            "/api/v1/chats/ghost-thread/group",
            json={"group_id": group["id"]},
            headers=headers,
        ).status_code == 404


def test_group_routes_require_a_session() -> None:
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        assert client.get("/api/v1/chat-groups").status_code == 401
        assert client.post("/api/v1/chat-groups", json={"name": "x"}).status_code == 401


def test_group_names_are_bounded_and_non_empty() -> None:
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        assert client.post(
            "/api/v1/chat-groups", json={"name": "   "}, headers=headers
        ).status_code == 422
        assert client.post(
            "/api/v1/chat-groups", json={"name": ""}, headers=headers
        ).status_code == 422
        assert client.post(
            "/api/v1/chat-groups", json={"name": "x" * 121}, headers=headers
        ).status_code == 422


def test_chat_and_message_text_is_trimmed_and_rejects_invisible_input() -> None:
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        chat = client.post(
            "/api/v1/chats", json={"title": "  Project  "}, headers=headers
        )
        assert chat.status_code == 201
        chat_payload = chat.json()
        assert chat_payload["title"] == "Project"

        thread_id = chat_payload["id"]
        message = client.post(
            f"/api/v1/chats/{thread_id}/messages",
            json={"role": "user", "content": " hello "},
            headers=headers,
        )
        assert message.status_code == 200
        assert message.json()["messages"][-1]["content"] == "hello"
        assert client.patch(
            f"/api/v1/chats/{thread_id}",
            json={"title": "\t\n"},
            headers=headers,
        ).status_code == 422
        assert client.post(
            "/api/v1/chat-groups", json={"name": "\u200b"}, headers=headers
        ).status_code == 422
        assert client.post(
            f"/api/v1/chats/{thread_id}/messages",
            json={"role": "user", "content": " \n\t "},
            headers=headers,
        ).status_code == 422
