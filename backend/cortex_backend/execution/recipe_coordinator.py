"""Durable coordinator for the qualified fixed-function image recipe.

The coordinator is intentionally an internal composition seam.  It accepts an
opaque, owner-scoped artifact identifier and a typed :class:`ImageTransformPlan`,
then delegates the bounded transform to an injected authenticated worker attempt.
It never accepts source paths, model code, shell commands, or provider authority.
Outputs are staged privately and published only through ``ArtifactBoundary`` after
the complete result has been validated.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Event, Lock, Thread
import time
from typing import Final, Any, Protocol
from uuid import uuid4

from .artifact_boundary import (
    ArtifactBoundary,
    ArtifactBoundaryError,
    OutputClaim,
    PublishedArtifact,
    sniff_artifact_mime,
)
from .models import ExecutionJob, TerminalExecutionStatus
from .recipe_provider import MAX_INPUT_BYTES, MAX_OUTPUT_BYTES
from .recipes import ImageTransformPlan, RecipeValidationError, parse_image_transform
from .repository import (
    ExecutionRepository,
    ExecutionRepositoryError,
    ExecutionTransitionConflict,
    LeaseConflict,
)


RECIPE_IMAGE_PROFILE: Final = "recipe.image.v1"
RECIPE_PAYLOAD_SCHEMA = "recipe.execution.v1"
RECIPE_RESULT_SCHEMA = "recipe.result.v1"
DEFAULT_RECIPE_RETENTION_SECONDS = 86_400
MAX_RECIPE_RETENTION_SECONDS = 30 * 86_400
DEFAULT_WORKER_TIMEOUT_SECONDS = 120.0
DEFAULT_CANCEL_GRACE_SECONDS = 5.0
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MIME_TO_FORMAT = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


class RecipeExecutionError(RuntimeError):
    """Stable, redacted coordinator failure category."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid recipe execution error code")
        self.code = code
        super().__init__("The image recipe could not be completed safely.")


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded opaque identifier")
    return value


@dataclass(frozen=True, slots=True)
class RecipeImageRequest:
    """A durable image request with no host path or executable authority."""

    owner: str
    request_id: str
    source_artifact_id: str
    plan: ImageTransformPlan
    retention_seconds: int = DEFAULT_RECIPE_RETENTION_SECONDS

    def __post_init__(self) -> None:
        _safe_id(self.owner, "owner")
        _safe_id(self.request_id, "request_id")
        _safe_id(self.source_artifact_id, "source_artifact_id")
        if not isinstance(self.plan, ImageTransformPlan):
            raise TypeError("plan must be an ImageTransformPlan")
        if self.plan.input_artifact_id != self.source_artifact_id:
            raise RecipeExecutionError("input_artifact_mismatch")
        if (
            isinstance(self.retention_seconds, bool)
            or not isinstance(self.retention_seconds, int)
            or not 1 <= self.retention_seconds <= MAX_RECIPE_RETENTION_SECONDS
        ):
            raise ValueError("retention_seconds is outside the bounded recipe limit")


@dataclass(frozen=True, slots=True)
class RecipeWorkerOutput:
    """Validated in-memory output returned by one worker attempt."""

    content: bytes
    mime_type: str
    format: str
    width: int
    height: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes) or not self.content:
            raise RecipeExecutionError("worker_output_invalid")
        if len(self.content) > MAX_OUTPUT_BYTES:
            raise RecipeExecutionError("worker_output_too_large")
        if self.mime_type not in _MIME_TO_FORMAT or _MIME_TO_FORMAT[self.mime_type] != self.format:
            raise RecipeExecutionError("worker_output_invalid")
        if type(self.width) is not int or type(self.height) is not int:
            raise RecipeExecutionError("worker_output_invalid")
        if not 1 <= self.width <= 16_384 or not 1 <= self.height <= 16_384:
            raise RecipeExecutionError("worker_output_invalid")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise RecipeExecutionError("worker_output_invalid")
        if sha256(self.content).hexdigest() != self.sha256:
            raise RecipeExecutionError("worker_output_hash_mismatch")
        try:
            detected = sniff_artifact_mime(self.content)
        except ArtifactBoundaryError:
            raise RecipeExecutionError("worker_output_invalid") from None
        if detected != self.mime_type:
            raise RecipeExecutionError("worker_output_mime_mismatch")


