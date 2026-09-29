"""Durable, owner-scoped staging for user attachment bytes.

The attachment boundary is intentionally a small capability: it accepts one
bounded in-memory payload, validates the bytes through :class:`ArtifactBoundary`,
and returns only an opaque artifact identifier and derived metadata.  Raw paths,
filenames, shell text, and executable authority never enter the job payload.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
import re
from typing import Any
from uuid import uuid4

from .artifact_boundary import ArtifactBoundary, ArtifactBoundaryError, sniff_artifact_mime
from .models import ExecutionArtifact, ExecutionJob
from .repository import ExecutionRepository, ExecutionRepositoryError


ATTACHMENT_STAGE_PROFILE = "attachment.stage.v1"
ATTACHMENT_PAYLOAD_SCHEMA = "attachment.stage.v1"
ATTACHMENT_RESULT_SCHEMA = "attachment.result.v1"
DEFAULT_ATTACHMENT_RETENTION_SECONDS = 86_400
MAX_ATTACHMENT_RETENTION_SECONDS = 30 * 86_400
# Staging one bounded payload takes milliseconds, so the lease that marks a job
# as in flight is short. It exists so that recovery can see a stager that died
# holding it; a job that has no live lease and has been silent for
# ABANDONED_STAGE_SECONDS is treated as dead by the retry path.
STAGING_LEASE_SECONDS = 60.0
ABANDONED_STAGE_SECONDS = 120.0
ATTACHMENT_INTERRUPTED = "attachment_interrupted"
ATTACHMENT_INTERRUPTED_MESSAGE = "Attachment staging was interrupted."
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class AttachmentStagingError(RuntimeError):
    """Stable, redacted attachment staging failure category."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid attachment staging error code")
        self.code = code
        super().__init__("The attachment could not be staged safely.")


