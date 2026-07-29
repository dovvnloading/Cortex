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
import base64
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import tempfile
from threading import Event, Lock, Thread
import time
from typing import Any, Protocol
from uuid import uuid4

from .artifact_boundary import (
    ArtifactBoundary,
    ArtifactBoundaryError,
    OutputClaim,
    PublishedArtifact,
    sniff_artifact_mime,
)
from .broker import BrokerMessage
from .models import ExecutionJob, TerminalExecutionStatus
from .recipes import ImageTransformPlan, RecipeValidationError, parse_image_transform
from .repository import (
    ExecutionRepository,
    ExecutionRepositoryError,
    LeaseConflict,
)
from .worker_protocol import (
    MAX_WORKER_CHUNK_BYTES,
    MAX_WORKER_INPUT_BYTES,
    MAX_WORKER_OUTPUT_BYTES,
    WorkerAck,
    WorkerCancel,
    WorkerCollect,
    WorkerError,
    WorkerInputChunk,
    WorkerInputComplete,
    WorkerOutputChunk,
    WorkerPrepare,
    WorkerProtocolError,
    WorkerResult,
)


RECIPE_IMAGE_PROFILE = "recipe.image.v1"
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
        if len(self.content) > MAX_WORKER_OUTPUT_BYTES:
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


class RecipeWorkerConnection(Protocol):
    """The narrow surface of an authenticated broker connection."""

    def send_message(self, message: BrokerMessage) -> None:
        ...

    def receive_message(self) -> BrokerMessage:
        ...

    def close(self) -> None:
        ...