class RecipeWorkerAttempt(Protocol):
    """One already-authenticated, bounded worker invocation."""

    def transform(
        self,
        request_id: str,
        job_id: str,
        plan: ImageTransformPlan,
        content: bytes,
        cancel_event: Event,
    ) -> RecipeWorkerOutput:
        """Transform immutable bytes and return a validated output record."""

    def cancel(self, reason: str = "user") -> None:
        """Send a bounded cancellation request to the worker."""

    def close(self) -> None:
        """Close the authenticated worker transport and release handles."""


RecipeWorkerAttemptFactory = Callable[[ExecutionJob], RecipeWorkerAttempt]



class RecipeExecutionCoordinator:
    """Durable, owner-scoped coordinator for the qualified image recipe."""

    def __init__(
        self,
        repository: ExecutionRepository,
        worker_factory: RecipeWorkerAttemptFactory,
        *,
        artifact_boundary: ArtifactBoundary | None = None,
        lease_seconds: float = 30.0,
        supervisor_lease_seconds: float = 30.0,
        auto_recover: bool = False,
    ) -> None:
        if not isinstance(repository, ExecutionRepository):
            raise TypeError("repository must be an ExecutionRepository")
        if not callable(worker_factory):
            raise TypeError("worker_factory must be callable")
        if lease_seconds <= 0 or supervisor_lease_seconds <= 0:
            raise ValueError("lease durations must be positive")
        self.repository = repository
        self.worker_factory = worker_factory
        self.artifact_boundary = artifact_boundary or ArtifactBoundary(repository)
        self.lease_seconds = float(lease_seconds)
        self.supervisor_lease_seconds = float(supervisor_lease_seconds)
        self._supervisor_owner = f"recipe-supervisor-{uuid4().hex}"
        self._supervisor_lease_active = False
        self._lock = Lock()
        self._threads: dict[str, Thread] = {}
        self._cancel_events: dict[str, Event] = {}
        self._attempts: dict[str, RecipeWorkerAttempt] = {}
        if auto_recover:
            self.startup_recover()

    def start_image_transform(self, request: RecipeImageRequest) -> ExecutionJob:
        if not isinstance(request, RecipeImageRequest):
            raise TypeError("request must be a RecipeImageRequest")
        self._load_input(request.owner, request.source_artifact_id)
        payload = self._payload_for_request(request)
        job, created = self.repository.create_job(
            job_id=uuid4().hex,
            owner=request.owner,
            request_id=request.request_id,
            profile=RECIPE_IMAGE_PROFILE,
            payload=payload,
        )
        if not created:
            if job.profile != RECIPE_IMAGE_PROFILE or self._canonical_payload(job.payload) != self._canonical_payload(payload):
                raise RecipeExecutionError("request_conflict")
            return job
        self._launch(job.job_id, request)
        return job

    start = start_image_transform

    def resume(self, job_id: str) -> ExecutionJob:
        job = self.repository.get_job(job_id)
        if job is None:
            raise ValueError("execution job does not exist")
        if job.profile != RECIPE_IMAGE_PROFILE:
            raise RecipeExecutionError("recovery_profile_invalid")
        if job.status in TerminalExecutionStatus:
            return job
        request = self._request_from_job(job)
        self._launch(job.job_id, request)
        return self.repository.get_job(job.job_id) or job

    def wait(self, job_id: str, *, timeout: float = 5.0) -> ExecutionJob:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = time.monotonic() + timeout
        while True:
            job = self.repository.get_job(job_id)
            if job is None:
                raise ValueError("execution job does not exist")
            if job.status in TerminalExecutionStatus:
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError("execution job did not reach a terminal state")
            time.sleep(0.005)

    def cancel(self, job_id: str, *, owner: str) -> ExecutionJob:
        job = self.repository.get_job(job_id, owner=owner)
        if job is None:
            raise ValueError("execution job does not exist or is not owned by caller")
        with self._lock:
            event = self._cancel_events.get(job_id)
            attempt = self._attempts.get(job_id)
            if event is not None:
                event.set()
        if attempt is not None:
            try:
                attempt.cancel("user")
            except Exception:
                pass
        return self.repository.request_cancel(job_id)

    def shutdown(self, *, timeout: float = 5.0) -> None:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        with self._lock:
            events = list(self._cancel_events.values())
            attempts = list(self._attempts.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        for attempt in attempts:
            try:
                attempt.cancel("shutdown")
            except Exception:
                pass
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._supervisor_lease_active:
            self.repository.release_supervisor_lease(lease_owner=self._supervisor_owner)
            self._supervisor_lease_active = False

    def startup_recover(self) -> list[str]:
        if self._supervisor_lease_active:
            return []
        self.repository.claim_supervisor_lease(
            lease_owner=self._supervisor_owner,
            ttl_seconds=self.supervisor_lease_seconds,
        )
        self._supervisor_lease_active = True
        recovered = self.repository.recover_expired_leases()
        self.repository.expire_approvals()
        self.recover_jobs(recovered)
        return recovered

    def recover_jobs(self, recovered_job_ids: Iterable[str]) -> None:
        """Resume only recipe jobs after a lifecycle owner reclaimed leases.

        The lifecycle owns its supervisor lease itself.
        A local composite runtime can instead claim that lease once and pass
        the recovered IDs here, avoiding two independent supervisors racing
        over the same durable store.
        """

        for job_id in recovered_job_ids:
            job = self.repository.get_job(job_id)
            if job is None or job.status in TerminalExecutionStatus:
                continue
            if job.profile != RECIPE_IMAGE_PROFILE:
                continue
            events = self.repository.events(job_id)
            if len(events) >= 2 and events[-2].event == "cancelling":
                try:
                    self.repository.transition(
                        job_id,
                        status="cancelled",
                        event="cancelled",
                        phase="recovery",
                        data={"message": "Recipe cancellation was recovered."},
                        error="cancelled",
                    )
                except Exception:
                    pass
                continue
            try:
                request = self._request_from_job(job)
                self._load_input(request.owner, request.source_artifact_id)
            except (RecipeExecutionError, ValueError, TypeError, KeyError, RecipeValidationError):
                self._fail_recovery(job_id, "recovery_invalid_payload")
                continue
            self._launch(job_id, request)

    def _fail_recovery(self, job_id: str, code: str) -> None:
        try:
            self.repository.transition(
                job_id,
                status="failed",
                event="failed",
                phase="recovery",
                data={"message": "Recipe recovery metadata is invalid."},
                error=code,
            )
        except Exception:
            pass

    @staticmethod
    def _canonical_payload(payload: Mapping[str, Any]) -> str:
        return json.dumps(dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _payload_for_request(request: RecipeImageRequest) -> Mapping[str, Any]:
        return {
            "schema_version": RECIPE_PAYLOAD_SCHEMA,
            "provider": "recipe-image-v1",
            "source_artifact_id": request.source_artifact_id,
            "plan": json.loads(request.plan.canonical_json()),
            "plan_digest": request.plan.digest(),
            "retention_seconds": request.retention_seconds,
        }

    @staticmethod
    def _request_from_job(job: ExecutionJob) -> RecipeImageRequest:
        payload = job.payload
        if (
            set(payload) != {
                "schema_version",
                "provider",
                "source_artifact_id",
                "plan",
                "plan_digest",
                "retention_seconds",
            }
            or payload.get("schema_version") != RECIPE_PAYLOAD_SCHEMA
            or payload.get("provider") != "recipe-image-v1"
        ):
            raise RecipeExecutionError("recovery_invalid_payload")
        source_artifact_id = _safe_id(payload.get("source_artifact_id"), "source_artifact_id")
        plan_payload = payload.get("plan")
        try:
            plan = parse_image_transform(plan_payload if isinstance(plan_payload, Mapping) else {})
        except RecipeValidationError:
            raise RecipeExecutionError("recovery_invalid_payload") from None
        if payload.get("plan_digest") != plan.digest():
            raise RecipeExecutionError("recovery_invalid_payload")
        retention = payload.get("retention_seconds", DEFAULT_RECIPE_RETENTION_SECONDS)
        try:
            return RecipeImageRequest(
                owner=job.owner,
                request_id=job.request_id,
                source_artifact_id=source_artifact_id,
                plan=plan,
                # Validated by RecipeImageRequest; the store holds plain JSON.
                retention_seconds=retention,
            )
        except (TypeError, ValueError, RecipeExecutionError):
            raise RecipeExecutionError("recovery_invalid_payload") from None

    def _load_input(self, owner: str, artifact_id: str) -> bytes:
        artifact = self.repository.get_artifact(artifact_id, owner=owner)
        if artifact is None:
            raise RecipeExecutionError("input_artifact_unavailable")
        if artifact.mime_type not in _MIME_TO_FORMAT:
            raise RecipeExecutionError("input_artifact_invalid")
        try:
            content = self.repository.read_artifact(artifact.artifact_id)
            detected = sniff_artifact_mime(content)
        except (ExecutionRepositoryError, ArtifactBoundaryError):
            raise RecipeExecutionError("input_artifact_unavailable") from None
        if detected != artifact.mime_type or len(content) > MAX_INPUT_BYTES:
            raise RecipeExecutionError("input_artifact_invalid")
        return content

    def _launch(self, job_id: str, request: RecipeImageRequest) -> None:
        with self._lock:
            existing = self._threads.get(job_id)
            if existing is not None and existing.is_alive():
                return
            event = self._cancel_events.setdefault(job_id, Event())
            thread = Thread(
                target=self._run,
                args=(job_id, request, event),
                name=f"cortex-recipe-{job_id}",
                daemon=True,
            )
            self._threads[job_id] = thread
            thread.start()

    @staticmethod
    def _write_staging(root: Path, content: bytes) -> None:
        path = root / "output"
        try:
            with path.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise RecipeExecutionError("artifact_staging_failed") from None

    def _publish(
        self,
        job: ExecutionJob,
        output: RecipeWorkerOutput,
        retention_seconds: int,
    ) -> tuple[PublishedArtifact, ...]:
        if len(output.content) > self.repository.max_artifact_bytes:
            raise RecipeExecutionError("worker_output_too_large")
        try:
            with tempfile.TemporaryDirectory(
                prefix=f".recipe-{job.job_id}-", dir=str(self.repository.artifact_root)
            ) as staging:
                root = Path(staging)
                self._write_staging(root, output.content)
                return self.artifact_boundary.collect_outputs(
                    job.job_id,
                    job.owner,
                    root,
                    (OutputClaim("output", output.mime_type),),
                    retention_seconds=retention_seconds,
                )
        except ArtifactBoundaryError:
            raise RecipeExecutionError("artifact_publication_failed") from None

    @staticmethod
    def _result(output: RecipeWorkerOutput, published: PublishedArtifact, plan: ImageTransformPlan) -> Mapping[str, Any]:
        artifact = published.artifact
        return {
            "schema_version": RECIPE_RESULT_SCHEMA,
            "artifact_id": artifact.artifact_id,
            "mime_type": output.mime_type,
            "format": output.format,
            "size": artifact.size,
            "sha256": artifact.sha256,
            "width": output.width,
            "height": output.height,
            "plan_digest": plan.digest(),
        }

    def _delete_published(self, published: tuple[PublishedArtifact, ...]) -> None:
        for item in published:
            try:
                self.repository.delete_artifact(item.artifact.artifact_id)
            except ExecutionRepositoryError:
                raise RecipeExecutionError("artifact_cleanup_pending") from None

    def _run(self, job_id: str, request: RecipeImageRequest, cancel_event: Event) -> None:
        lease_owner = f"recipe-coordinator-{uuid4().hex}"
        attempt: RecipeWorkerAttempt | None = None
        with self._lock:
            self._cancel_events[job_id] = cancel_event
        published: tuple[PublishedArtifact, ...] = ()
        try:
            self.repository.claim_lease(job_id, lease_owner=lease_owner, ttl_seconds=self.lease_seconds)
            current = self.repository.get_job(job_id)
            if current is None:
                return
            if current.status == "cancelling" or cancel_event.is_set():
                self.repository.transition(
                    job_id,
                    status="cancelled",
                    event="cancelled",
                    phase="cancelled",
                    data={"message": "Recipe cancelled before start."},
                    error="cancelled",
                )
                return
            self.repository.transition(
                job_id,
                status="running",
                event="started",
                phase="prepare",
                data={"message": "Image recipe started."},
            )
            content = self._load_input(request.owner, request.source_artifact_id)
            self.repository.transition(
                job_id,
                status="running",
                event="progress",
                phase="worker",
                data={"message": "Image recipe worker is processing the staged artifact."},
            )
            current = self.repository.get_job(job_id) or current
            attempt = self.worker_factory(current)
            if not all(callable(getattr(attempt, name, None)) for name in ("transform", "cancel", "close")):
                raise RecipeExecutionError("worker_attempt_invalid")
            with self._lock:
                self._attempts[job_id] = attempt
            output = attempt.transform(
                request.request_id,
                job_id,
                request.plan,
                content,
                cancel_event,
            )
            if not isinstance(output, RecipeWorkerOutput):
                raise RecipeExecutionError("worker_output_invalid")
            current = self.repository.get_job(job_id)
            if cancel_event.is_set() or (current is not None and current.status == "cancelling"):
                raise RecipeExecutionError("cancelled")
            if current is None:
                raise RecipeExecutionError("coordinator_failed")
            published = self._publish(current, output, request.retention_seconds)
            current = self.repository.get_job(job_id)
            if cancel_event.is_set() or (current is not None and current.status == "cancelling"):
                self._delete_published(published)
                published = ()
                raise RecipeExecutionError("cancelled")
            try:
                self.repository.transition(
                    job_id,
                    status="succeeded",
                    event="completed",
                    phase="completed",
                    data={"message": "Image recipe completed."},
                    result=self._result(output, published[0], request.plan),
                    # The check above is a read. A Stop committing between it
                    # and this write would otherwise be overwritten, leaving
                    # the user told that cancelled work succeeded -- with an
                    # artifact they never accepted still published.
                    expected_status="running",
                )
            except ExecutionTransitionConflict:
                self._delete_published(published)
                published = ()
                raise RecipeExecutionError("cancelled") from None
        except RecipeExecutionError as execution_error:
            failure_code = execution_error.code
            if published:
                try:
                    self._delete_published(published)
                except RecipeExecutionError:
                    failure_code = "artifact_cleanup_pending"
            current = self.repository.get_job(job_id)
            cancelled = failure_code == "cancelled" or cancel_event.is_set() or (current is not None and current.status == "cancelling")
            try:
                self.repository.transition(
                    job_id,
                    status="cancelled" if cancelled else "failed",
                    event="cancelled" if cancelled else "failed",
                    phase="cancelled" if cancelled else "failed",
                    data={"message": "Image recipe was cancelled." if cancelled else "Image recipe failed safely."},
                    error="cancelled" if cancelled else failure_code,
                )
            except Exception:
                pass
        except LeaseConflict:
            self._fail_recovery(job_id, "lease_unavailable")
        except Exception:
            current = self.repository.get_job(job_id)
            cancelled = cancel_event.is_set() or (current is not None and current.status == "cancelling")
            try:
                self.repository.transition(
                    job_id,
                    status="cancelled" if cancelled else "failed",
                    event="cancelled" if cancelled else "failed",
                    phase="cancelled" if cancelled else "failed",
                    data={"message": "Image recipe was cancelled." if cancelled else "Image recipe failed safely."},
                    error="cancelled" if cancelled else "coordinator_failed",
                )
            except Exception:
                pass
        finally:
            if attempt is not None:
                try:
                    attempt.close()
                except Exception:
                    pass
            with self._lock:
                self._attempts.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._threads.pop(job_id, None)
            try:
                self.repository.release_lease(job_id, lease_owner=lease_owner)
            except Exception:
                pass


__all__ = [
    "DEFAULT_CANCEL_GRACE_SECONDS",
    "DEFAULT_RECIPE_RETENTION_SECONDS",
    "DEFAULT_WORKER_TIMEOUT_SECONDS",
    "MAX_RECIPE_RETENTION_SECONDS",
    "RecipeExecutionCoordinator",
    "RecipeExecutionError",
    "RecipeImageRequest",
    "RecipeWorkerAttempt",
    "RecipeWorkerAttemptFactory",
    "RecipeWorkerOutput",
    "RECIPE_IMAGE_PROFILE",
    "RECIPE_PAYLOAD_SCHEMA",
    "RECIPE_RESULT_SCHEMA",
]
