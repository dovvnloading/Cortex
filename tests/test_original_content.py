"""A translated answer keeps the untranslated one beside it.

With translation on, ``content`` is what the user reads and ``original_content``
is the answer as the model wrote it. The model is shown the original as its own
earlier turn and a title is made from it, so the conversation it continues stays
in one language; the user still sees only the translation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from cortex_backend.api import create_app
from cortex_backend.core.settings import CortexSettings, TranslationSettings
from cortex_backend.repositories.chats import InMemoryChatRepository, LegacyDatabaseChatRepository
from cortex_backend.repositories.settings import InMemorySettingsRepository
from cortex_backend.repositories.storage import DatabaseManager
from cortex_backend.services.generation import GenerationService
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.testing.fake_ollama import FakeGenerationEngine, FakeOllamaState
from support import parse_sse_events, session_headers


@pytest.fixture(params=["memory", "database"])
def chats(request: pytest.FixtureRequest, tmp_path: Path):
    """Both implementations of the chat repository contract."""
    if request.param == "memory":
        return InMemoryChatRepository()
    return LegacyDatabaseChatRepository(DatabaseManager(db_path=str(tmp_path / "chats.sqlite")))


def _messages(chats, thread_id: str = "t1") -> list[dict[str, Any]]:
    return list(chats.get_chat(thread_id)["messages"])


def _translated_chat(chats) -> str:
    chats.create_chat("t1", "Chat")
    chats.add_message("t1", "user", "Hola")
    return chats.add_message("t1", "assistant", "Hola, ¿cómo estás?", original_content="Hello, how are you?")


def test_an_answer_is_stored_with_the_text_the_model_wrote(chats) -> None:
    _translated_chat(chats)

    user, assistant = _messages(chats)

    assert assistant["content"] == "Hola, ¿cómo estás?"
    assert assistant["original_content"] == "Hello, how are you?"
    assert user["original_content"] is None


def test_an_answer_without_a_translation_has_no_original(chats) -> None:
    chats.create_chat("t1", "Chat")
    chats.add_message("t1", "assistant", "Just an answer")

    (message,) = _messages(chats)

    assert message["original_content"] is None


def test_only_an_assistant_message_can_carry_an_original(chats) -> None:
    chats.create_chat("t1", "Chat")
    chats.add_message("t1", "user", "What the user typed", original_content="not an answer")

    (message,) = _messages(chats)

    assert message["original_content"] is None


def test_replacing_an_answer_sets_or_clears_its_original(chats) -> None:
    message_id = _translated_chat(chats)

    # A regeneration that was not translated must not keep the old answer's original.
    chats.replace_message("t1", message_id, "A fresh answer")
    assert _messages(chats)[1]["original_content"] is None

    chats.replace_message("t1", message_id, "Otra respuesta", original_content="Another answer")
    replaced = _messages(chats)[1]
    assert (replaced["content"], replaced["original_content"]) == ("Otra respuesta", "Another answer")

    chats.replace_message("t1", message_id, "Plain again")
    assert _messages(chats)[1]["original_content"] is None


def test_a_fork_keeps_the_original_of_the_answers_it_copies(chats) -> None:
    message_id = _translated_chat(chats)

    chats.fork_chat("t1", message_id, "t2")

    forked = _messages(chats, "t2")[1]
    assert (forked["content"], forked["original_content"]) == ("Hola, ¿cómo estás?", "Hello, how are you?")


def _write_version_4_database(path: Path) -> None:
    """A database as the release before this column left it, with one answer in it."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL, timestamp TEXT NOT NULL, group_id TEXT);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, sources TEXT, thoughts TEXT, attachments TEXT,
                generation_stats_json TEXT, timestamp TEXT NOT NULL
            );
            CREATE TABLE chat_groups (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
                collapsed INTEGER NOT NULL DEFAULT 0, timestamp TEXT NOT NULL
            );
            INSERT INTO threads VALUES ('old', 'Existing', '2026-01-01T00:00:00Z', NULL);
            INSERT INTO messages (thread_id, role, content, timestamp)
                VALUES ('old', 'assistant', 'An answer from before', '2026-01-01T00:00:00Z');
            PRAGMA user_version = 4;
            """
        )
        connection.commit()
    finally:
        connection.close()


def _columns(path: Path | str) -> set[str]:
    probe = sqlite3.connect(path)
    try:
        return {row[1] for row in probe.execute("PRAGMA table_info(messages)")}
    finally:
        probe.close()


def test_a_database_from_before_the_column_upgrades_and_reads_as_untranslated(tmp_path: Path) -> None:
    path = tmp_path / "chats.sqlite"
    _write_version_4_database(path)

    manager = DatabaseManager(db_path=str(path))

    # The release before this one can still be restored from what was kept.
    snapshot = Path(f"{path}.pre-v4.bak")
    assert snapshot.exists()
    assert "original_content" not in _columns(snapshot)
    assert "original_content" in _columns(path)
    (old,) = manager.load_chat("old")["messages"]
    assert (old["content"], old["original_content"]) == ("An answer from before", None)

    manager.add_message("old", "assistant", "Traducido", original_content="Translated")
    assert manager.load_chat("old")["messages"][-1]["original_content"] == "Translated"


# --- end to end ---------------------------------------------------------------


class _RecordingEngine(FakeGenerationEngine):
    """The shipped fake, plus a record of the history and title text it is handed."""

    def __init__(self, state: FakeOllamaState, history_seen: list, titles_seen: list) -> None:
        super().__init__(state)
        self._history_seen = history_seen
        self._titles_seen = titles_seen

    def fit_history(self, messages, **kwargs):
        self._history_seen.append([dict(message) for message in messages])
        return super().fit_history(messages, **kwargs)

    def generate_chat_title(self, chat_history, **kwargs):
        self._titles_seen.append(chat_history)
        return super().generate_chat_title(chat_history, **kwargs)


def _run_turn(client: TestClient, headers: dict[str, str], **payload: Any) -> dict[str, Any]:
    accepted = client.post("/api/v1/generations", json=payload, headers=headers)
    assert accepted.status_code == 202, accepted.text
    with client.stream(
        "GET", f"/api/v1/generations/{accepted.json()['job_id']}/events", headers=headers
    ) as response:
        events = parse_sse_events("".join(response.iter_text()))
    assert events[-1]["event"] == "generation.completed", events[-1]
    return events[-1]["data"]


def test_a_translated_turn_stores_both_texts_shows_the_translation_and_feeds_back_the_original() -> None:
    state = FakeOllamaState()
    history_seen: list = []
    titles_seen: list = []
    dependencies = build_demo_dependencies(ollama_state=state)
    dependencies.settings = InMemorySettingsRepository(
        CortexSettings(translation=TranslationSettings(enabled=True, target_language="Spanish"))
    )
    dependencies.generation = GenerationService(
        history_loader=lambda thread_id: (dependencies.chats.get_chat(thread_id) or {}).get("messages", []),
        memory_loader=dependencies.memories.get_memos,
        engine_factory=lambda snapshot: _RecordingEngine(state, history_seen, titles_seen),
    )
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = session_headers(client, app)

        first = _run_turn(client, headers, request_id="turn-1", user_input="hello")
        thread_id = first["thread_id"]
        _run_turn(client, headers, request_id="turn-2", thread_id=thread_id, user_input="and again")

        # What the user reads is the translation, and nothing else about it is exposed.
        shown = client.get(f"/api/v1/chats/{thread_id}", headers=headers).json()["messages"]
        assert shown[1]["content"] == "[Spanish] Echo: hello"
        assert shown[3]["content"] == "[Spanish] Echo: and again"
        assert all("original_content" not in message for message in shown)
        assert first["response"] == "[Spanish] Echo: hello"

        # What is stored keeps the answer as the model wrote it.
        stored = dependencies.chats.get_chat(thread_id)["messages"]
        assert [message["original_content"] for message in stored] == [
            None,
            "Echo: hello",
            None,
            "Echo: and again",
        ]

    # The second turn showed the model its own untranslated first answer.
    second_turn_history = history_seen[1]
    assert [(message["role"], message["content"]) for message in second_turn_history] == [
        ("user", "hello"),
        ("assistant", "Echo: hello"),
    ]
    # The title is made from the untranslated answer too.
    assert titles_seen and "Assistant: Echo: hello" in titles_seen[0]
    assert "[Spanish]" not in titles_seen[0]
