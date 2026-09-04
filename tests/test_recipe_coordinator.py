"""Adversarial tests for the durable image-recipe coordinator."""

from __future__ import annotations

from io import BytesIO
import hashlib
from pathlib import Path
from threading import Event
import time
from typing import Any

import pytest
from PIL import Image

from cortex_backend.execution.recipe_coordinator import (
    RecipeExecutionCoordinator,
    RecipeExecutionError,
    RecipeImageRequest,
    RecipeWorkerOutput,
)
from cortex_backend.execution.recipes import parse_image_transform
from cortex_backend.execution.repository import ExecutionRepository


PRINCIPAL = "a" * 64
OWNER = PRINCIPAL


def _image_bytes() -> bytes:
    image = Image.new("RGB", (4, 3), (120, 80, 40))
    try:
        with BytesIO() as stream:
            image.save(stream, format="PNG")
            return stream.getvalue()
    finally:
        image.close()


def _plan(artifact_id: str):
    return parse_image_transform(
        {
            "schema_version": "artifact.transform.v1",
            "input_artifact_id": artifact_id,
            "steps": [{"op": "grayscale"}],
            "output_format": "png",
        }
    )


def _repository(tmp_path: Path) -> tuple[ExecutionRepository, str, str]:
    repository = ExecutionRepository(
        tmp_path / "execution.sqlite",
        tmp_path / "artifacts",
        max_artifact_bytes=2 * 1024 * 1024,
    )
    source_job, _ = repository.create_job(
        job_id="source-job",
        owner=OWNER,
        request_id="source-request",
        profile="artifact.transform.v1",
        payload={},
    )
    source = repository.publish_artifact(
        source_job.job_id,
        name="source.png",
        content=_image_bytes(),
        mime_type="image/png",
    )
    return repository, source_job.job_id, source.artifact_id


class _FakeAttempt:
    def __init__(self, output: RecipeWorkerOutput | None = None, error: str | None = None) -> None:
        self.output = output
        self.error = error
        self.started = Event()
        self.cancelled = Event()
        self.closed = False
        self.close_event = Event()

    def transform(self, _request_id: str, _job_id: str, _plan: Any, _content: bytes, cancel_event: Event) -> RecipeWorkerOutput:
        self.started.set()
        while not cancel_event.is_set() and not self.cancelled.is_set():
            if self.error is not None:
                raise RecipeExecutionError(self.error)
            break
        if cancel_event.is_set() or self.cancelled.is_set():
            raise RecipeExecutionError("cancelled")
        assert self.output is not None
        return self.output

    def cancel(self, _reason: str = "user") -> None:
        self.cancelled.set()

    def close(self) -> None:
        self.closed = True
        self.close_event.set()


def _request(source_artifact_id: str, *, request_id: str = "recipe-request") -> RecipeImageRequest:
    return RecipeImageRequest(
        owner=OWNER,
        request_id=request_id,
        source_artifact_id=source_artifact_id,
        plan=_plan(source_artifact_id),
    )


