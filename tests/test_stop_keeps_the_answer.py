"""Stop keeps the part of the answer the user already watched appear.

With real streaming, a stopped answer is visibly on screen. The runner used to
discard the whole turn on Stop, so the text vanished the moment the chat
reloaded. These tests drive a fake engine that shows text, then blocks until
Stop, synchronised with events rather than sleeps.
"""

from __future__ import annotations

import json
from threading import Event

from fastapi.testclient import TestClient

from cortex_backend.api import create_app
from cortex_backend.core.generation import ModelOperationError
from cortex_backend.testing import build_demo_dependencies
from support import session_headers as _session

# 80 characters or more flushes a delta immediately (services/generation.py),
# so the text is on the stream before the test presses Stop.
SHOWN = "The first half of a careful answer, long enough to reach the client right now. "
REASONING = "Weighing the options before answering, at enough length to be flushed at once. "
NEVER_SHOWN = "Written after Stop, so no client ever received it."
WAIT_SECONDS = 10


def _events(body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


class _StoppableEngine:
    """Shows some text, waits for Stop, then fails the way a stopped engine does."""

    def __init__(self, inner, *, shown: Event, show_text: bool = True, after_stop: str = ""):
        self._inner = inner
        self._shown = shown
        self._show_text = show_text
        self._after_stop = after_stop

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def generate(self, *, on_delta=None, cancellation_event=None, **kwargs):
        if self._show_text and on_delta is not None:
            on_delta("thinking", REASONING)
            on_delta("content", SHOWN)
        self._shown.set()
        assert cancellation_event is not None
        if not cancellation_event.wait(WAIT_SECONDS):
            raise AssertionError("the test never pressed Stop")
        if self._after_stop and on_delta is not None:
            on_delta("content", self._after_stop)
        raise ModelOperationError("Generation was cancelled.", operation="generation")


def _app_with(engine_options: dict, shown: Event):
    dependencies = build_demo_dependencies()
    inner_factory = dependencies.generation._engine_factory
    dependencies.generation._engine_factory = lambda snapshot: _StoppableEngine(
        inner_factory(snapshot), shown=shown, **engine_options
    )
    return create_app(dependencies, allowed_hosts=("testserver",))


def _stop_and_drain(client, headers, job_id: str) -> list[dict]:
    cancelled = client.post(f"/api/v1/generations/{job_id}/cancel", headers=headers)
    assert cancelled.status_code == 200
    with client.stream("GET", f"/api/v1/generations/{job_id}/events", headers=headers) as response:
        return _events("".join(response.iter_text()))


def _start(client, headers, **body) -> dict:
    accepted = client.post("/api/v1/generations", json=body, headers=headers)
    assert accepted.status_code == 202
    return accepted.json()


def test_stop_keeps_what_was_shown_and_marks_it_stopped():
    shown = Event()
    app = _app_with({}, shown)
    with TestClient(app) as client:
        headers = _session(client, app)
        job = _start(client, headers, request_id="stop-1", user_input="explain it")
        assert shown.wait(WAIT_SECONDS)

        events = _stop_and_drain(client, headers, job["job_id"])
        names = [event["event"] for event in events]
        # Still a cancellation, not a success: nothing downstream of a
        # finished turn (title, code proposals) runs for it.
        assert names[-2:] == ["generation.cancelling", "generation.cancelled"]
        assert "generation.completed" not in names

        chat = client.get(f"/api/v1/chats/{job['thread_id']}", headers=headers).json()
        assert [message["role"] for message in chat["messages"]] == ["user", "assistant"]
        kept = chat["messages"][-1]
        assert kept["content"] == SHOWN
        assert kept["thoughts"] == REASONING
        assert kept["stats"]["stopped"] is True
        # The terminal event names the kept answer, so the client can show it
        # instead of reporting a failure.
        assert events[-1]["data"]["assistant_message_id"] == kept["id"]

        # The job is over, so the chat takes a new turn straight away.
        _start(client, headers, request_id="stop-2", thread_id=job["thread_id"], user_input="go on")


def test_stop_before_anything_was_shown_keeps_nothing():
    shown = Event()
    app = _app_with({"show_text": False}, shown)
    with TestClient(app) as client:
        headers = _session(client, app)
        job = _start(client, headers, request_id="stop-early", user_input="explain it")
        assert shown.wait(WAIT_SECONDS)

        events = _stop_and_drain(client, headers, job["job_id"])

        chat = client.get(f"/api/v1/chats/{job['thread_id']}", headers=headers).json()
        assert [message["role"] for message in chat["messages"]] == ["user"]
        assert "assistant_message_id" not in events[-1]["data"]


def test_text_written_after_stop_is_not_kept():
    """Output that reaches the sink after Stop is dropped from the stream, so
    no client saw it; keeping it would save words the user never read."""
    shown = Event()
    app = _app_with({"after_stop": NEVER_SHOWN}, shown)
    with TestClient(app) as client:
        headers = _session(client, app)
        job = _start(client, headers, request_id="stop-late", user_input="explain it")
        assert shown.wait(WAIT_SECONDS)

        _stop_and_drain(client, headers, job["job_id"])

        chat = client.get(f"/api/v1/chats/{job['thread_id']}", headers=headers).json()
        assert chat["messages"][-1]["content"] == SHOWN


def test_stopping_a_regeneration_keeps_the_original_answer():
    """A regeneration replaces an answer only when it finishes; a stopped
    attempt leaves the original in place, as any unfinished attempt does."""
    dependencies = build_demo_dependencies()
    inner_factory = dependencies.generation._engine_factory
    stoppable = {"on": False}
    shown = Event()

    def factory(snapshot):
        inner = inner_factory(snapshot)
        return _StoppableEngine(inner, shown=shown) if stoppable["on"] else inner

    dependencies.generation._engine_factory = factory
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = _session(client, app)
        first = _start(client, headers, request_id="regen-base", user_input="explain it")
        with client.stream("GET", f"/api/v1/generations/{first['job_id']}/events", headers=headers) as response:
            "".join(response.iter_text())
        original = client.get(f"/api/v1/chats/{first['thread_id']}", headers=headers).json()["messages"][-1]

        stoppable["on"] = True
        regeneration = client.post(
            f"/api/v1/chats/{first['thread_id']}/regenerations",
            json={"request_id": "regen-stop", "message_id": original["id"]},
            headers=headers,
        )
        assert regeneration.status_code == 202
        assert shown.wait(WAIT_SECONDS)
        _stop_and_drain(client, headers, regeneration.json()["job_id"])

        after = client.get(f"/api/v1/chats/{first['thread_id']}", headers=headers).json()
        assert after["messages"][-1]["id"] == original["id"]
        assert after["messages"][-1]["content"] == original["content"]
        assert not (after["messages"][-1].get("stats") or {}).get("stopped")
