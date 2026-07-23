"""Qualification tests for the bounded fixed recipe worker contract."""

from __future__ import annotations

import base64
from hashlib import sha256
from io import BytesIO
from threading import Event, Thread

import pytest
from PIL import Image

from cortex_backend.execution.lifecycle import RuntimeHealth
from cortex_backend.execution.recipe_provider import RecipeImageProvider, RecipeProviderResult
from cortex_backend.execution.recipes import parse_image_transform
from cortex_backend.execution.worker_protocol import (
    RecipeWorkerDispatcher,
    RecipeWorkerSession,
    WorkerCancel,
    WorkerCollect,
    WorkerInputChunk,
    WorkerInputComplete,
    WorkerPrepare,
    WorkerProtocolError,
)


def _image_bytes() -> bytes:
    image = Image.new("RGB", (4, 3), (120, 80, 40))
    try:
        with BytesIO() as stream:
            image.save(stream, format="PNG")
            return stream.getvalue()
    finally:
        image.close()


def _plan():
    return parse_image_transform(
        {
            "schema_version": "artifact.transform.v1",
            "input_artifact_id": "artifact-1",
            "steps": [{"op": "grayscale"}],
            "output_format": "png",
        }
    )


def _provider() -> RecipeImageProvider:
    provider = RecipeImageProvider()
    assert provider.start(RuntimeHealth.ready("worker protocol test"))
    return provider


def _prepare(content: bytes) -> WorkerPrepare:
    return WorkerPrepare(
        schema_version="recipe.worker.prepare.v1",
        request_id="request-1",
        job_id="job-1",
        plan=_plan(),
        input_size=len(content),
        input_sha256=sha256(content).hexdigest(),
        input_mime_type="image/png",
    )


def _chunk(content: bytes, *, offset: int = 0) -> WorkerInputChunk:
    encoded = base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")
    return WorkerInputChunk(
        schema_version="recipe.worker.input_chunk.v1",
        request_id="request-1",
        job_id="job-1",
        offset=offset,
        data=encoded,
        sha256=sha256(content).hexdigest(),
    )


def test_worker_session_requires_in_order_hashed_input_and_collects_output():
    content = _image_bytes()
    session = RecipeWorkerSession(_provider())
    prepare = _prepare(content)
    session.prepare(prepare)
    session.input_chunk(_chunk(content[:8]))
    session.input_chunk(_chunk(content[8:], offset=8))

    result = session.complete_input(
        WorkerInputComplete(
            schema_version="recipe.worker.input_complete.v1",
            request_id="request-1",
            job_id="job-1",
            input_size=len(content),
            input_sha256=sha256(content).hexdigest(),
        )
    )

    assert session.state == "complete"
    assert result.output_size > 0
    output = session.collect(
        WorkerCollect(
            schema_version="recipe.worker.collect.v1",
            request_id="request-1",
            job_id="job-1",
            offset=0,
            max_bytes=1024,
        )
    )
    assert output.decoded()
    assert output.final


def test_dispatcher_rejects_unknown_or_malformed_operations():
    dispatcher = RecipeWorkerDispatcher(RecipeWorkerSession(_provider()))
    with pytest.raises(WorkerProtocolError) as unknown:
        dispatcher.dispatch("shell", {})
    assert unknown.value.code == "operation_invalid"

    with pytest.raises(WorkerProtocolError) as malformed:
        dispatcher.dispatch("prepare", {"schema_version": "recipe.worker.prepare.v1"})
    assert malformed.value.code == "message_invalid"


def test_worker_session_rejects_replay_order_and_claim_mismatch():
    content = _image_bytes()
    session = RecipeWorkerSession(_provider())
    session.prepare(_prepare(content))
    with pytest.raises(WorkerProtocolError) as order:
        session.input_chunk(_chunk(content[:4], offset=1))
    assert order.value.code == "chunk_out_of_order"

    with pytest.raises(WorkerProtocolError) as claim:
        session.complete_input(
            WorkerInputComplete(
                schema_version="recipe.worker.input_complete.v1",
                request_id="request-1",
                job_id="job-1",
                input_size=len(content),
                input_sha256="0" * 64,
            )
        )
    assert claim.value.code == "input_claim_mismatch"


def test_worker_session_cancellation_is_terminal_and_redacted():
    content = _image_bytes()
    session = RecipeWorkerSession(_provider())
    session.prepare(_prepare(content))
    session.cancel(
        WorkerCancel(
            schema_version="recipe.worker.cancel.v1",
            request_id="request-1",
            job_id="job-1",
            reason="user",
        )
    )
    assert session.state == "cancelled"
    with pytest.raises(WorkerProtocolError) as after:
        session.complete_input(
            WorkerInputComplete(
                schema_version="recipe.worker.input_complete.v1",
                request_id="request-1",
                job_id="job-1",
                input_size=len(content),
                input_sha256=sha256(content).hexdigest(),
            )
        )
    assert after.value.code == "request_state_invalid"


def test_cancellation_wins_if_requested_while_provider_is_running():
    content = _image_bytes()
    entered = Event()
    release = Event()

    class _SlowProvider:
        def transform(self, plan, payload, *, cancel_check):
            del plan, payload, cancel_check
            entered.set()
            release.wait(timeout=5)
            return RecipeProviderResult(
                content=b"output",
                mime_type="image/png",
                width=1,
                height=1,
                format="PNG",
                sha256=sha256(b"output").hexdigest(),
            )

    session = RecipeWorkerSession(_SlowProvider())
    session.prepare(_prepare(content))
    session.input_chunk(_chunk(content))
    complete_error: list[WorkerProtocolError] = []

    def complete() -> None:
        try:
            session.complete_input(
                WorkerInputComplete(
                    schema_version="recipe.worker.input_complete.v1",
                    request_id="request-1",
                    job_id="job-1",
                    input_size=len(content),
                    input_sha256=sha256(content).hexdigest(),
                )
            )
        except WorkerProtocolError as error:
            complete_error.append(error)

    thread = Thread(target=complete)
    thread.start()
    assert entered.wait(timeout=5)
    session.cancel(
        WorkerCancel(
            schema_version="recipe.worker.cancel.v1",
            request_id="request-1",
            job_id="job-1",
            reason="user",
        )
    )
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert [error.code for error in complete_error] == ["cancelled"]
    assert session.state == "cancelled"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: {**payload, "sha256": "0" * 64},
        lambda payload: {**payload, "data": "%%%"},
    ],
)
def test_worker_input_chunk_rejects_tampering(mutator):
    content = b"worker input"
    payload = {
        "schema_version": "recipe.worker.input_chunk.v1",
        "request_id": "request-1",
        "job_id": "job-1",
        "offset": 0,
        "data": base64.urlsafe_b64encode(content).decode("ascii").rstrip("="),
        "sha256": sha256(content).hexdigest(),
    }
    message = WorkerInputChunk.model_validate(mutator(payload))
    with pytest.raises(WorkerProtocolError):
        message.decoded()
