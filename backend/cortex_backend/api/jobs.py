"""Owned, replayable job lifecycle used by the local API."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
import logging
from threading import Event, RLock
from typing import Any, Literal
from uuid import uuid4

from cortex_backend.services.progress import ProgressEvent


JobKind = Literal["generation", "models"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
EventKind = Literal["state", "progress", "completed", "error"]
TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {"succeeded", "failed", "cancelled"}
)


class JobConflict(RuntimeError):
    """Raised when the single active job for a kind already exists."""


class JobNotFound(RuntimeError):
    """Raised when a job ID is unknown."""


class JobOwnershipError(RuntimeError):
    """Raised when a session accesses another session's job."""


@dataclass(frozen=True, slots=True)
class JobEvent:
    """One ordered, safe event retained for SSE replay."""

    sequence: int
    job_id: str
    kind: EventKind
    status: JobStatus
    phase: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    """Public job state without exposing worker internals."""

    job_id: str
    kind: JobKind
    owner: str
    thread_id: str | None
    status: JobStatus
    sequence: int
    error: str | None = None


JobRunner = Callable[["JobProgressSink", Event], Any]
JobSerializer = Callable[[Any], Mapping[str, Any]]


@dataclass
class _JobRecord:
    job_id: str
    kind: JobKind
    owner: str
    thread_id: str | None
    request_id: str | None = None
    cancel_event: Event = field(default_factory=Event)
    status: JobStatus = "queued"
    sequence: int = 0
    error: str | None = None
    events: list[JobEvent] = field(default_factory=list)
    task: asyncio.Task[Any] | None = None


class JobProgressSink:
    """Adapt typed service progress into this job's retained event stream."""

    def __init__(self, registry: "JobRegistry", record: _JobRecord):
        self._registry = registry
        self._record = record

    def publish(self, event: ProgressEvent) -> None:
        self.publish_progress(event.phase, event.message)

    def publish_progress(self, phase: str, message: str) -> None:
        """Publish a safe progress message for generation or model work."""
        with self._registry._lock:
            if self._record.status in TERMINAL_STATUSES:
                return
            self._registry._append_event(
                self._record,
                kind="progress",
                status="running",
                phase=phase,
                data={"message": message},
            )


