"""Untrusted input must fail with a real status code, not a 500 or an OOM.

Each of these was reachable with a single ordinary request, and each failed in
a way that told the caller nothing useful -- or, in the resize case, spent two
gigabytes before failing.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from cortex_backend.api import create_app
from cortex_backend.api.app import MAX_REQUEST_BODY_BYTES, RequestBodyLimitMiddleware
from cortex_backend.execution.recipes import (
    MAX_PIXELS,
    RecipeValidationError,
    parse_image_transform,
)
from cortex_backend.execution.repository import (
    ExecutionRepository,
    ExecutionRepositoryError,
)
from cortex_backend.testing import (
    DurableFakeCoordinator,
    build_demo_dependencies,
    install_execution_preview,
)
from cortex_backend.services.attachments import (
    MAX_CHAT_ATTACHMENT_BYTES,
    ChatAttachmentService,
)
from cortex_backend.testing.fake_ollama import FakeOllamaState
from support import session_headers


def _plan(steps: list[dict]) -> dict:
    return {
        "schema_version": "artifact.transform.v1",
        "input_artifact_id": "artifact-1",
        "steps": steps,
        "output_format": "png",
    }


def test_a_resize_bounded_per_side_is_still_bounded_by_area() -> None:
    """16384 x 16384 passed both per-side limits and asked for ~2.2 GB.

    A few hundred bytes of JSON made a worker allocate until it died, and the
    request is trivially repeatable.
    """
    with pytest.raises(RecipeValidationError):
        parse_image_transform(_plan([{"op": "resize", "width": 16384, "height": 16384}]))

    # A plan inside the budget still parses.
    ok = parse_image_transform(_plan([{"op": "resize", "width": 1024, "height": 1024}]))
    assert ok.steps[0].width == 1024


def test_a_crop_region_is_bounded_by_area_too() -> None:
    with pytest.raises(RecipeValidationError):
        parse_image_transform(
            _plan([{"op": "crop", "x": 0, "y": 0, "width": 16384, "height": 16384}])
        )


def test_the_pixel_budget_matches_the_provider_that_enforces_it() -> None:
    """Parse-time and run-time ceilings must not drift apart."""
    from cortex_backend.execution.recipe_provider import MAX_PIXELS as PROVIDER_MAX

    assert MAX_PIXELS == PROVIDER_MAX


@pytest.mark.parametrize(
    ("raw_host", "expected"),
    [
        ("[::1", ""),
        ("[", ""),
        ("[:evil.com", ""),
        # urlsplit raises on this from 3.12, but returns "::1" on 3.10 and
        # 3.11 -- including the 3.11 this ships on -- so trailing junk after a
        # bracketed literal used to pass the host allowlist. CI caught the
        # difference; the parser no longer depends on the interpreter version.
        ("[::1]evil.com", ""),
        ("[::1]", "::1"),
        ("[::1]:8080", "::1"),
        ("127.0.0.1", "127.0.0.1"),
        ("localhost:5173", "localhost"),
    ],
)
def test_the_host_parser_agrees_on_every_python_version(raw_host: str, expected: str) -> None:
    from cortex_backend.api.security import _parse_host_header

    assert _parse_host_header(raw_host) == expected


@pytest.mark.parametrize("raw_host", ["[::1", "[", "[:evil.com", "[::1]evil.com"])
def test_a_malformed_host_header_is_a_400_not_a_500(app_factory_client, raw_host: str) -> None:
    """urlsplit raises on an unbalanced bracket.

    This check runs before any credential is examined, on every route, so an
    uncaught error here was an unauthenticated 500 with a traceback in the log.
    """
    response = app_factory_client.get("/api/v1/health/live", headers={"Host": raw_host})

    assert response.status_code == 400, (
        f"Host: {raw_host!r} produced {response.status_code}"
    )


def test_an_out_of_range_last_event_id_is_refused_before_the_stream_opens(app_factory_client) -> None:
    """SQLite cannot bind above 2**63-1.

    The OverflowError landed inside the streaming generator, after the 200 and
    its headers were already sent, so the client saw a successful response
    with a truncated body that never terminated.
    """
    accepted = app_factory_client.post(
        "/api/v1/execution/scratch",
        json={"request_id": "overflow-1", "expression": "1 + 1"},
    )
    job_id = accepted.json()["job_id"]

    response = app_factory_client.get(
        f"/api/v1/execution/{job_id}/events",
        headers={"Last-Event-ID": str(2**63)},
    )

    assert response.status_code == 400


def test_an_oversized_memory_is_refused_the_same_way_by_put_and_post(app_factory_client) -> None:
    """POST bounded each item; its PUT sibling did not, so PUT answered 500."""
    assert app_factory_client.post("/api/v1/memories", json={"memo": "x" * 501}).status_code == 422
    assert app_factory_client.put("/api/v1/memories", json={"memos": ["x" * 501]}).status_code == 422


def test_a_full_memory_store_is_a_conflict_not_a_server_fault(app_factory_client) -> None:
    """Reaching the limit is an expected outcome the user can act on."""
    response = None
    for index in range(101):
        response = app_factory_client.post("/api/v1/memories", json={"memo": f"memory {index}"})

    assert response is not None
    assert response.status_code == 409
    assert "100" in response.json()["detail"]


@dataclass(frozen=True)
class _Seeded:
    """A chat holding one exchange, and what a scenario needs to act on it."""

    client: TestClient
    headers: dict[str, str]
    ollama_state: FakeOllamaState
    thread_id: str
    user_message_id: str
    assistant_message_id: str


def _chats(client: TestClient):
    """The chat repository behind ``client``'s app, for seeding a transcript.

    There is no route that writes a raw turn -- a client can only generate one --
    so a scenario that needs an assistant reply or a system note already in a
    chat puts it there the way the generation worker does.
    """
    return client.app.state.dependencies.chats


def _seed_chat(
    client: TestClient, headers: dict[str, str], ollama_state: FakeOllamaState
) -> _Seeded:
    chat = client.post("/api/v1/chats", json={"title": "Status codes"}, headers=headers).json()
    user_id = _chats(client).add_message(chat["id"], "user", "hello")
    assistant_id = _chats(client).add_message(chat["id"], "assistant", "hi there")
    return _Seeded(client, headers, ollama_state, chat["id"], user_id, assistant_id)


def _regenerate(seeded: _Seeded, message_id: str, **extra: object) -> httpx.Response:
    return seeded.client.post(
        f"/api/v1/chats/{seeded.thread_id}/regenerations",
        json={"request_id": "status-regenerate", "message_id": message_id, **extra},
        headers=seeded.headers,
    )


def _fork_an_unknown_message(seeded: _Seeded) -> httpx.Response:
    return seeded.client.post(
        f"/api/v1/chats/{seeded.thread_id}/forks",
        json={"message_id": "no-such-message"},
        headers=seeded.headers,
    )


def _regenerate_an_unknown_message(seeded: _Seeded) -> httpx.Response:
    return _regenerate(seeded, "no-such-message")


def _regenerate_a_message_that_is_no_longer_last(seeded: _Seeded) -> httpx.Response:
    return _regenerate(seeded, seeded.user_message_id)


def _regenerate_a_message_that_is_not_a_reply(seeded: _Seeded) -> httpx.Response:
    note_id = _chats(seeded.client).add_message(
        seeded.thread_id, "system", "a note, not a reply"
    )
    return _regenerate(seeded, note_id)


def _regenerate_from_a_stale_revision(seeded: _Seeded) -> httpx.Response:
    return _regenerate(seeded, seeded.assistant_message_id, base_revision=0)


def _generate_from_a_stale_revision(seeded: _Seeded) -> httpx.Response:
    return seeded.client.post(
        "/api/v1/generations",
        json={"thread_id": seeded.thread_id, "user_input": "again", "base_revision": 0},
        headers=seeded.headers,
    )


def _generate_with_no_model_installed(seeded: _Seeded) -> httpx.Response:
    # The inventory is read for every turn, so emptying the fake Ollama between
    # requests is exactly what "nothing installed" looks like to the route.
    seeded.ollama_state.installed_models.clear()
    return seeded.client.post(
        "/api/v1/generations",
        json={"user_input": "anyone there?"},
        headers=seeded.headers,
    )


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        pytest.param(_fork_an_unknown_message, 404, id="fork-unknown-message-is-not-found"),
        pytest.param(_regenerate_an_unknown_message, 404, id="regenerate-unknown-message-is-not-found"),
        pytest.param(_regenerate_a_message_that_is_no_longer_last, 409, id="regenerate-an-older-message-is-a-conflict"),
        pytest.param(_regenerate_a_message_that_is_not_a_reply, 422, id="regenerate-a-system-message-is-invalid-input"),
        pytest.param(_regenerate_from_a_stale_revision, 409, id="regenerate-from-a-stale-revision-is-a-conflict"),
        pytest.param(_generate_from_a_stale_revision, 409, id="generate-from-a-stale-revision-is-a-conflict"),
        pytest.param(_generate_with_no_model_installed, 503, id="no-installed-model-is-unavailable"),
    ],
)
def test_status_codes_follow_error_meaning(
    client: TestClient,
    headers: dict[str, str],
    ollama_state: FakeOllamaState,
    scenario: Callable[[_Seeded], httpx.Response],
    expected_status: int,
) -> None:
    """A client retries, reloads, or tells the user to install a model from the
    status alone. Each of these was a 409 because it was caught beside a real
    conflict, whatever had actually gone wrong."""
    seeded = _seed_chat(client, headers, ollama_state)

    response = scenario(seeded)

    assert response.status_code == expected_status, response.text
    assert isinstance(response.json()["detail"], str)


def _execution_app(tmp_path: Path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=("testserver",),
        preview=True,
        execution_coordinator=DurableFakeCoordinator(repository),
    )
    return install_execution_preview(app)


def _fail_with_a_disk_error(*_args: object, **_kwargs: object) -> object:
    raise ExecutionRepositoryError("disk")


def test_a_repository_failure_is_a_500_not_a_missing_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole ExecutionRepositoryError family answered 404 "job not found",
    a disk failure or a lost lease included -- sending the user hunting for a
    job that exists."""
    app = _execution_app(tmp_path)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        accepted = client.post(
            "/api/v1/execution/preview/fake",
            json={"request_id": "status-disk-failure"},
            headers=headers,
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        coordinator = app.state.execution_coordinator
        monkeypatch.setattr(coordinator.repository, "decide_approval", _fail_with_a_disk_error)
        monkeypatch.setattr(coordinator, "cancel", _fail_with_a_disk_error)

        decision = client.post(
            f"/api/v1/execution/{job_id}/approval",
            json={"decision": "approved"},
            headers=headers,
        )
        cancel = client.post(f"/api/v1/execution/{job_id}/cancel", headers=headers)

    assert decision.status_code == 500, decision.text
    assert cancel.status_code == 500, cancel.text
    assert "disk" not in decision.text + cancel.text


def test_a_job_that_does_not_exist_is_still_a_404_for_approval_and_cancel(tmp_path: Path) -> None:
    app = _execution_app(tmp_path)
    with TestClient(app) as client:
        headers = session_headers(client, app)

        decision = client.post(
            "/api/v1/execution/no-such-job/approval",
            json={"decision": "approved"},
            headers=headers,
        )
        cancel = client.post("/api/v1/execution/no-such-job/cancel", headers=headers)

    assert decision.status_code == 404
    assert cancel.status_code == 404


@pytest.mark.parametrize("blank", ["   ", "\t\n", "​", ""])
def test_a_blank_model_pull_is_a_422_not_a_failed_job(
    client: TestClient,
    headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
    blank: str,
) -> None:
    """It used to be accepted, then fail inside the job with a generic "Job
    failed", after taking the models job slot."""
    response = client.post("/api/v1/models/pulls", json={"model": blank}, headers=headers)

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "model"]
    assert "job failed" not in caplog.text.lower()


