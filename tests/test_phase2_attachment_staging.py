"""Adversarial tests for durable trusted attachment staging."""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from cortex_backend.execution import (
    ArtifactBoundary,
    AttachmentStagingError,
    AttachmentStagingService,
    ExecutionRepository,
)


OWNER = "a" * 64


def _image_bytes() -> bytes:
    image = Image.new("RGB", (4, 3), (120, 80, 40))
    try:
        with BytesIO() as stream:
            image.save(stream, format="PNG")
            return stream.getvalue()
    finally:
        image.close()


def _service(tmp_path: Path, *, maximum: int = 2 * 1024 * 1024):
    repository = ExecutionRepository(
        tmp_path / "execution.sqlite",
        tmp_path / "artifacts",
        max_artifact_bytes=maximum,
    )
    boundary = ArtifactBoundary(repository, max_input_bytes=maximum)
    return repository, AttachmentStagingService(repository, boundary)


def test_stage_bytes_is_owner_scoped_idempotent_and_never_persists_payload(tmp_path: Path):
    repository, service = _service(tmp_path)
    content = _image_bytes()

    first = service.stage(owner=OWNER, request_id="attach-1", content=content)
    duplicate = service.stage(owner=OWNER, request_id="attach-1", content=content)

    assert first.artifact.artifact_id == duplicate.artifact.artifact_id
    assert first.artifact.mime_type == "image/png"
    assert first.job.status == "succeeded"
    assert first.job.result is not None
    assert "path" not in str(first.job.result).lower()
    assert base64.b64encode(content).decode("ascii") not in str(first.job.payload)
    assert repository.read_artifact(first.artifact.artifact_id) == content

    with pytest.raises(AttachmentStagingError) as conflict:
        service.stage(owner=OWNER, request_id="attach-1", content=content + b"x")
    assert conflict.value.code == "request_conflict"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", "attachment_content_invalid"),
        (b"PK\x03\x04not-an-image", "attachment_invalid"),
        (b"<svg><script>alert(1)</script></svg>", "attachment_invalid"),
    ],
)
def test_stage_bytes_rejects_empty_archive_and_active_payloads(
    tmp_path: Path,
    content: bytes,
    code: str,
):
    _repository, service = _service(tmp_path)
    with pytest.raises(AttachmentStagingError) as error:
        service.stage(owner=OWNER, request_id="attach-invalid", content=content)
    assert error.value.code == code


def test_stage_bytes_enforces_configured_byte_and_retention_limits(tmp_path: Path):
    _repository, service = _service(tmp_path, maximum=64)
    with pytest.raises(AttachmentStagingError) as too_large:
        service.stage(owner=OWNER, request_id="attach-large", content=b"x" * 65)
    assert too_large.value.code == "attachment_too_large"
    with pytest.raises(AttachmentStagingError) as retention:
        service.stage(
            owner=OWNER,
            request_id="attach-retention",
            content=b"x",
            retention_seconds=0,
        )
    assert retention.value.code == "attachment_retention_invalid"


def test_stage_failure_recording_failure_is_logged_not_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    repository, service = _service(tmp_path)

    def broken_transition(*_args: object, **_kwargs: object):
        raise RuntimeError("simulated persistence outage")

    monkeypatch.setattr(repository, "transition", broken_transition)

    with caplog.at_level("ERROR"):
        with pytest.raises(AttachmentStagingError) as error:
            service.stage(owner=OWNER, request_id="attach-outage", content=_image_bytes())

    assert error.value.code == "attachment_persist_failed"
    stuck = repository.list_jobs(owner=OWNER)
    assert [job.status for job in stuck] == ["queued"]
    assert any(
        "could not record failure attachment_persist_failed" in record.getMessage()
        for record in caplog.records
    )


def test_stage_bytes_duplicate_terminal_result_is_revalidated(tmp_path: Path):
    repository, service = _service(tmp_path)
    staged = service.stage(owner=OWNER, request_id="attach-integrity", content=_image_bytes())
    artifact_path = Path(staged.artifact.path)
    artifact_path.write_bytes(b"tampered")

    with pytest.raises(AttachmentStagingError) as error:
        service.stage(owner=OWNER, request_id="attach-integrity", content=_image_bytes())
    assert error.value.code in {"attachment_artifact_unavailable", "attachment_artifact_invalid"}
