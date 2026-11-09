"""A runtime error must not erase already streamed assistant text."""

from __future__ import annotations

from fastapi.testclient import TestClient

from cortex_backend.api import create_app
from cortex_backend.core.generation import ModelOperationError
from cortex_backend.testing import build_demo_dependencies
from support import parse_sse_events as events
from support import session_headers as session

PARTIAL = "The model answered part of this long question before its connection failed. " * 2


class _FailAfterProgress:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def generate(self, *, on_delta=None, **kwargs):
        if on_delta is not None:
            on_delta("content", PARTIAL)
        raise ModelOperationError("Synthetic runtime failure.", operation="generation")


def _drain(client, headers, job_id):
    with client.stream(
        "GET", f"/api/v1/generations/{job_id}/events", headers=headers
    ) as response:
        assert response.status_code == 200
        return events("".join(response.iter_text()))


def test_failure_keeps_visible_partial_response_and_failed_job():
    dependencies = build_demo_dependencies()
    factory = dependencies.generation._engine_factory
    dependencies.generation._engine_factory = lambda snapshot: _FailAfterProgress(
        factory(snapshot)
    )
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            headers=headers,
            json={"request_id": "failed-partial", "user_input": "Explain this"},
        )
        assert accepted.status_code == 202
        job = accepted.json()
        stream_events = _drain(client, headers, job["job_id"])
        assert stream_events[-1]["event"] == "generation.failed"
        assert any(event["event"] == "generation.content_delta" for event in stream_events)
        assert not any(event["event"] == "generation.completed" for event in stream_events)

        response = client.get(f"/api/v1/chats/{job['thread_id']}", headers=headers)
        assert response.status_code == 200
        messages = response.json()["messages"]
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert messages[-1]["content"] == PARTIAL
        assert messages[-1]["stats"]["stop_reason"] == "error"


def test_failed_regeneration_does_not_overwrite_an_existing_answer():
    dependencies = build_demo_dependencies()
    factory = dependencies.generation._engine_factory
    faulting = {"enabled": False}
    dependencies.generation._engine_factory = lambda snapshot: (
        _FailAfterProgress(factory(snapshot))
        if faulting["enabled"]
        else factory(snapshot)
    )
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            headers=headers,
            json={"request_id": "before-failed-regen", "user_input": "Explain this"},
        )
        assert accepted.status_code == 202
        first = accepted.json()
        assert _drain(client, headers, first["job_id"])[-1]["event"] == "generation.completed"
        original = client.get(
            f"/api/v1/chats/{first['thread_id']}", headers=headers
        ).json()["messages"][-1]

        faulting["enabled"] = True
        retry = client.post(
            f"/api/v1/chats/{first['thread_id']}/regenerations",
            headers=headers,
            json={"request_id": "failing-regeneration", "message_id": original["id"]},
        )
        assert retry.status_code == 202
        assert _drain(client, headers, retry.json()["job_id"])[-1]["event"] == "generation.failed"
        messages = client.get(
            f"/api/v1/chats/{first['thread_id']}", headers=headers
        ).json()["messages"]
        assert [item["role"] for item in messages] == ["user", "assistant"]
        assert messages[-1]["id"] == original["id"]
        assert messages[-1]["content"] == original["content"]
        assert (messages[-1].get("stats") or {}).get("stop_reason") != "error"