def test_a_padded_model_name_is_pulled_trimmed(
    client: TestClient, headers: dict[str, str]
) -> None:
    accepted = client.post(
        "/api/v1/models/pulls", json={"model": "  tiny-model:latest  "}, headers=headers
    )
    assert accepted.status_code == 202, accepted.text

    deadline = time.monotonic() + 10.0
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/jobs/{accepted.json()['job_id']}", headers=headers).json()
        if body["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.01)

    assert body["status"] == "succeeded", body
    assert body["result"]["model"] == "tiny-model:latest"


def _assert_issues_carry_only_where_and_why(response: httpx.Response) -> list[dict]:
    """The shape the SPA reads (``loc`` and ``msg``), and nothing that could
    carry what the caller sent (``input``, ``ctx``, ``url``)."""
    assert response.status_code == 422, response.text
    issues = response.json()["detail"]
    assert issues
    for issue in issues:
        assert set(issue) == {"loc", "msg", "type"}, issue
        assert issue["loc"] and isinstance(issue["msg"], str) and issue["msg"]
    return issues


def test_validation_errors_do_not_echo_the_offending_input(
    client: TestClient, headers: dict[str, str]
) -> None:
    """An invalid PUT /settings echoed the whole settings object, system
    instructions included, and an over-long attachment echoed its entire base64
    body. Prompts are not meant to reach anything a proxy or a log can keep."""
    prompt = "PRIVATE-PROMPT-" + "x" * 600

    memory = client.post("/api/v1/memories", json={"memo": prompt}, headers=headers)
    memory_issues = _assert_issues_carry_only_where_and_why(memory)
    assert memory_issues[0]["loc"] == ["body", "memo"]
    assert "PRIVATE-PROMPT" not in memory.text

    current = client.get("/api/v1/settings", headers=headers).json()["settings"]
    current["generation"]["system_instructions"] = prompt
    current["generation"]["num_ctx"] = 1  # invalid: below the minimum
    settings = client.put("/api/v1/settings", json={"settings": current}, headers=headers)
    settings_issues = _assert_issues_carry_only_where_and_why(settings)
    assert ["body", "settings", "generation", "num_ctx"] in [
        issue["loc"] for issue in settings_issues
    ]
    assert "PRIVATE-PROMPT" not in settings.text

    oversized = "Q" * ((MAX_CHAT_ATTACHMENT_BYTES * 4) // 3 + 1024)
    attachment = client.post(
        "/api/v1/attachments",
        json={"request_id": "echo-1", "filename": "notes.txt", "content_base64": oversized},
        headers=headers,
    )
    _assert_issues_carry_only_where_and_why(attachment)
    assert "QQQQQQQQ" not in attachment.text
    assert len(attachment.content) < 2_000


def test_a_validation_report_is_bounded_too(client: TestClient, headers: dict[str, str]) -> None:
    """The location is a field name, and the caller chose that as well."""
    response = client.post(
        "/api/v1/memories",
        json={"memo": "fine", "K" * 5_000: "unknown field"},
        headers=headers,
    )

    issues = _assert_issues_carry_only_where_and_why(response)
    assert all(len(str(part)) <= 303 for issue in issues for part in issue["loc"])
    assert len(response.content) < 2_000


def test_invalid_json_is_reported_without_the_body(
    client: TestClient, headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/memories",
        content=b'{"memo": "BODY-MARKER", oops',
        headers={**headers, "Content-Type": "application/json"},
    )

    _assert_issues_carry_only_where_and_why(response)
    assert "BODY-MARKER" not in response.text


class _RecordingAttachments(ChatAttachmentService):
    """The real attachment service, plus a record of every attempt to stage."""

    def __init__(self) -> None:
        super().__init__()
        self.staged: list[str] = []

    def stage(self, **kwargs: Any):
        self.staged.append(kwargs["request_id"])
        return super().stage(**kwargs)


def _app_with_recording_attachments() -> tuple[Any, _RecordingAttachments]:
    dependencies = build_demo_dependencies()
    recorder = _RecordingAttachments()
    dependencies.attachments = recorder
    return create_app(dependencies, allowed_hosts=("testserver",)), recorder


def _stage_request(content_base64: str) -> dict[str, str]:
    return {"request_id": "limit-1", "filename": "notes.txt", "content_base64": content_base64}


def test_an_oversized_body_is_a_413_before_the_route_runs() -> None:
    """Field limits only apply once the body has been buffered and decoded, so
    without a ceiling a single request could make the backend hold as much as
    a client cared to send."""
    app, attachments = _app_with_recording_attachments()
    with TestClient(app) as client:
        headers = session_headers(client, app)

        response = client.post(
            "/api/v1/attachments",
            content=b"x" * (17 * 1024 * 1024),
            headers={**headers, "Content-Type": "application/json"},
        )

    assert response.status_code == 413, response.text[:200]
    assert response.json() == {"detail": "Request body is too large."}
    assert response.headers["connection"] == "close"
    assert attachments.staged == []


def test_an_oversized_body_without_a_length_is_cut_off_too() -> None:
    """A chunked upload declares no length, so only the running total can stop it."""
    app, attachments = _app_with_recording_attachments()
    chunk = b"x" * (1024 * 1024)
    with TestClient(app) as client:
        headers = session_headers(client, app)

        response = client.post(
            "/api/v1/attachments",
            content=(chunk for _ in range(17)),
            headers={**headers, "Content-Type": "application/json"},
        )

    assert response.status_code == 413, response.text[:200]
    assert attachments.staged == []


def test_a_refusal_carries_the_cors_headers_a_browser_needs_to_read_it() -> None:
    app, _ = _app_with_recording_attachments()
    with TestClient(app) as client:
        headers = session_headers(client, app)

        response = client.post(
            "/api/v1/attachments",
            content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
            headers={
                **headers,
                "Content-Type": "application/json",
                "Origin": "http://localhost:5173",
            },
        )

    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_the_largest_legitimate_attachment_is_still_accepted() -> None:
    """The ceiling has to sit above the biggest body the API is meant to take:
    a full-size file, base64-encoded."""
    app, attachments = _app_with_recording_attachments()
    encoded = base64.b64encode(b"a" * MAX_CHAT_ATTACHMENT_BYTES).decode("ascii")
    assert len(encoded) < MAX_REQUEST_BODY_BYTES
    with TestClient(app) as client:
        headers = session_headers(client, app)

        response = client.post(
            "/api/v1/attachments", json=_stage_request(encoded), headers=headers
        )

    assert response.status_code == 201, response.text[:200]
    assert attachments.staged == ["limit-1"]


class _Downstream:
    """An ASGI app that reads its body and answers, noting what it saw."""

    def __init__(self) -> None:
        self.called = False
        self.received = 0
        self.saw_disconnect = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.called = True
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self.saw_disconnect = True
                break
            self.received += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _drive(
    app: Any,
    *,
    chunks: list[bytes],
    path: str = "/api/v1/echo",
    content_length: bytes | None = None,
    scope_type: str = "http",
) -> list[dict[str, Any]]:
    """Run ``app`` once over a scripted request; nothing here can wait forever."""
    sent: list[dict[str, Any]] = []
    script = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]

    async def receive() -> dict[str, Any]:
        return script.pop(0) if script else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    headers = [(b"content-length", content_length)] if content_length is not None else []
    scope = {"type": scope_type, "path": path, "headers": headers}
    asyncio.run(app(scope, receive, send))
    return sent


def _statuses(sent: list[dict[str, Any]]) -> list[int]:
    return [message["status"] for message in sent if message["type"] == "http.response.start"]


def test_a_declared_length_over_the_ceiling_never_reaches_the_app() -> None:
    downstream = _Downstream()
    limited = RequestBodyLimitMiddleware(downstream, max_body_bytes=10)

    sent = _drive(limited, chunks=[b"x" * 11], content_length=b"11")

    assert _statuses(sent) == [413]
    assert downstream.called is False


def test_a_body_at_the_ceiling_passes_and_one_byte_more_does_not() -> None:
    at_ceiling = _Downstream()
    over = _Downstream()

    passed = _drive(
        RequestBodyLimitMiddleware(at_ceiling, max_body_bytes=10),
        chunks=[b"x" * 5, b"x" * 5],
        content_length=b"10",
    )
    refused = _drive(
        RequestBodyLimitMiddleware(over, max_body_bytes=10),
        chunks=[b"x" * 5, b"x" * 6],
    )

    assert _statuses(passed) == [200] and at_ceiling.received == 10
    assert _statuses(refused) == [413] and over.saw_disconnect is True


def test_a_body_that_understates_its_length_is_cut_off_at_the_ceiling() -> None:
    downstream = _Downstream()
    limited = RequestBodyLimitMiddleware(downstream, max_body_bytes=10)

    sent = _drive(limited, chunks=[b"x" * 6, b"x" * 6], content_length=b"5")

    assert _statuses(sent) == [413]
    assert downstream.saw_disconnect is True


def test_only_one_response_is_sent_when_the_body_is_cut_off() -> None:
    """The app sees a disconnect and answers it; that answer must not follow
    the 413 onto the wire."""
    limited = RequestBodyLimitMiddleware(_Downstream(), max_body_bytes=10)

    sent = _drive(limited, chunks=[b"x" * 6, b"x" * 6, b"x" * 6])

    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]
    assert _statuses(sent) == [413]


def test_paths_outside_the_api_and_other_protocols_are_not_limited() -> None:
    static = _Downstream()
    lifespan = _Downstream()

    outside = _drive(
        RequestBodyLimitMiddleware(static, max_body_bytes=10),
        chunks=[b"x" * 11],
        path="/assets/app.js",
        content_length=b"11",
    )
    other_protocol = _drive(
        RequestBodyLimitMiddleware(lifespan, max_body_bytes=10),
        chunks=[b"x" * 11],
        scope_type="websocket",
    )

    assert _statuses(outside) == [200] and static.received == 11
    assert _statuses(other_protocol) == [200] and lifespan.received == 11


def test_a_malformed_length_header_falls_back_to_counting_the_bytes() -> None:
    downstream = _Downstream()
    limited = RequestBodyLimitMiddleware(downstream, max_body_bytes=10)

    fine = _drive(limited, chunks=[b"x" * 4], content_length=b"not-a-number")
    too_much = _drive(limited, chunks=[b"x" * 11], content_length=b"-3")

    assert _statuses(fine) == [200]
    assert _statuses(too_much) == [413]