def _output() -> RecipeWorkerOutput:
    content = _image_bytes()
    return RecipeWorkerOutput(
        content=content,
        mime_type="image/png",
        format="PNG",
        width=4,
        height=3,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_request_is_opaque_and_plan_must_bind_to_input_artifact():
    with pytest.raises(RecipeExecutionError) as mismatch:
        RecipeImageRequest(
            owner=OWNER,
            request_id="recipe-request",
            source_artifact_id="artifact-a",
            plan=_plan("artifact-b"),
        )
    assert mismatch.value.code == "input_artifact_mismatch"
    with pytest.raises(ValueError):
        _request(r"C:\Users\Admin\secret.png")


def test_worker_output_rechecks_digest_and_mime():
    content = _image_bytes()
    with pytest.raises(RecipeExecutionError) as error:
        RecipeWorkerOutput(
            content=content,
            mime_type="image/jpeg",
            format="JPEG",
            width=4,
            height=3,
            sha256=hashlib.sha256(content).hexdigest(),
        )
    assert error.value.code == "worker_output_mime_mismatch"


def test_coordinator_publishes_owner_scoped_result_once(tmp_path: Path):
    repository, _source_job_id, source_artifact_id = _repository(tmp_path)
    attempt = _FakeAttempt(_output())
    coordinator = RecipeExecutionCoordinator(repository, lambda _job: attempt)
    request = _request(source_artifact_id)

    accepted = coordinator.start_image_transform(request)
    completed = coordinator.wait(accepted.job_id, timeout=15)

    assert completed.status == "succeeded"
    assert completed.result is not None
    assert "path" not in completed.result
    result_artifact_id = completed.result["artifact_id"]
    artifact = repository.get_artifact(result_artifact_id, owner=OWNER)
    assert artifact is not None
    assert repository.read_artifact(result_artifact_id) == _image_bytes()
    # The worker thread's `finally: attempt.close()` runs just after the
    # repository transition wait() observed, not atomically with it -- give
    # it a bounded window to actually execute under load rather than
    # asserting it has already happened.
    assert attempt.close_event.wait(2)

    duplicate = coordinator.start_image_transform(request)
    assert duplicate.job_id == accepted.job_id


def test_coordinator_rejects_request_conflict_and_wrong_owner_artifact(tmp_path: Path):
    repository, _source_job_id, source_artifact_id = _repository(tmp_path)
    coordinator = RecipeExecutionCoordinator(repository, lambda _job: _FakeAttempt(_output()))
    accepted = coordinator.start_image_transform(_request(source_artifact_id))
    conflicting = RecipeImageRequest(
        owner=OWNER,
        request_id="recipe-request",
        source_artifact_id=source_artifact_id,
        plan=parse_image_transform(
            {
                "schema_version": "artifact.transform.v1",
                "input_artifact_id": source_artifact_id,
                "steps": [{"op": "grayscale"}],
                "output_format": "jpeg",
            }
        ),
    )
    with pytest.raises(RecipeExecutionError) as conflict:
        coordinator.start_image_transform(conflicting)
    assert accepted.job_id
    assert conflict.value.code == "request_conflict"

    with pytest.raises(RecipeExecutionError) as missing:
        coordinator.start_image_transform(_request("not-an-artifact"))
    assert missing.value.code == "input_artifact_unavailable"


def test_coordinator_failure_is_redacted_and_does_not_publish(tmp_path: Path):
    repository, _source_job_id, source_artifact_id = _repository(tmp_path)
    attempt = _FakeAttempt(error="provider_failed")
    coordinator = RecipeExecutionCoordinator(repository, lambda _job: attempt)
    accepted = coordinator.start_image_transform(_request(source_artifact_id))
    completed = coordinator.wait(accepted.job_id, timeout=15)

    assert completed.status == "failed"
    assert completed.error == "provider_failed"
    assert completed.result is None
    assert list((repository.artifact_root / accepted.job_id).glob("*")) == []


def test_coordinator_cancellation_is_terminal_and_cleans_staging(tmp_path: Path):
    repository, _source_job_id, source_artifact_id = _repository(tmp_path)
    attempt = _FakeAttempt(_output())
    coordinator = RecipeExecutionCoordinator(repository, lambda _job: attempt)
    accepted = coordinator.start_image_transform(_request(source_artifact_id))
    assert attempt.started.wait(2)
    coordinator.cancel(accepted.job_id, owner=OWNER)
    completed = coordinator.wait(accepted.job_id, timeout=15)

    assert completed.status == "cancelled"
    assert completed.error == "cancelled"
    assert completed.result is None
    # Same reasoning as the success-path test above: attempt.close() runs in
    # the worker thread's finally block, just after (not atomically with)
    # the repository transition that wait() observed.
    assert attempt.close_event.wait(2)


def test_recovery_rejects_tampered_payload_and_never_interprets_a_path(tmp_path: Path):
    repository, _source_job_id, source_artifact_id = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="recovery-job",
        owner=OWNER,
        request_id="recovery-request",
        profile="recipe.image.v1",
        payload={
            "schema_version": "recipe.execution.v1",
            "provider": "recipe-image-v1",
            "source_artifact_id": source_artifact_id,
            "plan": _plan(source_artifact_id).model_dump(mode="json"),
            "plan_digest": _plan(source_artifact_id).digest(),
            "retention_seconds": 86_400,
            "path": r"C:\Windows\System32\cmd.exe",
        },
    )
    repository.claim_lease(job.job_id, lease_owner="crashed-coordinator", ttl_seconds=0.01)
    time.sleep(0.03)
    coordinator = RecipeExecutionCoordinator(repository, lambda _job: _FakeAttempt(_output()))
    recovered = coordinator.startup_recover()

    assert job.job_id in recovered
    failed = repository.get_job(job.job_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error == "recovery_invalid_payload"
