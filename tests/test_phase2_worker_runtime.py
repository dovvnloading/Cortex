"""Authenticated worker-loop contract and hostile recovery tests."""

from __future__ import annotations

import base64
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from queue import Queue
import runpy
from threading import Event
import time
from typing import Any

import pytest
from PIL import Image

from cortex_backend.execution.broker import BrokerMessage
from cortex_backend.execution.lifecycle import RuntimeHealth
from cortex_backend.execution.recipe_provider import RecipeImageProvider, RecipeProviderError
from cortex_backend.execution.recipes import parse_image_transform
from cortex_backend.execution.worker_protocol import (
    WorkerCancel,
    WorkerCollect,
    WorkerInputChunk,
    WorkerInputComplete,
    WorkerPrepare,
)
from cortex_backend.execution.worker_runtime import (
    RecipeWorkerBrokerRuntime,
    WorkerRuntimeError,
)


PRINCIPAL = "a" * 64
JOB_ID = "job_1"


class _FakeConnection:
    def __init__(self, incoming: list[BrokerMessage]) -> None:
        self.incoming: Queue[BrokerMessage | None] = Queue()
        for message in incoming:
            self.incoming.put(message)
        self.sent: list[BrokerMessage] = []
        self.closed = False

    def receive_message(self) -> BrokerMessage:
        message = self.incoming.get()
        if message is None:
            raise AssertionError("runtime read past the expected terminal response")
        return message

    def send_message(self, message: BrokerMessage) -> None:
        self.sent.append(message)

    def close(self) -> None:
        self.closed = True
        self.incoming.put(None)


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


def _prepare(content: bytes, *, request_id: str = "request_1") -> WorkerPrepare:
    return WorkerPrepare(
        schema_version="recipe.worker.prepare.v1",
        request_id=request_id,
        job_id=JOB_ID,
        plan=_plan(),
        input_size=len(content),
        input_sha256=sha256(content).hexdigest(),
        input_mime_type="image/png",
    )


def _chunk(content: bytes, *, offset: int, request_id: str = "request_1") -> WorkerInputChunk:
    return WorkerInputChunk(
        schema_version="recipe.worker.input_chunk.v1",
        request_id=request_id,
        job_id=JOB_ID,
        offset=offset,
        data=base64.urlsafe_b64encode(content).decode("ascii").rstrip("="),
        sha256=sha256(content).hexdigest(),
    )


def _message(operation: str, model: Any, *, request_id: str = "request_1") -> BrokerMessage:
    return BrokerMessage(
        schema_version="broker.message.v1",
        direction="to_executor",
        operation=operation,
        request_id=request_id,
        job_id=JOB_ID,
        installation_principal_id=PRINCIPAL,
        body=model.model_dump(mode="json"),
    )


def _successful_messages(content: bytes) -> list[BrokerMessage]:
    prepare = _prepare(content)
    complete = WorkerInputComplete(
        schema_version="recipe.worker.input_complete.v1",
        request_id="request_1",
        job_id=JOB_ID,
        input_size=len(content),
        input_sha256=sha256(content).hexdigest(),
    )
    collect = WorkerCollect(
        schema_version="recipe.worker.collect.v1",
        request_id="request_1",
        job_id=JOB_ID,
        offset=0,
        max_bytes=48 * 1024,
    )
    return [
        _message("prepare", prepare),
        _message("input_chunk", _chunk(content, offset=0)),
        _message("input_complete", complete),
        _message("collect", collect),
    ]


def test_runtime_processes_authenticated_request_and_closes_provider_and_transport():
    connection = _FakeConnection(_successful_messages(_image_bytes()))
    providers: list[RecipeImageProvider] = []

    def provider_factory() -> RecipeImageProvider:
        provider = RecipeImageProvider()
        providers.append(provider)
        return provider

    report = RecipeWorkerBrokerRuntime(
        connection,
        expected_principal_id=PRINCIPAL,
        job_id=JOB_ID,
        provider_factory=provider_factory,
    ).run()

    assert report.terminal_state == "complete"
    assert report.processed_messages == 4
    assert connection.closed
    assert providers[0].health_snapshot.code == "recipe_provider_stopped"
    assert [message.operation for message in connection.sent] == [
        "prepare",
        "input_chunk",
        "input_complete",
        "collect",
    ]
    assert connection.sent[0].body["schema_version"] == "recipe.worker.ack.v1"
    assert connection.sent[2].body["schema_version"] == "recipe.worker.result.v1"
    assert connection.sent[3].body["schema_version"] == "recipe.worker.output_chunk.v1"
    assert connection.sent[3].body["final"] is True


def test_runtime_redacts_malformed_body_and_allows_bounded_repair():
    content = _image_bytes()
    malformed = BrokerMessage(
        schema_version="broker.message.v1",
        direction="to_executor",
        operation="prepare",
        request_id="request_1",
        job_id=JOB_ID,
        installation_principal_id=PRINCIPAL,
        body={"schema_version": "recipe.worker.prepare.v1"},
    )
    connection = _FakeConnection([malformed, *_successful_messages(content)])

    report = RecipeWorkerBrokerRuntime(
        connection,
        expected_principal_id=PRINCIPAL,
        job_id=JOB_ID,
    ).run()

    assert report.terminal_state == "complete"
    assert connection.sent[0].body == {
        "schema_version": "recipe.worker.error.v1",
        "request_id": "request_1",
        "job_id": JOB_ID,
        "code": "message_invalid",
    }
    assert "path" not in str(connection.sent[0].body)


