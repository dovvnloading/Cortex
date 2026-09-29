"""Adversarial tests for durable trusted attachment staging."""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from cortex_backend.execution.artifact_boundary import ArtifactBoundary, ArtifactBoundaryError
from cortex_backend.execution.attachment_staging import (
    ABANDONED_STAGE_SECONDS,
    ATTACHMENT_INTERRUPTED,
    ATTACHMENT_PAYLOAD_SCHEMA,
    ATTACHMENT_STAGE_PROFILE,
    DEFAULT_ATTACHMENT_RETENTION_SECONDS,
    STAGING_LEASE_SECONDS,
    AttachmentStagingError,
    AttachmentStagingService,
)
from cortex_backend.execution.repository import ExecutionRepository


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


def test_a_disk_failure_fails_the_job_instead_of_escaping(tmp_path: Path, monkeypatch):
    """A full disk must produce a stable code and a terminal job.

    `publish_artifact` removes its partial files and then re-raises the
    original exception, so an OSError -- a full disk, a permission error, an
    antivirus lock -- reached `_publish_bytes` unwrapped. That method caught
    only `ExecutionRepositoryError`, so the exception escaped the boundary
    entirely: the caller's `except ArtifactBoundaryError` missed it, `_fail`
    never ran, the job stayed non-terminal for the rest of the installation's
    life, and the route answered HTTP 500 rather than a stable code.

    `publish_outputs` in the same class already treats both the same way.
    """
    repository, service = _service(tmp_path)

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ExecutionRepository, "publish_artifact", full_disk)

    with pytest.raises(AttachmentStagingError):
        service.stage(owner=OWNER, request_id="attach-disk-full", content=_image_bytes())

    with repository.connect() as connection:
        statuses = [
            str(row["status"])
            for row in connection.execute("SELECT status FROM execution_jobs").fetchall()
        ]

    assert statuses == ["failed"], "the staging job was left non-terminal"


# -- A stager that dies part-way must not block its request id for good -------------


class _HardKill(BaseException):
    """Stands in for a killed process: nothing in a ``finally`` runs when one dies."""


def _service_over(repository: ExecutionRepository) -> tuple[ExecutionRepository, ArtifactBoundary, AttachmentStagingService]:
    boundary = ArtifactBoundary(repository)
    return repository, boundary, AttachmentStagingService(repository, boundary)


def _payload_for(content: bytes) -> dict[str, object]:
    return {
        "schema_version": ATTACHMENT_PAYLOAD_SCHEMA,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "mime_type": "image/png",
        "retention_seconds": DEFAULT_ATTACHMENT_RETENTION_SECONDS,
    }


def _queued_stage_job(repository: ExecutionRepository, content: bytes, request_id: str, job_id: str):
    job, _ = repository.create_job(
        job_id=job_id,
        owner=OWNER,
        request_id=request_id,
        profile=ATTACHMENT_STAGE_PROFILE,
        payload=_payload_for(content),
    )
    return job


def _stage_and_die_holding_the_lease(service, repository, boundary, monkeypatch, request_id: str) -> str:
    """Run a stage that is killed mid-way; return the job it left behind."""

    def killed(*_args, **_kwargs):
        raise _HardKill

    with monkeypatch.context() as crash:
        crash.setattr(boundary, "stage_bytes", killed)
        # A killed process never reaches its ``finally`` blocks.
        crash.setattr(repository, "release_lease", lambda *_a, **_k: None)
        with pytest.raises(_HardKill):
            service.stage(owner=OWNER, request_id=request_id, content=_image_bytes())
    (job,) = repository.list_jobs(owner=OWNER)
    return job.job_id


def test_the_staging_lease_is_held_while_bytes_are_staged_and_released_afterwards(
    execution_repository, monkeypatch
):
    repository, boundary, service = _service_over(execution_repository)
    real_stage = boundary.stage_bytes
    seen_during: list[str | None] = []

    def observing(job_id, *args, **kwargs):
        seen_during.append(repository.lease_holder(job_id))
        return real_stage(job_id, *args, **kwargs)

    monkeypatch.setattr(boundary, "stage_bytes", observing)
    staged = service.stage(owner=OWNER, request_id="attach-lease", content=_image_bytes())

    assert len(seen_during) == 1 and seen_during[0] is not None
    assert repository.lease_holder(staged.job.job_id) is None


