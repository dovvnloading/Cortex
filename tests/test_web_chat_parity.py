"""Stage 4 API parity tests for persisted streaming chat workflows."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.testing.fake_ollama import FakeOllamaState, create_fake_ollama_app


def _session(client: TestClient, app) -> dict[str, str]:
    token = client.post(
        "/api/v1/session/exchange",
        json={"bootstrap_token": app.state.session_manager.bootstrap_token},
    ).json()["session_token"]
    return {"Authorization": f"Bearer {token}"}


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