def test_runtime_cancellation_is_terminal_and_acknowledged():
    content = _image_bytes()
    prepare = _prepare(content)
    cancel = WorkerCancel(
        schema_version="recipe.worker.cancel.v1",
        request_id="request_1",
        job_id=JOB_ID,
        reason="user",
    )
    connection = _FakeConnection([_message("prepare", prepare), _message("cancel", cancel)])

    report = RecipeWorkerBrokerRuntime(
        connection,
        expected_principal_id=PRINCIPAL,
        job_id=JOB_ID,
    ).run()

    assert report.terminal_state == "cancelled"
    assert connection.sent[-1].body["schema_version"] == "recipe.worker.ack.v1"
    assert connection.sent[-1].body["acknowledged_operation"] == "cancel"
    assert connection.closed


class _CancellableProvider:
    def __init__(self) -> None:
        self.entered = Event()
        self.stopped = False
        self.on_entered: Any = None

    def start(self, _health: RuntimeHealth) -> RuntimeHealth:
        return RuntimeHealth.ready("test")

    def stop(self) -> RuntimeHealth:
        self.stopped = True
        return RuntimeHealth.blocked("test_stopped", "test provider stopped")

    def transform(self, _plan: Any, _content: bytes, *, cancel_check: Any) -> Any:
        self.entered.set()
        if self.on_entered is not None:
            self.on_entered()
        while not cancel_check():
            time.sleep(0.001)
        raise RecipeProviderError("cancelled")


def test_runtime_cancellation_reaches_provider_during_transform():
    content = _image_bytes()
    provider = _CancellableProvider()
    messages = _successful_messages(content)[:3]
    connection = _FakeConnection(messages)
    provider.on_entered = lambda: connection.incoming.put(
        _message(
            "cancel",
            WorkerCancel(
                schema_version="recipe.worker.cancel.v1",
                request_id="request_1",
                job_id=JOB_ID,
                reason="user",
            ),
        )
    )

    report = RecipeWorkerBrokerRuntime(
        connection,
        expected_principal_id=PRINCIPAL,
        job_id=JOB_ID,
        provider_factory=lambda: provider,
    ).run()

    assert provider.entered.is_set()
    assert provider.stopped
    assert report.terminal_state == "cancelled"
    assert connection.sent[-1].body["schema_version"] == "recipe.worker.ack.v1"


def test_runtime_rejects_job_confusion_before_dispatch_and_closes():
    content = _image_bytes()
    message = _message("prepare", _prepare(content))
    message = message.model_copy(update={"job_id": "other_job"})
    connection = _FakeConnection([message])

    with pytest.raises(WorkerRuntimeError) as error:
        RecipeWorkerBrokerRuntime(
            connection,
            expected_principal_id=PRINCIPAL,
            job_id=JOB_ID,
        ).run()
    assert error.value.code == "broker_job_mismatch"
    assert connection.sent == []
    assert connection.closed


class _FailingProvider:
    def __init__(self) -> None:
        self.stopped = False

    def start(self, _health: RuntimeHealth) -> RuntimeHealth:
        return RuntimeHealth.ready("test")

    def stop(self) -> RuntimeHealth:
        self.stopped = True
        return RuntimeHealth.blocked("test_stopped", "test provider stopped")

    def transform(self, _plan: Any, _content: bytes, *, cancel_check: Any) -> Any:
        del cancel_check
        raise RecipeProviderError("decode_failed")


def test_runtime_returns_redacted_provider_failure_and_stops():
    content = _image_bytes()
    provider = _FailingProvider()
    complete = _successful_messages(content)[:3]
    connection = _FakeConnection(complete)

    report = RecipeWorkerBrokerRuntime(
        connection,
        expected_principal_id=PRINCIPAL,
        job_id=JOB_ID,
        provider_factory=lambda: provider,
    ).run()

    assert report.terminal_state == "failed"
    assert connection.sent[-1].body == {
        "schema_version": "recipe.worker.error.v1",
        "request_id": "request_1",
        "job_id": JOB_ID,
        "code": "decode_failed",
    }
    assert provider.stopped


def test_runtime_enforces_message_budget_and_cleans_up():
    content = _image_bytes()
    connection = _FakeConnection([_message("prepare", _prepare(content))])
    with pytest.raises(WorkerRuntimeError) as error:
        RecipeWorkerBrokerRuntime(
            connection,
            expected_principal_id=PRINCIPAL,
            job_id=JOB_ID,
            max_messages=1,
        ).run()
    assert error.value.code == "message_budget_exceeded"
    assert connection.closed


def test_runtime_watchdog_captures_stalled_transform_and_closes():
    content = _image_bytes()
    provider = _CancellableProvider()
    connection = _FakeConnection(_successful_messages(content)[:3])

    report = RecipeWorkerBrokerRuntime(
        connection,
        expected_principal_id=PRINCIPAL,
        job_id=JOB_ID,
        provider_factory=lambda: provider,
        watchdog_timeout_ms=10,
    ).run()

    assert report.terminal_state == "failed"
    assert connection.sent[-1].body["schema_version"] == "recipe.worker.error.v1"
    assert connection.sent[-1].body["code"] == "timeout"
    assert connection.closed


def test_packaged_entrypoint_accepts_only_fixed_native_launch_shape():
    entry = runpy.run_path(
        str(Path(__file__).parents[1] / "packaging" / "recipe_worker" / "recipe_worker.py")
    )
    assert entry["main"]([]) == 78
    parsed = entry["_parse_args"](
        [
            "--native-broker",
            "--broker-pipe",
            r"\\.\pipe\cortex-test",
            "--broker-pid",
            "321",
            "--broker-principal",
            PRINCIPAL,
            "--job-id",
            JOB_ID,
        ]
    )
    assert parsed.pipe_name == r"\\.\pipe\cortex-test"
    assert parsed.broker_process_id == 321
    with pytest.raises(ValueError):
        entry["_parse_args"](["--native-broker", "--broker-pipe", "unsafe"])