def test_the_staging_lease_is_released_when_staging_fails(execution_repository, monkeypatch):
    repository, boundary, service = _service_over(execution_repository)

    def refuses(*_args, **_kwargs):
        raise ArtifactBoundaryError("artifact_publish_failed")

    monkeypatch.setattr(boundary, "stage_bytes", refuses)
    with pytest.raises(AttachmentStagingError):
        service.stage(owner=OWNER, request_id="attach-fails", content=_image_bytes())

    (job,) = repository.list_jobs(owner=OWNER, include_terminal=True)
    assert job.status == "failed"
    assert repository.lease_holder(job.job_id) is None


def test_a_stager_that_died_holding_its_lease_is_found_by_recovery(
    execution_repository, frozen_clock, monkeypatch
):
    repository, boundary, service = _service_over(execution_repository)
    job_id = _stage_and_die_holding_the_lease(service, repository, boundary, monkeypatch, "attach-crash")

    assert repository.get_job(job_id).status == "queued"
    assert repository.lease_holder(job_id) is not None
    frozen_clock.advance(STAGING_LEASE_SECONDS + 1)
    assert repository.recover_expired_leases() == [job_id]


def test_a_retry_after_a_crashed_stage_retires_the_job_instead_of_answering_in_progress_forever(
    execution_repository, frozen_clock, monkeypatch
):
    """A hard kill between creating the job and finishing it left it queued for good.

    Recovery only sees jobs that hold a lease and cleanup only purges finished
    ones, so the job stayed in the tray and its request id answered "in
    progress" on every retry. The retry now retires a job nobody is working on.
    """

    repository, boundary, service = _service_over(execution_repository)
    content = _image_bytes()
    job_id = _stage_and_die_holding_the_lease(service, repository, boundary, monkeypatch, "attach-crash")
    frozen_clock.advance(ABANDONED_STAGE_SECONDS + 1)

    for _ in range(2):  # the answer is stable, not a one-off
        with pytest.raises(AttachmentStagingError) as retry:
            service.stage(owner=OWNER, request_id="attach-crash", content=content)
        assert retry.value.code == "attachment_failed"

    job = repository.get_job(job_id)
    assert job is not None
    assert (job.status, job.error) == ("failed", ATTACHMENT_INTERRUPTED)
    assert [event.event for event in repository.events(job_id)].count("failed") == 1
    assert repository.lease_holder(job_id) is None
    assert repository.list_jobs(owner=OWNER) == []  # no longer a task in the tray


def test_a_recent_or_leased_stage_is_still_reported_as_in_progress(execution_repository, frozen_clock):
    repository, _boundary, service = _service_over(execution_repository)
    content = _image_bytes()
    quiet = _queued_stage_job(repository, content, "attach-quiet", "job-quiet")
    frozen_clock.advance(ABANDONED_STAGE_SECONDS - 5)

    with pytest.raises(AttachmentStagingError) as recent:
        service.stage(owner=OWNER, request_id="attach-quiet", content=content)
    assert recent.value.code == "attachment_in_progress"
    assert repository.get_job(quiet.job_id).status == "queued"

    # Old enough to be abandoned, but another stager holds a live lease on it.
    frozen_clock.advance(ABANDONED_STAGE_SECONDS)
    repository.claim_lease(quiet.job_id, lease_owner="another-stager", ttl_seconds=STAGING_LEASE_SECONDS)
    with pytest.raises(AttachmentStagingError) as leased:
        service.stage(owner=OWNER, request_id="attach-quiet", content=content)
    assert leased.value.code == "attachment_in_progress"
    assert repository.get_job(quiet.job_id).status == "queued"


