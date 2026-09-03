"""Stage 4 API parity tests for persisted streaming chat workflows."""

from __future__ import annotations

import json
from threading import Event
import time

from fastapi.testclient import TestClient

import cortex_backend.api.routes as api_routes
from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.testing.fake_ollama import FakeOllamaState, create_fake_ollama_app
from support import session_headers as _session



def _events(body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_fake_ollama_streams_thinking_content_and_can_disconnect():
    fake = create_fake_ollama_app(
        FakeOllamaState(
            generation_response="streamed response",
            generation_thoughts="private reasoning",
            disconnect_after_chunks=2,
        )
    )
    with TestClient(fake) as client:
        response = client.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    lines = [json.loads(line) for line in response.text.splitlines()]
    assert lines[0]["message"]["thinking"] == "private reasoning"
    assert lines[1]["message"]["content"] == "streamed res"
    assert len(lines) == 2


def test_new_generation_persists_both_turns_and_replays_parity_events():
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "new-1", "user_input": "hello"},
            headers=headers,
        )
        assert accepted.status_code == 202
        payload = accepted.json()
        assert payload["thread_id"]
        assert payload["user_message_id"]

        duplicate = client.post(
            "/api/v1/generations",
            json={"request_id": "new-1", "user_input": "hello"},
            headers=headers,
        )
        assert duplicate.json()["job_id"] == payload["job_id"]

        with client.stream(
            "GET",
            f"/api/v1/generations/{payload['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))
        assert [event["event"] for event in events][-2:] == [
            "generation.persisting",
            "generation.completed",
        ]
        assert any(event["event"] == "generation.content_delta" for event in events)
        assert all(
            event.get("data", {}).get("message") != "START_FINAL_ANIMATION"
            for event in events
        )
        assert events[-1]["data"]["assistant_message_id"]

        chat = client.get(f"/api/v1/chats/{payload['thread_id']}", headers=headers).json()
        assert [message["role"] for message in chat["messages"]] == ["user", "assistant"]
        assert chat["title"] == "hello"
        assert all(message["id"] for message in chat["messages"])
        assert chat["revision"] == 2

        replay = client.get(
            f"/api/v1/generations/{payload['job_id']}/events",
            headers={**headers, "Last-Event-ID": "5"},
        )
        replay_events = _events(replay.text)
        assert replay_events and all(event["event_id"] > 5 for event in replay_events)


def test_generation_rejects_malformed_new_chat_thread_id():
    """A client-supplied body ``thread_id`` gates chat creation the same way
    the ``/messages`` path parameter does.

    When no chat with the given id exists yet, ``_start_generation_job``
    creates one using that literal string as its permanent id. A
    pathological id must be rejected with 422 before that happens, and no
    chat may be created as a side effect of the rejected request.
    """

    dependencies = build_demo_dependencies()
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        for bad_thread_id in ("has space", "slash/like", "control\x07char"):
            response = client.post(
                "/api/v1/generations",
                json={"thread_id": bad_thread_id, "user_input": "hello"},
                headers=headers,
            )
            assert response.status_code == 422, bad_thread_id
            assert "thread_id" in response.json()["detail"]
            assert dependencies.chats.get_chat(bad_thread_id) is None


def test_generation_with_valid_new_thread_id_creates_chat():
    """A well-formed client-chosen thread_id may still start a brand new chat."""

    dependencies = build_demo_dependencies()
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        new_thread_id = "Client-Chosen_Thread-456"
        accepted = client.post(
            "/api/v1/generations",
            json={"thread_id": new_thread_id, "user_input": "hello"},
            headers=headers,
        )
        assert accepted.status_code == 202
        assert accepted.json()["thread_id"] == new_thread_id
        chat = dependencies.chats.get_chat(new_thread_id)
        assert chat is not None
        assert chat["messages"][0]["content"] == "hello"


def test_new_generation_persists_model_title_and_returns_it_in_completion_event():
    state = FakeOllamaState(title_response="Cortex launch planning")
    app = create_app(build_demo_dependencies(ollama_state=state), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "title-1", "user_input": "Plan the Cortex launch"},
            headers=headers,
        ).json()
        with client.stream(
            "GET",
            f"/api/v1/generations/{accepted['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))

        completed = events[-1]
        assert completed["event"] == "generation.completed"
        assert completed["data"]["title"] == "Cortex launch planning"
        assert "suggestions" not in completed["data"]
        chat = client.get(
            f"/api/v1/chats/{accepted['thread_id']}", headers=headers
        ).json()
        assert chat["title"] == "Cortex launch planning"


def test_generation_surfaces_local_runtime_loading_status_over_sse():
    """An engine that reports startup progress (e.g. a locally-managed
    llama.cpp runtime downloading/starting) via set_status_callback must
    reach the client as a real, schema-valid SSE event -- this is an
    end-to-end regression test for the full ProgressEvent -> GenerationEvent
    pipeline, not just the in-process phase list, since a phase can be valid
    at the service layer while still being rejected by the API schema."""
    state = FakeOllamaState(status_updates=("Downloading the local model runtime...", "Starting the local model..."))
    app = create_app(build_demo_dependencies(ollama_state=state), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "loading-1", "user_input": "hello"},
            headers=headers,
        ).json()
        with client.stream(
            "GET",
            f"/api/v1/generations/{accepted['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))

        loading_events = [event for event in events if event["event"] == "generation.loading_model"]
        assert [event["data"].get("message") for event in loading_events] == [
            "Downloading the local model runtime...",
            "Starting the local model...",
        ]
        assert events[-1]["event"] == "generation.completed"


def test_failed_generation_keeps_user_turn_without_successful_assistant():
    state = FakeOllamaState(fail_generation=True)
    app = create_app(build_demo_dependencies(ollama_state=state), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "failed-1", "user_input": "will fail"},
            headers=headers,
        ).json()
        with client.stream(
            "GET",
            f"/api/v1/generations/{accepted['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))
        assert events[-1]["event"] == "generation.failed"
        chat = client.get(
            f"/api/v1/chats/{accepted['thread_id']}", headers=headers
        ).json()
        assert [message["role"] for message in chat["messages"]] == ["user"]


def test_model_memory_proposal_is_not_persisted_and_does_not_invalidate_assistant(
    monkeypatch,
):
    dependencies = build_demo_dependencies()
    code_observations: list[bool] = []
    memory_observations: list[bool] = []

    def assistant_exists() -> bool:
        summaries = dependencies.chats.list_summaries()
        if not summaries:
            return False
        chat = dependencies.chats.get_chat(summaries[0]["id"])
        return bool(chat and chat["messages"][-1]["role"] == "assistant")

    def fail_code_proposal(*_args, **_kwargs):
        code_observations.append(assistant_exists())
        raise RuntimeError("derived code failure")

    def fail_memory(_memo):
        memory_observations.append(assistant_exists())
        raise RuntimeError("derived memory failure")

    monkeypatch.setattr(api_routes, "_queue_code_proposal", fail_code_proposal)
    monkeypatch.setattr(dependencies.memories, "add_memo", fail_memory)
    app = create_app(dependencies, allowed_hosts=("testserver",))

    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "derived-failure-1", "user_input": "!remember tea"},
            headers=headers,
        ).json()
        with client.stream(
            "GET",
            f"/api/v1/generations/{accepted['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))

        assert events[-1]["event"] == "generation.completed"
        assert code_observations == [True]
        # Model proposals are untrusted and no longer reach the memory
        # repository without an explicit user-confirmation flow.
        assert memory_observations == []
        assert dependencies.memories.get_memos() == []
        chat = client.get(
            f"/api/v1/chats/{accepted['thread_id']}", headers=headers
        ).json()
        assert [message["role"] for message in chat["messages"]] == [
            "user",
            "assistant",
        ]


def test_model_clear_proposal_is_returned_without_clearing_memory(monkeypatch):
    dependencies = build_demo_dependencies()
    dependencies.memories.add_memo("keep this memory")
    clear_observations: list[bool] = []

    def fail_clear():
        clear_observations.append(True)
        raise AssertionError("model output must not clear permanent memory")

    monkeypatch.setattr(dependencies.memories, "clear_memos", fail_clear)
    app = create_app(dependencies, allowed_hosts=("testserver",))

    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "clear-proposal-1", "user_input": "!clear-memory"},
            headers=headers,
        ).json()
        with client.stream(
            "GET",
            f"/api/v1/generations/{accepted['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))

        assert events[-1]["event"] == "generation.completed"
        assert events[-1]["data"]["clear_requested"] is True
        assert clear_observations == []
        assert dependencies.memories.get_memos() == ["keep this memory"]


def test_precommit_cancellation_waits_for_worker_and_skips_response_persistence():
    state = FakeOllamaState(
        generation_delay_seconds=0.2,
        title_response="This title must not be persisted",
    )
    app = create_app(build_demo_dependencies(ollama_state=state), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "cancel-1", "user_input": "cancel this response"},
            headers=headers,
        ).json()
        for _ in range(100):
            current = client.get(
                f"/api/v1/generations/{accepted['job_id']}", headers=headers
            ).json()
            if current["status"] == "running":
                break
            time.sleep(0.002)
        else:
            raise AssertionError("generation did not begin running")

        cancelled = client.post(
            f"/api/v1/generations/{accepted['job_id']}/cancel",
            headers=headers,
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelling"
        blocked = client.post(
            "/api/v1/generations",
            json={"thread_id": accepted["thread_id"], "user_input": "must wait"},
            headers=headers,
        )
        assert blocked.status_code == 409

        with client.stream(
            "GET",
            f"/api/v1/generations/{accepted['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))

        event_names = [event["event"] for event in events]
        assert event_names[-2:] == ["generation.cancelling", "generation.cancelled"]
        assert "generation.completed" not in event_names
        chat = client.get(
            f"/api/v1/chats/{accepted['thread_id']}", headers=headers
        ).json()
        assert chat["title"] == "New Chat"
        assert [message["role"] for message in chat["messages"]] == ["user"]


def test_cancellation_after_commit_does_not_downgrade_the_persisted_response():
    dependencies = build_demo_dependencies()
    title_started = Event()
    release_title = Event()

    def delayed_title(snapshot, response):
        del snapshot, response
        title_started.set()
        release_title.wait(timeout=1)
        return "Late title"

    dependencies.generation.generate_chat_title = delayed_title
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        try:
            accepted = client.post(
                "/api/v1/generations",
                json={"request_id": "cancel-title-1", "user_input": "make a title"},
                headers=headers,
            ).json()
            assert title_started.wait(timeout=1), "title generator did not start"

            cancelled = client.post(
                f"/api/v1/generations/{accepted['job_id']}/cancel",
                headers=headers,
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "running"
            blocked = client.post(
                "/api/v1/generations",
                json={"thread_id": accepted["thread_id"], "user_input": "must wait"},
                headers=headers,
            )
            assert blocked.status_code == 409
        finally:
            release_title.set()

        with client.stream(
            "GET",
            f"/api/v1/generations/{accepted['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))
        assert events[-1]["event"] == "generation.completed"
        assert "generation.cancelling" not in [event["event"] for event in events]
        assert "generation.cancelled" not in [event["event"] for event in events]
        chat = client.get(
            f"/api/v1/chats/{accepted['thread_id']}", headers=headers
        ).json()
        assert chat["title"] == "Late title"
        assert [message["role"] for message in chat["messages"]] == [
            "user",
            "assistant",
        ]


def test_hung_title_generation_times_out_and_falls_back_without_blocking_completion(
    monkeypatch,
):
    """Regression guard: chat-title generation runs after begin_commit, with
    no cancel_event and no bound from JobRegistry.shutdown (see
    CHAT_TITLE_TIMEOUT_SECONDS in api/routes.py). A hung title model must not
    stall the job -- it should time out quickly and fall back to the same
    default title an outright title-generation failure already produces."""
    monkeypatch.setattr(api_routes, "CHAT_TITLE_TIMEOUT_SECONDS", 0.2)
    dependencies = build_demo_dependencies()
    title_started = Event()
    never_release = Event()

    def hanging_title(snapshot, response):
        del snapshot, response
        title_started.set()
        never_release.wait()  # simulates a hung local model; never returns
        return "This title must never be used"

    dependencies.generation.generate_chat_title = hanging_title
    app = create_app(dependencies, allowed_hosts=("testserver",))
    try:
        with TestClient(app) as client:
            headers = _session(client, app)
            started_at = time.monotonic()
            accepted = client.post(
                "/api/v1/generations",
                json={"request_id": "title-timeout-1", "user_input": "hello there"},
                headers=headers,
            ).json()
            with client.stream(
                "GET",
                f"/api/v1/generations/{accepted['job_id']}/events",
                headers=headers,
            ) as response:
                events = _events("".join(response.iter_text()))
            elapsed = time.monotonic() - started_at
            assert title_started.wait(timeout=1), "title generator did not start"
            assert elapsed < 5, "a hung title call must not block job completion"
            assert events[-1]["event"] == "generation.completed"

            chat = client.get(
                f"/api/v1/chats/{accepted['thread_id']}", headers=headers
            ).json()
            assert chat["title"] not in {"New Chat", "This title must never be used"}
            assert [message["role"] for message in chat["messages"]] == [
                "user",
                "assistant",
            ]
    finally:
        never_release.set()  # let the abandoned daemon thread unblock and exit


def test_regeneration_fills_in_a_reply_for_a_dangling_user_turn_without_duplicating_it():
    """A generation that fails after its user message was already admitted
    (the model call itself errors, before any assistant reply is persisted)
    leaves the thread with a user turn and no reply. Regression guard: the
    only way to retry used to be posting the same text as a brand new
    message, duplicating the user's turn. Regenerating against that
    message's own id must add the missing reply instead."""
    state = FakeOllamaState(fail_generation=True)
    app = create_app(build_demo_dependencies(ollama_state=state), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "dangling-1", "user_input": "hello"},
            headers=headers,
        ).json()
        with client.stream(
            "GET",
            f"/api/v1/generations/{accepted['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))
        assert events[-1]["event"] == "generation.failed"

        thread_id = accepted["thread_id"]
        chat = client.get(f"/api/v1/chats/{thread_id}", headers=headers).json()
        assert [m["role"] for m in chat["messages"]] == ["user"]
        user_message_id = chat["messages"][0]["id"]
        assert user_message_id == accepted["user_message_id"]

        state.fail_generation = False
        regeneration = client.post(
            f"/api/v1/chats/{thread_id}/regenerations",
            json={"request_id": "dangling-1-retry", "message_id": user_message_id},
            headers=headers,
        )
        assert regeneration.status_code == 202
        with client.stream(
            "GET",
            f"/api/v1/generations/{regeneration.json()['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))
        assert events[-1]["event"] == "generation.completed"

        after = client.get(f"/api/v1/chats/{thread_id}", headers=headers).json()
        assert [m["role"] for m in after["messages"]] == ["user", "assistant"]
        assert after["messages"][0]["id"] == user_message_id
        assert after["messages"][0]["content"] == "hello"
        assert after["messages"][1]["content"] == "Echo: hello"


def test_regeneration_rejects_a_dangling_turn_that_is_not_the_last_message():
    """Only the thread's current, unanswered turn may be filled in this way
    -- an older user message earlier in the thread must not be retried."""
    state = FakeOllamaState()
    app = create_app(build_demo_dependencies(ollama_state=state), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "first-1", "user_input": "first"},
            headers=headers,
        ).json()
        with client.stream(
            "GET", f"/api/v1/generations/{accepted['job_id']}/events", headers=headers
        ) as response:
            _events("".join(response.iter_text()))
        thread_id = accepted["thread_id"]
        chat = client.get(f"/api/v1/chats/{thread_id}", headers=headers).json()
        first_user_message_id = chat["messages"][0]["id"]

        regeneration = client.post(
            f"/api/v1/chats/{thread_id}/regenerations",
            json={"request_id": "stale-retry", "message_id": first_user_message_id},
            headers=headers,
        )
        assert regeneration.status_code == 409
        assert "final message" in regeneration.json()["detail"]


def test_fork_and_regeneration_use_message_ids_and_preserve_original_until_success():
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            json={"request_id": "fork-1", "user_input": "first"},
            headers=headers,
        ).json()
        with client.stream(
            "GET",
            f"/api/v1/generations/{accepted['job_id']}/events",
            headers=headers,
        ) as response:
            _events("".join(response.iter_text()))
        chat = client.get(
            f"/api/v1/chats/{accepted['thread_id']}", headers=headers
        ).json()
        assistant_id = chat["messages"][-1]["id"]

        fork = client.post(
            f"/api/v1/chats/{accepted['thread_id']}/forks",
            json={"message_id": assistant_id},
            headers=headers,
        )
        assert fork.status_code == 201
        assert [message["content"] for message in fork.json()["messages"]] == [
            "first",
            "Echo: first",
        ]
        assert fork.json()["id"] != accepted["thread_id"]

        regeneration = client.post(
            f"/api/v1/chats/{accepted['thread_id']}/regenerations",
            json={"request_id": "regen-1", "message_id": assistant_id},
            headers=headers,
        )
        assert regeneration.status_code == 202
        during = client.get(
            f"/api/v1/chats/{accepted['thread_id']}", headers=headers
        ).json()
        assert during["messages"][-1]["id"] == assistant_id
        with client.stream(
            "GET",
            f"/api/v1/generations/{regeneration.json()['job_id']}/events",
            headers=headers,
        ) as response:
            events = _events("".join(response.iter_text()))
        assert events[-1]["event"] == "generation.completed"
        after = client.get(
            f"/api/v1/chats/{accepted['thread_id']}", headers=headers
        ).json()
        assert len(after["messages"]) == 2
        assert after["messages"][-1]["id"] == assistant_id