@dataclass(frozen=True, slots=True)
class AttachmentStageResult:
    """Opaque result returned after one durable attachment stage operation."""

    job: ExecutionJob
    artifact: ExecutionArtifact


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded opaque identifier")
    return value


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class AttachmentStagingService:
    """Create and complete owner-scoped ``attachment.stage.v1`` jobs."""

    def __init__(self, repository: ExecutionRepository, boundary: ArtifactBoundary) -> None:
        if not isinstance(repository, ExecutionRepository):
            raise TypeError("repository must be an ExecutionRepository")
        if not isinstance(boundary, ArtifactBoundary):
            raise TypeError("boundary must be an ArtifactBoundary")
        if boundary.repository is not repository:
            raise ValueError("attachment boundary repository mismatch")
        self.repository = repository
        self.boundary = boundary

    def stage(
        self,
        *,
        owner: str,
        request_id: str,
        content: bytes,
        retention_seconds: int = DEFAULT_ATTACHMENT_RETENTION_SECONDS,
    ) -> AttachmentStageResult:
        """Validate, persist, and return one idempotent attachment stage."""

        _safe_id(owner, "owner")
        _safe_id(request_id, "request_id")
        if not isinstance(content, bytes) or not content:
            raise AttachmentStagingError("attachment_content_invalid")
        if len(content) > self.boundary.max_input_bytes:
            raise AttachmentStagingError("attachment_too_large")
        if (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or not 1 <= retention_seconds <= MAX_ATTACHMENT_RETENTION_SECONDS
        ):
            raise AttachmentStagingError("attachment_retention_invalid")
        try:
            mime_type = sniff_artifact_mime(content)
        except ArtifactBoundaryError:
            raise AttachmentStagingError("attachment_invalid") from None
        digest = sha256(content).hexdigest()
        payload = {
            "schema_version": ATTACHMENT_PAYLOAD_SCHEMA,
            "sha256": digest,
            "size": len(content),
            "mime_type": mime_type,
            "retention_seconds": retention_seconds,
        }
        try:
            job, created = self.repository.create_job(
                job_id=uuid4().hex,
                owner=owner,
                request_id=request_id,
                profile=ATTACHMENT_STAGE_PROFILE,
                payload=payload,
            )
        except ExecutionRepositoryError:
            raise AttachmentStagingError("attachment_persist_failed") from None
        if not created:
            return self._existing(job, payload)

        # A stager that dies between creating the job and finishing it leaves a
        # queued job that recovery cannot see unless it holds a lease, and that
        # nothing else ever retires -- its request id then answers "in
        # progress" for good. Holding a short lease while the bytes are staged
        # makes the job findable; it is released on every exit that runs code.
        lease_owner = f"attachment-stager-{uuid4().hex}"
        try:
            self.repository.claim_lease(
                job.job_id, lease_owner=lease_owner, ttl_seconds=STAGING_LEASE_SECONDS
            )
        except ExecutionRepositoryError:
            self._fail(job.job_id, "attachment_persist_failed")
            raise AttachmentStagingError("attachment_persist_failed") from None
        try:
            return self._stage_created(
                job,
                owner=owner,
                content=content,
                retention_seconds=retention_seconds,
                digest=digest,
                mime_type=mime_type,
            )
        finally:
            try:
                self.repository.release_lease(job.job_id, lease_owner=lease_owner)
            except Exception:
                pass  # It expires by itself; nothing here may replace the real outcome.

    def _stage_created(
        self,
        job: ExecutionJob,
        *,
        owner: str,
        content: bytes,
        retention_seconds: int,
        digest: str,
        mime_type: str,
    ) -> AttachmentStageResult:
        try:
            artifact = self.boundary.stage_bytes(
                job.job_id,
                owner,
                content,
                retention_seconds=retention_seconds,
            )
        except ArtifactBoundaryError as exc:
            code = self._boundary_code(exc.code)
            self._fail(job.job_id, code)
            raise AttachmentStagingError(code) from None
        result = self._result_payload(artifact, digest=digest, mime_type=mime_type)
        try:
            completed = self.repository.transition(
                job.job_id,
                status="succeeded",
                event="completed",
                phase="completed",
                data={"message": "Attachment staged."},
                result=result,
            )
        except Exception:
            try:
                self.repository.delete_artifact(artifact.artifact_id)
            except Exception:
                self._fail(job.job_id, "attachment_cleanup_pending")
                raise AttachmentStagingError("attachment_cleanup_pending") from None
            self._fail(job.job_id, "attachment_persist_failed")
            raise AttachmentStagingError("attachment_persist_failed") from None
        if completed.status != "succeeded":
            # A finished job is immutable, so the write above changed nothing and
            # only reported the job as it stands: a retry retired it as
            # abandoned while this call was still holding its bytes. Reporting
            # success for a job the store says failed would hand out an
            # artifact no result refers to.
            try:
                self.repository.delete_artifact(artifact.artifact_id)
            except Exception:
                pass  # Retention reclaims it.
            raise AttachmentStagingError("attachment_failed")
        return AttachmentStageResult(job=completed, artifact=artifact)

    def _existing(
        self,
        job: ExecutionJob,
        payload: Mapping[str, Any],
    ) -> AttachmentStageResult:
        try:
            payload_matches = _canonical(job.payload) == _canonical(payload)
        except (TypeError, ValueError):
            payload_matches = False
        if job.profile != ATTACHMENT_STAGE_PROFILE or not payload_matches:
            raise AttachmentStagingError("request_conflict")
        if job.status in {"queued", "running"}:
            job = self._retire_if_abandoned(job)
        if job.status != "succeeded":
            if job.status in {"queued", "running"}:
                raise AttachmentStagingError("attachment_in_progress")
            raise AttachmentStagingError("attachment_failed")
        result = job.result
        if not isinstance(result, Mapping):
            raise AttachmentStagingError("attachment_result_invalid")
        artifact_id = result.get("artifact_id")
        if not isinstance(artifact_id, str) or _SAFE_ID.fullmatch(artifact_id) is None:
            raise AttachmentStagingError("attachment_result_invalid")
        artifact = self.repository.get_artifact(artifact_id, owner=job.owner)
        if artifact is None:
            raise AttachmentStagingError("attachment_artifact_unavailable")
        if (
            set(result) != {
                "schema_version",
                "artifact_id",
                "mime_type",
                "size",
                "sha256",
                "expires_at",
            }
            or result.get("schema_version") != ATTACHMENT_RESULT_SCHEMA
            or result.get("sha256") != artifact.sha256
            or result.get("size") != artifact.size
            or result.get("mime_type") != artifact.mime_type
            or result.get("expires_at") != artifact.expires_at
            or artifact.sha256 != payload.get("sha256")
            or artifact.size != payload.get("size")
            or artifact.mime_type != payload.get("mime_type")
        ):
            raise AttachmentStagingError("attachment_result_invalid")
        try:
            content = self.repository.read_artifact(artifact.artifact_id)
            if sha256(content).hexdigest() != artifact.sha256 or sniff_artifact_mime(content) != artifact.mime_type:
                raise AttachmentStagingError("attachment_artifact_invalid")
        except AttachmentStagingError:
            raise
        except (ExecutionRepositoryError, ArtifactBoundaryError):
            raise AttachmentStagingError("attachment_artifact_unavailable") from None
        return AttachmentStageResult(job=job, artifact=artifact)

    def _retire_if_abandoned(self, job: ExecutionJob) -> ExecutionJob:
        """Fail a job whose stager died, and return the job as it now reads.

        The store decides, atomically, that nothing is working on it: no live
        lease and no write for ABANDONED_STAGE_SECONDS. A job that is merely
        slow, or that finished in the meantime, comes back as it is. The
        request id then answers ``attachment_failed`` like any other failed
        staging, instead of "in progress" for good.
        """

        try:
            current = self.repository.retire_abandoned_job(
                job.job_id,
                idle_seconds=ABANDONED_STAGE_SECONDS,
                error=ATTACHMENT_INTERRUPTED,
                message=ATTACHMENT_INTERRUPTED_MESSAGE,
            )
        except ExecutionRepositoryError:
            return job
        return job if current is None else current

    @staticmethod
    def _result_payload(
        artifact: ExecutionArtifact,
        *,
        digest: str,
        mime_type: str,
    ) -> Mapping[str, Any]:
        return {
            "schema_version": ATTACHMENT_RESULT_SCHEMA,
            "artifact_id": artifact.artifact_id,
            "mime_type": mime_type,
            "size": artifact.size,
            "sha256": digest,
            "expires_at": artifact.expires_at,
        }

    @staticmethod
    def _boundary_code(code: str) -> str:
        return {
            "artifact_too_large": "attachment_too_large",
            "artifact_content_invalid": "attachment_content_invalid",
            "artifact_retention_invalid": "attachment_retention_invalid",
            "artifact_publish_failed": "attachment_publish_failed",
        }.get(code, "attachment_invalid")

    def _fail(self, job_id: str, code: str) -> None:
        # Best-effort: the caller is about to raise the real staging error, so a
        # failure to record it must not replace that error -- but it may not be
        # silent either, because the job is left in a non-terminal state.
        try:
            self.repository.transition(
                job_id,
                status="failed",
                event="failed",
                phase="failed",
                data={"message": "Attachment staging failed safely."},
                error=code,
            )
        except Exception as exc:
            logging.error(
                "Cortex attachment staging could not record failure %s for job %s (%s); "
                "the job remains non-terminal.",
                code,
                job_id,
                type(exc).__name__,
            )


__all__ = [
    "ABANDONED_STAGE_SECONDS",
    "ATTACHMENT_INTERRUPTED",
    "ATTACHMENT_INTERRUPTED_MESSAGE",
    "ATTACHMENT_PAYLOAD_SCHEMA",
    "ATTACHMENT_RESULT_SCHEMA",
    "ATTACHMENT_STAGE_PROFILE",
    "AttachmentStageResult",
    "AttachmentStagingError",
    "AttachmentStagingService",
    "DEFAULT_ATTACHMENT_RETENTION_SECONDS",
    "MAX_ATTACHMENT_RETENTION_SECONDS",
    "STAGING_LEASE_SECONDS",
]