class _ResponseReader:
    """Make a blocking broker receive cancellable without changing the transport."""

    def __init__(self, connection: RecipeWorkerConnection) -> None:
        self.connection = connection
        self.items: Queue[Any] = Queue(maxsize=16)
        self.stop_event = Event()
        self.thread = Thread(target=self._run, name="cortex-recipe-worker-reader", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.items.put(self.connection.receive_message())
            except Exception as error:
                if not self.stop_event.is_set():
                    try:
                        self.items.put(error, timeout=0.1)
                    except Exception:
                        pass
                return

    def next(self, timeout: float) -> Any:
        try:
            return self.items.get(timeout=max(0.001, timeout))
        except Empty:
            return None

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.connection.close()
        except Exception:
            pass
        self.thread.join(timeout=1.0)


class RecipeWorkerClient:
    """Client for the authenticated worker protocol, adapted as one attempt.

    The client accepts only in-memory bytes and typed plans.  It validates every
    response envelope, chunk offset, digest, MIME claim, and terminal condition.
    """

    def __init__(
        self,
        connection: RecipeWorkerConnection,
        *,
        installation_principal_id: str,
        timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
    ) -> None:
        if not all(callable(getattr(connection, name, None)) for name in ("send_message", "receive_message", "close")):
            raise TypeError("worker client requires an authenticated broker connection")
        _safe_id(installation_principal_id, "installation_principal_id")
        if re.fullmatch(r"[0-9a-f]{64}", installation_principal_id) is None:
            raise ValueError("installation_principal_id must be a broker principal")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0.1 <= timeout_seconds <= 600:
            raise ValueError("timeout_seconds is outside the bounded worker limit")
        if not isinstance(cancel_grace_seconds, (int, float)) or isinstance(cancel_grace_seconds, bool) or not 0.1 <= cancel_grace_seconds <= 30:
            raise ValueError("cancel_grace_seconds is outside the bounded worker limit")
        self._connection = connection
        self._principal = installation_principal_id
        self._timeout = float(timeout_seconds)
        self._cancel_grace = float(cancel_grace_seconds)
        self._reader: _ResponseReader | None = None
        self._active_cancel: Event | None = None
        self._cancel_sent = False
        self._started = False
        self._closed = False
        self._lock = Lock()

    @property
    def principal_id(self) -> str:
        return self._principal

    def _message(self, operation: str, request_id: str, job_id: str, model: Any) -> BrokerMessage:
        return BrokerMessage(
            schema_version="broker.message.v1",
            direction="to_executor",
            operation=operation,
            request_id=request_id,
            job_id=job_id,
            installation_principal_id=self._principal,
            body=model.model_dump(mode="json"),
        )

    def _send(self, message: BrokerMessage) -> None:
        try:
            self._connection.send_message(message)
        except Exception:
            raise RecipeExecutionError("worker_transport_failed") from None

    def _validate_response(self, message: Any, request_id: str, job_id: str, operation: str) -> Any:
        if not isinstance(message, BrokerMessage):
            raise RecipeExecutionError("worker_message_invalid")
        if (
            message.direction != "to_broker"
            or message.operation not in {operation, "cancel"}
            or message.request_id != request_id
            or message.job_id != job_id
            or message.installation_principal_id != self._principal
        ):
            raise RecipeExecutionError("worker_identity_mismatch")
        body = message.body
        if not isinstance(body, dict):
            raise RecipeExecutionError("worker_message_invalid")
        schema = body.get("schema_version")
        try:
            if schema == "recipe.worker.ack.v1":
                return WorkerAck.model_validate(body)
            if schema == "recipe.worker.result.v1":
                return WorkerResult.model_validate(body)
            if schema == "recipe.worker.error.v1":
                return WorkerError.model_validate(body)
            if schema == "recipe.worker.output_chunk.v1":
                return WorkerOutputChunk.model_validate(body)
        except (TypeError, ValueError):
            raise RecipeExecutionError("worker_message_invalid") from None
        raise RecipeExecutionError("worker_message_invalid")

    def _send_cancel(self, request_id: str, job_id: str, reason: str) -> None:
        if self._cancel_sent:
            return
        if reason not in {"user", "timeout", "shutdown"}:
            reason = "user"
        self._cancel_sent = True
        self._send(
            self._message(
                "cancel",
                request_id,
                job_id,
                WorkerCancel(
                    schema_version="recipe.worker.cancel.v1",
                    request_id=request_id,
                    job_id=job_id,
                    reason=reason,
                ),
            )
        )

    def _wait(
        self,
        *,
        request_id: str,
        job_id: str,
        operation: str,
        expected: tuple[type[Any], ...],
        cancel_event: Event,
        deadline: float,
    ) -> Any:
        if self._reader is None:
            raise RecipeExecutionError("worker_transport_failed")
        cancel_deadline: float | None = None
        while True:
            now = time.monotonic()
            if cancel_event.is_set() and not self._cancel_sent:
                self._send_cancel(request_id, job_id, "user")
                cancel_deadline = now + self._cancel_grace
            elif not self._cancel_sent and now >= deadline:
                self._send_cancel(request_id, job_id, "timeout")
                cancel_deadline = now + self._cancel_grace
            if self._cancel_sent and cancel_deadline is not None and now >= cancel_deadline:
                raise RecipeExecutionError("cancelled" if cancel_event.is_set() else "worker_timeout")
            wait_for = 0.05
            if not self._cancel_sent:
                wait_for = min(wait_for, max(0.001, deadline - now))
            elif cancel_deadline is not None:
                wait_for = min(wait_for, max(0.001, cancel_deadline - now))
            item = self._reader.next(wait_for)
            if item is None:
                continue
            if isinstance(item, BaseException):
                raise RecipeExecutionError("cancelled" if self._cancel_sent and cancel_event.is_set() else "worker_transport_failed")
            response = self._validate_response(item, request_id, job_id, operation)
            if isinstance(response, WorkerError):
                if response.code == "cancelled" or self._cancel_sent and cancel_event.is_set():
                    raise RecipeExecutionError("cancelled")
                if response.code == "timeout":
                    raise RecipeExecutionError("worker_timeout")
                raise RecipeExecutionError(response.code)
            if self._cancel_sent:
                if isinstance(response, WorkerAck) and response.acknowledged_operation == "cancel":
                    raise RecipeExecutionError("cancelled" if cancel_event.is_set() else "worker_timeout")
                continue
            if not isinstance(response, expected):
                raise RecipeExecutionError("worker_response_unexpected")
            return response

    def transform(
        self,
        request_id: str,
        job_id: str,
        plan: ImageTransformPlan,
        content: bytes,
        cancel_event: Event,
    ) -> RecipeWorkerOutput:
        if self._started or self._closed:
            raise RecipeExecutionError("worker_attempt_reused")
        _safe_id(request_id, "request_id")
        _safe_id(job_id, "job_id")
        if not isinstance(plan, ImageTransformPlan):
            raise RecipeExecutionError("invalid_plan")
        if not isinstance(content, bytes) or not 1 <= len(content) <= MAX_WORKER_INPUT_BYTES:
            raise RecipeExecutionError("input_too_large" if isinstance(content, bytes) else "invalid_input")
        if not isinstance(cancel_event, Event):
            raise TypeError("cancel_event must be a threading.Event")
        self._started = True
        self._active_cancel = cancel_event
        self._cancel_sent = False
        self._reader = _ResponseReader(self._connection)
        self._reader.start()
        digest = sha256(content).hexdigest()
        try:
            try:
                input_mime = sniff_artifact_mime(content)
            except ArtifactBoundaryError:
                raise RecipeExecutionError("invalid_input") from None
            if input_mime not in _MIME_TO_FORMAT:
                raise RecipeExecutionError("invalid_input")
            prepare = WorkerPrepare(
                schema_version="recipe.worker.prepare.v1",
                request_id=request_id,
                job_id=job_id,
                plan=plan,
                input_size=len(content),
                input_sha256=digest,
                input_mime_type=input_mime,
            )
            deadline = time.monotonic() + self._timeout
            self._send(self._message("prepare", request_id, job_id, prepare))
            self._wait(
                request_id=request_id,
                job_id=job_id,
                operation="prepare",
                expected=(WorkerAck,),
                cancel_event=cancel_event,
                deadline=deadline,
            )
            for offset in range(0, len(content), MAX_WORKER_CHUNK_BYTES):
                chunk = content[offset : offset + MAX_WORKER_CHUNK_BYTES]
                message = WorkerInputChunk(
                    schema_version="recipe.worker.input_chunk.v1",
                    request_id=request_id,
                    job_id=job_id,
                    offset=offset,
                    data=base64.urlsafe_b64encode(chunk).decode("ascii").rstrip("="),
                    sha256=sha256(chunk).hexdigest(),
                )
                self._send(self._message("input_chunk", request_id, job_id, message))
                self._wait(
                    request_id=request_id,
                    job_id=job_id,
                    operation="input_chunk",
                    expected=(WorkerAck,),
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
            complete = WorkerInputComplete(
                schema_version="recipe.worker.input_complete.v1",
                request_id=request_id,
                job_id=job_id,
                input_size=len(content),
                input_sha256=digest,
            )
            self._send(self._message("input_complete", request_id, job_id, complete))
            result = self._wait(
                request_id=request_id,
                job_id=job_id,
                operation="input_complete",
                expected=(WorkerResult,),
                cancel_event=cancel_event,
                deadline=deadline,
            )
            assert isinstance(result, WorkerResult)
            output = bytearray()
            while len(output) < result.output_size:
                collect = WorkerCollect(
                    schema_version="recipe.worker.collect.v1",
                    request_id=request_id,
                    job_id=job_id,
                    offset=len(output),
                    max_bytes=MAX_WORKER_CHUNK_BYTES,
                )
                self._send(self._message("collect", request_id, job_id, collect))
                chunk = self._wait(
                    request_id=request_id,
                    job_id=job_id,
                    operation="collect",
                    expected=(WorkerOutputChunk,),
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
                assert isinstance(chunk, WorkerOutputChunk)
                if chunk.offset != len(output):
                    raise RecipeExecutionError("worker_output_offset_invalid")
                try:
                    decoded = chunk.decoded()
                except WorkerProtocolError:
                    raise RecipeExecutionError("worker_output_chunk_invalid") from None
                if not decoded or len(output) + len(decoded) > result.output_size:
                    raise RecipeExecutionError("worker_output_size_mismatch")
                output.extend(decoded)
                if chunk.final and len(output) != result.output_size:
                    raise RecipeExecutionError("worker_output_size_mismatch")
            if not output or len(output) != result.output_size or not chunk.final:
                raise RecipeExecutionError("worker_output_size_mismatch")
            content_out = bytes(output)
            digest_out = sha256(content_out).hexdigest()
            if digest_out != result.output_sha256:
                raise RecipeExecutionError("worker_output_hash_mismatch")
            if result.mime_type not in _MIME_TO_FORMAT or _MIME_TO_FORMAT[result.mime_type] != result.format:
                raise RecipeExecutionError("worker_output_mime_mismatch")
            return RecipeWorkerOutput(
                content=content_out,
                mime_type=result.mime_type,
                format=result.format,
                width=result.width,
                height=result.height,
                sha256=digest_out,
            )
        except RecipeExecutionError:
            raise
        except (TypeError, ValueError, WorkerProtocolError):
            raise RecipeExecutionError("worker_protocol_failed") from None

    def cancel(self, reason: str = "user") -> None:
        if reason not in {"user", "timeout", "shutdown"}:
            reason = "user"
        active = self._active_cancel
        if active is not None:
            active.set()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            self._reader.close()
        else:
            try:
                self._connection.close()
            except Exception:
                pass


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

        The normal qualification lifecycle owns its supervisor lease itself.
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
        retention = payload.get("retention_seconds")
        try:
            return RecipeImageRequest(
                owner=job.owner,
                request_id=job.request_id,
                source_artifact_id=source_artifact_id,
                plan=plan,
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
        if detected != artifact.mime_type or len(content) > MAX_WORKER_INPUT_BYTES:
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
            self.repository.transition(
                job_id,
                status="succeeded",
                event="completed",
                phase="completed",
                data={"message": "Image recipe completed."},
                result=self._result(output, published[0], request.plan),
            )
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
    "RecipeWorkerClient",
    "RecipeWorkerConnection",
    "RecipeWorkerOutput",
    "RECIPE_IMAGE_PROFILE",
    "RECIPE_PAYLOAD_SCHEMA",
    "RECIPE_RESULT_SCHEMA",
]
