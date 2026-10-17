"""One generation turn reads the transcript once, and blocking work stays off the loop.

A full-transcript read on the event loop stalls every other request, the live
stream the user is watching included, and a send used to make three of them.
These tests wrap the dependencies in a proxy that notes every call and whether
it ran on the loop's thread, so both properties are observed rather than
inferred from the code.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest

from cortex_backend.api import create_app
from cortex_backend.repositories.chats import ChatRepository, LegacyDatabaseChatRepository
from cortex_backend.repositories.storage import DatabaseManager
from cortex_backend.services.generation import HISTORY_MESSAGE_FIELDS, GenerationService
from cortex_backend.testing import (
    DurableFakeCoordinator,
    build_demo_dependencies,
    install_execution_preview,
)
from cortex_backend.testing.fake_ollama import FakeGenerationEngine, FakeOllamaState
from cortex_backend.execution.repository import ExecutionRepository
from support import parse_sse_events, session_headers


@dataclass
class _Probe:
    """What the proxies and the spy engine saw."""

    calls: Counter[str] = field(default_factory=Counter)
    on_loop: list[str] = field(default_factory=list)
    history_loads: list[str] = field(default_factory=list)
    engine_histories: list[list[dict[str, Any]]] = field(default_factory=list)

    def reset(self) -> None:
        self.calls.clear()
        self.on_loop.clear()
        self.history_loads.clear()
        self.engine_histories.clear()

    def note(self, name: str) -> None:
        self.calls[name] += 1
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop on this thread: the call was made off the loop
        self.on_loop.append(name)


class _Watched:
    """Forward every call to ``inner``, noting it and which thread made it."""

    def __init__(self, inner: object, label: str, probe: _Probe) -> None:
        self._inner = inner
        self._label = label
        self._probe = probe

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            self._probe.note(f"{self._label}.{name}")
            return attribute(*args, **kwargs)

        return call


class _SpyEngine(FakeGenerationEngine):
    """Records the history the generation service hands the model."""

    def __init__(self, state: FakeOllamaState, probe: _Probe) -> None:
        super().__init__(state)
        self._probe = probe

    def fit_history(self, messages: list[dict[str, Any]], **kwargs: Any):
        self._probe.engine_histories.append([dict(message) for message in messages])
        return super().fit_history(messages, **kwargs)


def _probed_app(*, chats: ChatRepository | None = None) -> tuple[Any, _Probe]:
    probe = _Probe()
    state = FakeOllamaState()
    dependencies = build_demo_dependencies(ollama_state=state)
    memories = dependencies.memories
    if chats is not None:
        dependencies.chats = chats
    dependencies.chats = _Watched(dependencies.chats, "chats", probe)
    dependencies.settings = _Watched(dependencies.settings, "settings", probe)
    dependencies.models = _Watched(dependencies.models, "models", probe)

    def unexpected_history_load(thread_id: str) -> list[dict[str, Any]]:
        # The API hands the service its history. Loading it again is the
        # third full read this test exists to keep out.
        probe.history_loads.append(thread_id)
        return []

    dependencies.generation = GenerationService(
        history_loader=unexpected_history_load,
        memory_loader=memories.get_memos,
        engine_factory=lambda snapshot: _SpyEngine(state, probe),
    )
    return create_app(dependencies, allowed_hosts=("testserver",)), probe


def _finish(client: TestClient, headers: dict[str, str], accepted: Mapping[str, Any]) -> None:
    with client.stream(
        "GET", f"/api/v1/generations/{accepted['job_id']}/events", headers=headers
    ) as response:
        events = parse_sse_events("".join(response.iter_text()))
    assert events[-1]["event"] == "generation.completed", events[-1]


def _send(client: TestClient, headers: dict[str, str], **payload: Any) -> dict[str, Any]:
    accepted = client.post("/api/v1/generations", json=payload, headers=headers)
    assert accepted.status_code == 202, accepted.text
    _finish(client, headers, accepted.json())
    return accepted.json()


def _roles(history: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    return [(str(message["role"]), str(message["content"])) for message in history]


def test_a_new_turn_reads_the_transcript_once_and_off_the_loop() -> None:
    app, probe = _probed_app()
    with TestClient(app) as client:
        headers = session_headers(client, app)
        _send(client, headers, request_id="hygiene-new-1", user_input="hello")
        first_turn = Counter(probe.calls)
        first_turn_on_loop = list(probe.on_loop)
        first_turn_history_loads = list(probe.history_loads)

        probe.reset()
        thread_id = client.get("/api/v1/chats", headers=headers).json()[0]["id"]
        _send(
            client, headers, request_id="hygiene-new-2", thread_id=thread_id, user_input="again"
        )

    # At most one transcript read per turn, and a partial one: the full read
    # decodes the reasoning, sources and stats of every reply, none of which
    # history uses. A chat that does not exist yet has nothing to read.
    assert first_turn["chats.get_chat"] == 0, first_turn
    assert probe.calls["chats.get_chat"] == 0, probe.calls
    assert first_turn["chats.get_messages"] == 0, first_turn
    assert probe.calls["chats.get_messages"] == 1, probe.calls
    assert first_turn_history_loads == [] and probe.history_loads == []
    assert first_turn_on_loop == [] and probe.on_loop == []


class _ColumnsSeen:
    """Note the fields each partial transcript read asked for."""

    def __init__(self, inner: ChatRepository) -> None:
        self.inner = inner
        self.columns: list[tuple[str, ...]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def get_messages(self, thread_id: str, *, columns: Sequence[str]) -> Any:
        self.columns.append(tuple(columns))
        return self.inner.get_messages(thread_id, columns=columns)


def test_a_new_turn_reads_only_the_fields_its_history_is_built_from(tmp_path: Path) -> None:
    seen = _ColumnsSeen(
        LegacyDatabaseChatRepository(DatabaseManager(db_path=str(tmp_path / "chats.sqlite")))
    )
    app, probe = _probed_app(chats=seen)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        first = _send(client, headers, request_id="fields-1", user_input="one")
        probe.reset()
        _send(client, headers, request_id="fields-2", thread_id=first["thread_id"], user_input="two")

    assert seen.columns[-1] == HISTORY_MESSAGE_FIELDS
    assert not {"thoughts", "sources", "stats"} & set(seen.columns[-1])
    # And the history is complete with them: the earlier exchange arrives whole.
    assert _roles(probe.engine_histories[0]) == [("user", "one"), ("assistant", "Echo: one")]


def test_a_regeneration_reads_the_transcript_once_and_off_the_loop() -> None:
    app, probe = _probed_app()
    with TestClient(app) as client:
        headers = session_headers(client, app)
        accepted = _send(client, headers, request_id="hygiene-seed", user_input="hello")
        chat = client.get(f"/api/v1/chats/{accepted['thread_id']}", headers=headers).json()
        reply_id = chat["messages"][-1]["id"]

        probe.reset()
        regenerated = client.post(
            f"/api/v1/chats/{accepted['thread_id']}/regenerations",
            json={"request_id": "hygiene-regenerate", "message_id": reply_id},
            headers=headers,
        )
        assert regenerated.status_code == 202, regenerated.text
        _finish(client, headers, regenerated.json())

    assert probe.calls["chats.get_chat"] == 1, probe.calls
    assert probe.history_loads == []
    assert probe.on_loop == []


def test_blocking_reads_before_a_models_job_are_off_the_loop() -> None:
    app, probe = _probed_app()
    with TestClient(app) as client:
        headers = session_headers(client, app)
        accepted = client.post("/api/v1/jobs/models", headers=headers)
        assert accepted.status_code == 202
        _wait_for_job(client, headers, accepted.json()["job_id"])

    assert probe.calls["settings.load"] >= 1
    assert probe.on_loop == []


def test_blocking_reads_before_a_gguf_download_are_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "cortex_backend.api.routers.models.download_gguf", lambda *args, **kwargs: None
    )
    app, probe = _probed_app()
    app.state.default_gguf_models_dir = tmp_path
    with TestClient(app) as client:
        headers = session_headers(client, app)
        accepted = client.post(
            "/api/v1/models/gguf/downloads",
            json={"source": "url", "url": "https://example.com/model.gguf"},
            headers=headers,
        )
        assert accepted.status_code == 202, accepted.text
        _wait_for_job(client, headers, accepted.json()["job_id"])

    assert probe.calls["settings.load"] >= 1
    assert probe.on_loop == []


def test_opening_an_execution_stream_reads_the_job_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    app = install_execution_preview(
        create_app(
            build_demo_dependencies(),
            allowed_hosts=("testserver",),
            preview=True,
            execution_coordinator=DurableFakeCoordinator(repository),
        )
    )
    probe = _Probe()
    real_get_job = repository.get_job

    def watched_get_job(*args: Any, **kwargs: Any) -> Any:
        probe.note("repository.get_job")
        return real_get_job(*args, **kwargs)

    monkeypatch.setattr(repository, "get_job", watched_get_job)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        accepted = client.post(
            "/api/v1/execution/preview/fake",
            json={"request_id": "hygiene-stream", "steps": 1},
            headers=headers,
        )
        assert accepted.status_code == 202
        probe.reset()
        events = client.get(f"/api/v1/execution/{accepted.json()['job_id']}/events", headers=headers)
        assert events.status_code == 200

    assert probe.calls["repository.get_job"] >= 1
    assert probe.on_loop == []


def test_existence_checks_do_not_read_the_transcript() -> None:
    """Renaming and filing a chat only ask whether it exists."""
    app, probe = _probed_app()
    with TestClient(app) as client:
        headers = session_headers(client, app)
        accepted = _send(client, headers, request_id="hygiene-exists", user_input="hello")
        thread_id = accepted["thread_id"]
        group = client.post("/api/v1/chat-groups", json={"name": "Work"}, headers=headers).json()

        probe.reset()
        renamed = client.patch(
            f"/api/v1/chats/{thread_id}", json={"title": "Renamed"}, headers=headers
        )
        rename_reads = probe.calls["chats.get_chat"]
        rename_existence_checks = {
            name: count
            for name, count in probe.calls.items()
            if name in {"chats.chat_exists", "chats.get_chat_overview", "chats.get_messages"}
        }

        probe.reset()
        filed = client.patch(
            f"/api/v1/chats/{thread_id}/group", json={"group_id": group["id"]}, headers=headers
        )
        file_reads = probe.calls["chats.get_chat"]
        list_reads = probe.calls["chats.list_summaries"]

    assert renamed.status_code == filed.status_code == 200
    assert filed.json() == {
        "id": thread_id,
        "title": "Renamed",
        "timestamp": filed.json()["timestamp"],
        "group_id": group["id"],
    }
    # The response of a rename is the chat itself, so that one read is the point
    # of the call; the existence check before it must not add another.
    assert rename_reads == 1
    assert rename_existence_checks == {"chats.chat_exists": 1}
    assert file_reads == 0
    assert list_reads == 0


def test_a_fork_checks_its_message_without_reading_the_whole_transcript() -> None:
    app, probe = _probed_app()
    with TestClient(app) as client:
        headers = session_headers(client, app)
        accepted = _send(client, headers, request_id="hygiene-fork", user_input="hello")
        thread_id = accepted["thread_id"]
        messages = client.get(f"/api/v1/chats/{thread_id}", headers=headers).json()["messages"]

        probe.reset()
        forked = client.post(
            f"/api/v1/chats/{thread_id}/forks",
            json={"message_id": messages[0]["id"]},
            headers=headers,
        )
        fork_reads = Counter(probe.calls)

        missing = client.post(
            f"/api/v1/chats/{thread_id}/forks",
            json={"message_id": "no-such-message"},
            headers=headers,
        )
        unknown_chat = client.post(
            "/api/v1/chats/no-such-chat/forks",
            json={"message_id": messages[0]["id"]},
            headers=headers,
        )

    assert forked.status_code == 201, forked.text
    assert [message["content"] for message in forked.json()["messages"]] == ["hello"]
    # The one full read left is of the new fork, which is the response.
    assert fork_reads["chats.get_chat"] == 1, fork_reads
    assert fork_reads["chats.get_messages"] == 1, fork_reads
    assert missing.status_code == 404
    assert unknown_chat.status_code == 404
    assert unknown_chat.json()["detail"] == "Chat not found."


def _history_after(setup: Callable[[TestClient, dict[str, str]], str], **turn: Any) -> list[tuple[str, str]]:
    """What the model is given as history for one further turn."""
    app, probe = _probed_app()
    with TestClient(app) as client:
        headers = session_headers(client, app)
        thread_id = setup(client, headers)
        probe.reset()
        _send(client, headers, request_id="history-turn", thread_id=thread_id, **turn)
    assert len(probe.engine_histories) == 1
    return _roles(probe.engine_histories[0])


def _chat_with(*messages: tuple[str, str]) -> Callable[[TestClient, dict[str, str]], str]:
    def setup(client: TestClient, headers: dict[str, str]) -> str:
        thread_id = client.post("/api/v1/chats", json={"title": "History"}, headers=headers).json()["id"]
        # No route writes a raw turn, so the earlier turns go in as the
        # generation worker would have written them.
        for role, content in messages:
            client.app.state.dependencies.chats.add_message(thread_id, role, content)
        return thread_id

    return setup


def test_the_model_gets_the_earlier_turns_and_never_the_one_it_is_answering() -> None:
    history = _history_after(
        _chat_with(("user", "q1"), ("assistant", "a1")), user_input="q2"
    )

    assert history == [("user", "q1"), ("assistant", "a1")]


def test_a_new_chat_has_no_history() -> None:
    app, probe = _probed_app()
    with TestClient(app) as client:
        headers = session_headers(client, app)
        _send(client, headers, request_id="history-first", user_input="hello")

    assert [_roles(history) for history in probe.engine_histories] == [[]]


def test_an_earlier_unanswered_message_stays_in_the_history() -> None:
    """A prior attempt that failed leaves a user turn with no reply. The next
    turn still shows it to the model; only the turn being answered is dropped."""
    history = _history_after(
        _chat_with(("user", "q1"), ("assistant", "a1"), ("user", "unanswered")),
        user_input="q2",
    )

    assert history == [("user", "q1"), ("assistant", "a1"), ("user", "unanswered")]


def test_regenerating_a_reply_gives_the_model_the_turns_before_its_question() -> None:
    app, probe = _probed_app()
    with TestClient(app) as client:
        headers = session_headers(client, app)
        thread_id = _chat_with(("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2"))(
            client, headers
        )
        reply_id = client.get(f"/api/v1/chats/{thread_id}", headers=headers).json()["messages"][-1]["id"]
        probe.reset()
        regenerated = client.post(
            f"/api/v1/chats/{thread_id}/regenerations",
            json={"request_id": "history-regenerate", "message_id": reply_id},
            headers=headers,
        )
        assert regenerated.status_code == 202, regenerated.text
        _finish(client, headers, regenerated.json())

    assert [_roles(history) for history in probe.engine_histories] == [
        [("user", "q1"), ("assistant", "a1")]
    ]


def test_retrying_an_unanswered_message_does_not_repeat_it_in_the_history() -> None:
    app, probe = _probed_app()
    with TestClient(app) as client:
        headers = session_headers(client, app)
        thread_id = _chat_with(("user", "q1"), ("assistant", "a1"), ("user", "unanswered"))(
            client, headers
        )
        target = client.get(f"/api/v1/chats/{thread_id}", headers=headers).json()["messages"][-1]["id"]
        probe.reset()
        regenerated = client.post(
            f"/api/v1/chats/{thread_id}/regenerations",
            json={"request_id": "history-retry", "message_id": target},
            headers=headers,
        )
        assert regenerated.status_code == 202, regenerated.text
        _finish(client, headers, regenerated.json())

    assert [_roles(history) for history in probe.engine_histories] == [
        [("user", "q1"), ("assistant", "a1")]
    ]


def test_the_sqlite_repository_takes_the_same_single_read_path(tmp_path: Path) -> None:
    """The same flow over the real database, where the transcript is decoded
    from rows and the revision is a stored counter."""
    app, probe = _probed_app(
        chats=LegacyDatabaseChatRepository(DatabaseManager(db_path=str(tmp_path / "chats.sqlite")))
    )
    with TestClient(app) as client:
        headers = session_headers(client, app)
        first = _send(client, headers, request_id="sqlite-1", user_input="one")
        thread_id = first["thread_id"]
        _send(client, headers, request_id="sqlite-2", thread_id=thread_id, user_input="two")
        chat = client.get(f"/api/v1/chats/{thread_id}", headers=headers).json()
        probe.reset()
        regenerated = client.post(
            f"/api/v1/chats/{thread_id}/regenerations",
            json={"request_id": "sqlite-regenerate", "message_id": chat["messages"][-1]["id"]},
            headers=headers,
        )
        assert regenerated.status_code == 202, regenerated.text
        _finish(client, headers, regenerated.json())
        regeneration_reads = probe.calls["chats.get_chat"]
        after = client.get(f"/api/v1/chats/{thread_id}", headers=headers).json()

    assert [message["role"] for message in after["messages"]] == ["user", "assistant"] * 2
    assert after["messages"][-1]["content"] == "Echo: two"
    assert probe.history_loads == []
    assert regeneration_reads == 1
    assert _roles(probe.engine_histories[0]) == [("user", "one"), ("assistant", "Echo: one")]


def _wait_for_job(client: TestClient, headers: dict[str, str], job_id: str) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
        if body["status"] in {"succeeded", "failed", "cancelled"}:
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish within 10s")