def test_a_retry_never_changes_the_payload_check_or_an_existing_success(execution_repository, frozen_clock):
    repository, _boundary, service = _service_over(execution_repository)
    content = _image_bytes()
    first = service.stage(owner=OWNER, request_id="attach-done", content=content)
    frozen_clock.advance(ABANDONED_STAGE_SECONDS * 10)

    again = service.stage(owner=OWNER, request_id="attach-done", content=content)
    assert again.artifact.artifact_id == first.artifact.artifact_id
    with pytest.raises(AttachmentStagingError) as conflict:
        service.stage(owner=OWNER, request_id="attach-done", content=content + b"x")
    assert conflict.value.code == "request_conflict"


def test_a_repeated_request_reads_the_staged_bytes_as_their_owner_never_unscoped(
    execution_repository, monkeypatch
):
    """``get_artifact`` is owner-scoped; the read that follows it must be too.

    The two calls are separate queries, so an unscoped read would trust
    whatever the first one saw.
    """

    repository, _boundary, service = _service_over(execution_repository)
    content = _image_bytes()
    service.stage(owner=OWNER, request_id="attach-owner", content=content)
    real_read = repository.read_artifact
    owners: list[str | None] = []

    def recording(artifact_id, *, owner=None):
        owners.append(owner)
        return real_read(artifact_id, owner=owner)

    monkeypatch.setattr(repository, "read_artifact", recording)

    service.stage(owner=OWNER, request_id="attach-owner", content=content)

    assert owners == [OWNER]


def test_a_stager_finishing_after_its_job_was_retired_reports_failure_and_drops_the_artifact(
    execution_repository, monkeypatch
):
    repository, boundary, service = _service_over(execution_repository)
    real_stage = boundary.stage_bytes

    def stage_then_get_retired(job_id, *args, **kwargs):
        artifact = real_stage(job_id, *args, **kwargs)
        repository.transition(
            job_id,
            status="failed",
            event="failed",
            phase="recovery",
            data={"message": "retired underneath the stager"},
            error=ATTACHMENT_INTERRUPTED,
        )
        return artifact

    monkeypatch.setattr(boundary, "stage_bytes", stage_then_get_retired)
    with pytest.raises(AttachmentStagingError) as error:
        service.stage(owner=OWNER, request_id="attach-late", content=_image_bytes())

    assert error.value.code == "attachment_failed"
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM execution_artifacts").fetchone()[0] == 0


def test_startup_recovery_fails_a_staging_job_whose_stager_died(
    execution_repository, coordinator, frozen_clock
):
    repository = execution_repository
    job = _queued_stage_job(repository, _image_bytes(), "attach-recover", "job-recover")
    repository.claim_lease(job.job_id, lease_owner="dead-stager", ttl_seconds=STAGING_LEASE_SECONDS)
    frozen_clock.advance(STAGING_LEASE_SECONDS + 1)

    recovered = coordinator.startup_recover()

    assert job.job_id in recovered
    failed = repository.get_job(job.job_id)
    assert failed is not None
    assert (failed.status, failed.error) == ("failed", ATTACHMENT_INTERRUPTED)
    assert repository.lease_holder(job.job_id) is None


def test_startup_recovery_retires_a_lease_less_staging_job_only_once_it_has_gone_quiet(
    execution_repository, coordinator, frozen_clock
):
    repository = execution_repository
    owner = repository.installation_principal_id
    content = _image_bytes()

    def queue(request_id: str, job_id: str):
        job, _ = repository.create_job(
            job_id=job_id,
            owner=owner,
            request_id=request_id,
            profile=ATTACHMENT_STAGE_PROFILE,
            payload=_payload_for(content),
        )
        return job

    quiet = queue("attach-quiet", "job-quiet")
    frozen_clock.advance(ABANDONED_STAGE_SECONDS + 1)
    fresh = queue("attach-fresh", "job-fresh")

    coordinator.startup_recover()

    assert repository.get_job(quiet.job_id).status == "failed"
    assert repository.get_job(quiet.job_id).error == ATTACHMENT_INTERRUPTED
    assert repository.get_job(fresh.job_id).status == "queued"