class JobRegistry:
    """Run at most one job of each kind and retain its ordered event history.

    Workers are synchronous because the existing model and persistence services
    are synchronous. They run in a thread owned by the event loop, while the
    registry itself remains the single authority for lifecycle transitions.
    """

    def __init__(self, *, poll_seconds: float = 0.025, max_terminal_jobs: int = 100):
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if max_terminal_jobs <= 0:
            raise ValueError("max_terminal_jobs must be positive")
        self._poll_seconds = poll_seconds
        self._max_terminal_jobs = max_terminal_jobs
        self._records: dict[str, _JobRecord] = {}
        self._active: dict[JobKind, str] = {}
        self._request_index: dict[tuple[JobKind, str, str], str] = {}
        self._lock = RLock()

    async def start(
        self,
        *,
        kind: JobKind,
        owner: str,
        thread_id: str | None,
        runner: JobRunner,
        serialize_result: JobSerializer | None = None,
        request_id: str | None = None,
    ) -> JobSnapshot:
        """Queue a worker with bounded terminal replay and request dedupe."""
        with self._lock:
            self._prune_terminal_records()
            if request_id:
                existing_id = self._request_index.get((kind, owner, request_id))
                existing = self._records.get(existing_id) if existing_id else None
                if existing is not None:
                    return self._snapshot(existing)
            active_id = self._active.get(kind)
            if active_id is not None:
                active = self._records.get(active_id)
                if active is not None and active.status not in TERMINAL_STATUSES:
                    raise JobConflict(f"A {kind} job is already active.")
                self._active.pop(kind, None)

            record = _JobRecord(
                job_id=uuid4().hex,
                kind=kind,
                owner=owner,
                thread_id=thread_id,
                request_id=request_id,
            )
            self._records[record.job_id] = record
            self._active[kind] = record.job_id
            if request_id:
                self._request_index[(kind, owner, request_id)] = record.job_id
            self._append_event(
                record,
                kind="state",
                status="queued",
                data={"message": "Job queued."},
            )
            record.task = asyncio.create_task(
                self._run(record, runner, serialize_result),
                name=f"cortex-{kind}-{record.job_id}",
            )
            return self._snapshot(record)

    def status(self, job_id: str, *, owner: str) -> JobSnapshot:
        record = self._owned_record(job_id, owner)
        with self._lock:
            return self._snapshot(record)

    def cancel(self, job_id: str, *, owner: str) -> JobSnapshot:
        record = self._owned_record(job_id, owner)
        with self._lock:
            if record.status not in TERMINAL_STATUSES:
                record.cancel_event.set()
                record.error = "Job cancelled."
                self._append_event(
                    record,
                    kind="state",
                    status="cancelled",
                    data={"message": "Job cancelled."},
                )
            return self._snapshot(record)

    async def events(
        self,
        job_id: str,
        *,
        owner: str,
        after_sequence: int = 0,
    ):
        """Yield retained and newly published events in sequence order."""
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        record = self._owned_record(job_id, owner)
        cursor = after_sequence
        while True:
            with self._lock:
                pending = [event for event in record.events if event.sequence > cursor]
                terminal = record.status in TERMINAL_STATUSES
            if pending:
                for event in pending:
                    cursor = event.sequence
                    yield event
                continue
            if terminal:
                return
            await asyncio.sleep(self._poll_seconds)

    async def shutdown(self) -> None:
        """Request cancellation and stop registry tasks during app shutdown."""
        with self._lock:
            records = [
                record
                for record in self._records.values()
                if record.status not in TERMINAL_STATUSES
            ]
            for record in records:
                record.cancel_event.set()
                self._append_event(
                    record,
                    kind="state",
                    status="cancelled",
                    data={"message": "Job cancelled during shutdown."},
                )
            tasks = [record.task for record in self._records.values() if record.task]
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(
        self,
        record: _JobRecord,
        runner: JobRunner,
        serialize_result: JobSerializer | None,
    ) -> None:
        try:
            with self._lock:
                if record.status in TERMINAL_STATUSES:
                    return
                self._append_event(
                    record,
                    kind="state",
                    status="running",
                    data={"message": "Job started."},
                )

            result = await asyncio.to_thread(
                runner,
                JobProgressSink(self, record),
                record.cancel_event,
            )
            with self._lock:
                if record.status in TERMINAL_STATUSES:
                    return
                if record.cancel_event.is_set():
                    record.error = "Job cancelled."
                    self._append_event(
                        record,
                        kind="state",
                        status="cancelled",
                        data={"message": "Job cancelled."},
                    )
                    return
                data = dict(
                    serialize_result(result) if serialize_result else _serialize(result)
                )
                self._append_event(
                    record,
                    kind="completed",
                    status="succeeded",
                    data=data,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with self._lock:
                if record.status in TERMINAL_STATUSES:
                    return
                logging.error(
                    "Cortex %s job failed (%s).", record.kind, type(exc).__name__
                )
                message = (
                    getattr(exc, "user_message", None)
                    or "Job failed. Please try again."
                )
                record.error = str(message)
                self._append_event(
                    record,
                    kind="error",
                    status="failed",
                    data={"message": record.error, "details": type(exc).__name__},
                )
        finally:
            with self._lock:
                if self._active.get(record.kind) == record.job_id:
                    self._active.pop(record.kind, None)

    def _owned_record(self, job_id: str, owner: str) -> _JobRecord:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                raise JobNotFound("Job not found.")
            if record.owner != owner:
                raise JobOwnershipError("Job does not belong to this session.")
            return record

    def _prune_terminal_records(self) -> None:
        terminal = [
            record
            for record in self._records.values()
            if record.status in TERMINAL_STATUSES
            and (record.task is None or record.task.done())
        ]
        excess = len(terminal) - self._max_terminal_jobs
        for record in terminal[: max(0, excess)]:
            self._records.pop(record.job_id, None)
            if record.request_id:
                self._request_index.pop(
                    (record.kind, record.owner, record.request_id), None
                )

    def _append_event(
        self,
        record: _JobRecord,
        *,
        kind: EventKind,
        status: JobStatus,
        phase: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> JobEvent:
        record.sequence += 1
        record.status = status
        event = JobEvent(
            sequence=record.sequence,
            job_id=record.job_id,
            kind=kind,
            status=status,
            phase=phase,
            data=dict(data or {}),
        )
        record.events.append(event)
        return event

    @staticmethod
    def _snapshot(record: _JobRecord) -> JobSnapshot:
        return JobSnapshot(
            job_id=record.job_id,
            kind=record.kind,
            owner=record.owner,
            thread_id=record.thread_id,
            status=record.status,
            sequence=record.sequence,
            error=record.error,
        )


def _serialize(result: Any) -> Mapping[str, Any]:
    if isinstance(result, Mapping):
        return result
    if is_dataclass(result):
        return asdict(result)
    return {"result": result}
