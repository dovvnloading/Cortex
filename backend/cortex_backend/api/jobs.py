"""Owned, replayable job lifecycle used by the local API."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
import json
import logging
from threading import Event, RLock
from typing import Any, Literal
from uuid import uuid4

from cortex_backend.services.progress import ProgressEvent


JobKind = Literal["generation", "models", "gguf_download"]
JobStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
]
EventKind = Literal["state", "progress", "completed", "error"]
TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {"succeeded", "failed", "cancelled"}
)

# In-memory SSE history is intentionally bounded independently of the number
# of terminal jobs.  ``max_event_bytes`` measures the UTF-8 JSON size of each
# retained event's ``data`` mapping; sequence/status metadata remains available
# in the event object and the job snapshot even when an event payload is
# compacted.
DEFAULT_MAX_EVENT_COUNT = 256
DEFAULT_MAX_EVENT_BYTES = 1_048_576
_EVENT_DATA_TRUNCATED = "Event data omitted because it exceeded the retention limit."


class JobConflict(RuntimeError):
    """Raised when the single active job for a kind already exists."""


class JobRegistryClosed(JobConflict):
    """Raised when new work is submitted during or after shutdown."""


class JobNotFound(RuntimeError):
    """Raised when a job ID is unknown."""


class JobOwnershipError(RuntimeError):
    """Raised when a session accesses another session's job."""


@dataclass(frozen=True, slots=True)
class JobEvent:
    """One ordered, safe event retained for SSE replay."""

    sequence: int
    job_id: str
    thread_id: str | None
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
    result: Mapping[str, Any] | None = None


JobRunner = Callable[["JobProgressSink", Event], Any]
JobSerializer = Callable[[Any], Mapping[str, Any]]
JobPreparation = Callable[[], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class JobReservation:
    """Atomic admission result for work that needs asynchronous preparation."""

    snapshot: JobSnapshot
    created: bool
    token: str | None = None


@dataclass
class _JobRecord:
    job_id: str
    kind: JobKind
    owner: str
    thread_id: str | None
    request_id: str | None = None
    request_fingerprint: str | None = None
    reservation_token: str | None = None
    acceptance: Mapping[str, Any] = field(default_factory=dict)
    # A start_reserved call has claimed this reservation and is running its
    # preparation off the event loop.  No worker task exists yet, but one is
    # coming: cancellation must defer finalization to that call instead of
    # treating the record as work that never began.
    starting: bool = False
    prepared: bool = False
    preparation_error: str | None = None
    cancel_event: Event = field(default_factory=Event)
    commit_started: bool = False
    status: JobStatus = "queued"
    sequence: int = 0
    error: str | None = None
    result: Mapping[str, Any] | None = None
    events: list[JobEvent] = field(default_factory=list)
    event_bytes: int = 0
    task: asyncio.Task[Any] | None = None


class JobProgressSink:
    """Adapt typed service progress into this job's retained event stream."""

    def __init__(self, registry: JobRegistry, record: _JobRecord):
        self._registry = registry
        self._record = record

    def publish(self, event: ProgressEvent) -> None:
        self.publish_progress(event.phase, event.message)

    def publish_progress(
        self,
        phase: str,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        """Publish a safe progress message for generation or model work."""
        payload = {"message": message, **dict(data or {})}
        self.publish_event("progress", phase=phase, data=payload)

    def begin_commit(
        self,
        phase: str,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> bool:
        """Atomically cross the point after which cancellation is too late.

        The worker must call this immediately before its first durable result
        mutation.  Sharing the registry lock with :meth:`JobRegistry.cancel`
        gives the two operations one unambiguous order: cancellation wins and
        this returns ``False``, or commit wins and later cancellation is inert.
        """
        payload = {"message": message, **dict(data or {})}
        with self._registry._lock:
            if (
                self._record.status != "running"
                or self._record.cancel_event.is_set()
            ):
                return False
            if self._record.commit_started:
                return True
            self._record.commit_started = True
            self._registry._append_event(
                self._record,
                kind="progress",
                status="running",
                phase=phase,
                data=payload,
            )
            return True

    def publish_event(
        self,
        kind: EventKind,
        *,
        phase: str | None = None,
        data: Mapping[str, Any] | None = None,
        status: JobStatus = "running",
    ) -> None:
        """Publish a typed event while the owning job is still active."""
        with self._registry._lock:
            # A cancellation request is intentionally non-terminal while the
            # synchronous worker unwinds. Do not let callbacks from that
            # worker turn the record back into ``running`` or add stale output
            # after the cancellation state has been published.
            if (
                self._record.status != "running"
                or self._record.cancel_event.is_set()
            ):
                return
            self._registry._append_event(
                self._record,
                kind=kind,
                status=status,
                phase=phase,
                data=data,
            )


class JobRegistry:
    """Run at most one job of each kind and retain its ordered event history.

    Workers are synchronous because the existing model and persistence services
    are synchronous. They run in a thread owned by the event loop, while the
    registry itself remains the single authority for lifecycle transitions.
    """

    def __init__(
        self,
        *,
        poll_seconds: float = 0.025,
        max_terminal_jobs: int = 100,
        shutdown_grace_seconds: float = 10.0,
        max_event_count: int = DEFAULT_MAX_EVENT_COUNT,
        max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
    ):
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if max_terminal_jobs <= 0:
            raise ValueError("max_terminal_jobs must be positive")
        if shutdown_grace_seconds <= 0:
            raise ValueError("shutdown_grace_seconds must be positive")
        if (
            isinstance(max_event_count, bool)
            or not isinstance(max_event_count, int)
            or max_event_count <= 0
        ):
            raise ValueError("max_event_count must be positive")
        if (
            isinstance(max_event_bytes, bool)
            or not isinstance(max_event_bytes, int)
            or max_event_bytes <= 0
        ):
            raise ValueError("max_event_bytes must be positive")
        self._poll_seconds = poll_seconds
        self._max_terminal_jobs = max_terminal_jobs
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._max_event_count = max_event_count
        self._max_event_bytes = max_event_bytes
        self._records: dict[str, _JobRecord] = {}
        self._active: dict[JobKind, str] = {}
        self._request_index: dict[tuple[JobKind, str, str], str] = {}
        self._lock = RLock()
        self._accepting = True

    async def start(
        self,
        *,
        kind: JobKind,
        owner: str,
        thread_id: str | None,
        runner: JobRunner,
        serialize_result: JobSerializer | None = None,
        request_id: str | None = None,
        request_fingerprint: str | None = None,
    ) -> JobSnapshot:
        """Queue a worker with bounded terminal replay and request dedupe."""
        reservation = self.reserve(
            kind=kind,
            owner=owner,
            thread_id=thread_id,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
        )
        if not reservation.created:
            snapshot, _ = await self.wait_until_prepared(
                reservation.snapshot.job_id,
                owner=owner,
            )
            return snapshot
        snapshot, _ = await self.start_reserved(
            reservation,
            owner=owner,
            runner=runner,
            serialize_result=serialize_result,
        )
        return snapshot

    def reserve(
        self,
        *,
        kind: JobKind,
        owner: str,
        thread_id: str | None,
        request_id: str | None = None,
        request_fingerprint: str | None = None,
    ) -> JobReservation:
        """Atomically claim a job kind before asynchronous preparation begins."""
        if request_id and not request_fingerprint:
            raise ValueError("request_fingerprint is required with request_id")
        with self._lock:
            if not self._accepting:
                raise JobRegistryClosed("Cortex is shutting down.")
            self._prune_terminal_records()
            if request_id:
                existing_id = self._request_index.get((kind, owner, request_id))
                existing = self._records.get(existing_id) if existing_id else None
                if existing is not None:
                    if (
                        request_fingerprint is not None
                        and existing.request_fingerprint is not None
                        and request_fingerprint != existing.request_fingerprint
                    ):
                        raise JobConflict(
                            "This request ID was already used for a different payload."
                        )
                    return JobReservation(
                        snapshot=self._snapshot(existing),
                        created=False,
                    )
            active_id = self._active.get(kind)
            if active_id is not None:
                active = self._records.get(active_id)
                if active is not None and active.status not in TERMINAL_STATUSES:
                    raise JobConflict(f"A {kind} job is already active.")
                self._active.pop(kind, None)

            reservation_token = uuid4().hex
            record = _JobRecord(
                job_id=uuid4().hex,
                kind=kind,
                owner=owner,
                thread_id=thread_id,
                request_id=request_id,
                request_fingerprint=request_fingerprint,
                reservation_token=reservation_token,
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
            return JobReservation(
                snapshot=self._snapshot(record),
                created=True,
                token=reservation_token,
            )

    async def start_reserved(
        self,
        reservation: JobReservation,
        *,
        owner: str,
        runner: JobRunner,
        serialize_result: JobSerializer | None = None,
        prepare: JobPreparation | None = None,
    ) -> tuple[JobSnapshot, Mapping[str, Any]]:
        """Prepare side effects and start one previously reserved worker.

        Preparation is the last step before an accepted job becomes visible,
        and for a generation it writes the user's turn to the chat database.
        That write must not run on the event loop, and must not run while the
        registry lock is held: everything else in this registry -- a worker
        thread publishing progress, a cancel, an SSE poll -- takes the same
        lock, and the API is served by a single event loop that a synchronous
        disk write freezes outright.

        So preparation runs in three phases.  Phase one claims the
        reservation under the lock (in-memory only, microseconds).  Phase two
        runs the callback in a worker thread with no lock held.  Phase three
        re-checks the state the callback could not see and starts the worker.

        Between phases two and three the record carries ``starting``, which is
        the contract that replaces "the lock was held the whole time":
        :meth:`cancel` and :meth:`shutdown` see a record with no task yet, and
        must not finalize it themselves, because this call is going to start
        one.  Cancellation is still honoured -- phase three creates the task
        anyway and ``_run`` finalizes it on its first lock acquisition,
        exactly as it does for a cancel that arrives moments later.
        """
        if not reservation.created or not reservation.token:
            raise JobConflict("Only the reservation owner can start this job.")
        record = self._owned_record(reservation.snapshot.job_id, owner)

        # Phase one: claim the reservation.  Nothing here touches disk, so the
        # lock is held only for as long as a few dict lookups take.
        with self._lock:
            if not self._accepting:
                raise JobRegistryClosed("Cortex is shutting down.")
            if reservation.token != record.reservation_token:
                raise JobConflict("Job reservation is no longer valid.")
            if record.starting:
                raise JobConflict("Job preparation is already in progress.")
            if record.prepared or record.task is not None:
                raise JobConflict("Job preparation has already completed.")
            if record.status in TERMINAL_STATUSES or record.cancel_event.is_set():
                raise JobConflict("Job preparation was cancelled.")
            record.starting = True

        # Phase two: the callback may block on disk.  Off the loop, no lock.
        # The reservation token is deliberately still valid here, so a caller
        # whose preparation raises can still abort the reservation it holds.
        try:
            acceptance = dict(
                await asyncio.to_thread(prepare) if prepare is not None else {}
            )
        except BaseException:
            with self._lock:
                record.starting = False
            raise

        # Phase three: publish what preparation produced and start the worker.
        with self._lock:
            record.starting = False
            if record.status in TERMINAL_STATUSES:
                raise JobConflict("Job preparation was cancelled.")
            if not self._accepting:
                # Shutdown began while preparation was in flight.  It has
                # already collected the tasks it will await, so starting one
                # now would leave a worker running behind a closed registry.
                record.prepared = True
                record.reservation_token = None
                self._finalize_cancellation(record)
                if self._active.get(record.kind) == record.job_id:
                    self._active.pop(record.kind, None)
                raise JobRegistryClosed("Cortex is shutting down.")
            record.acceptance = acceptance
            record.prepared = True
            record.reservation_token = None
            record.task = asyncio.create_task(
                self._run(record, runner, serialize_result),
                name=f"cortex-{record.kind}-{record.job_id}",
            )
            return self._snapshot(record), dict(acceptance)

    def abort_reservation(
        self,
        reservation: JobReservation,
        *,
        owner: str,
        message: str = "Job preparation failed. Please try again.",
    ) -> JobSnapshot:
        """Fail a reservation that never reached its owned worker."""
        if not reservation.created or not reservation.token:
            raise JobConflict("Only the reservation owner can abort this job.")
        record = self._owned_record(reservation.snapshot.job_id, owner)
        with self._lock:
            if reservation.token != record.reservation_token:
                return self._snapshot(record)
            if record.prepared or record.task is not None:
                return self._snapshot(record)
            if record.status not in TERMINAL_STATUSES:
                record.preparation_error = message
                record.error = message
                record.prepared = True
                record.reservation_token = None
                self._append_event(
                    record,
                    kind="error",
                    status="failed",
                    data={"message": message, "details": "JobPreparationError"},
                )
            if self._active.get(record.kind) == record.job_id:
                self._active.pop(record.kind, None)
            if record.request_id:
                key = (record.kind, record.owner, record.request_id)
                if self._request_index.get(key) == record.job_id:
                    self._request_index.pop(key, None)
            return self._snapshot(record)

    async def wait_until_prepared(
        self,
        job_id: str,
        *,
        owner: str,
    ) -> tuple[JobSnapshot, Mapping[str, Any]]:
        """Wait for an idempotent reservation to finish safe preparation."""
        record = self._owned_record(job_id, owner)
        while True:
            with self._lock:
                if record.prepared:
                    if record.preparation_error is not None:
                        raise JobConflict(record.preparation_error)
                    return self._snapshot(record), dict(record.acceptance)
                if record.status in TERMINAL_STATUSES:
                    return self._snapshot(record), dict(record.acceptance)
            await asyncio.sleep(self._poll_seconds)

    def status(self, job_id: str, *, owner: str) -> JobSnapshot:
        record = self._owned_record(job_id, owner)
        with self._lock:
            return self._snapshot(record)

    def request_snapshot(
        self, *, kind: JobKind, owner: str, request_id: str | None
    ) -> JobSnapshot | None:
        """Return a previously accepted request for safe retry idempotency."""
        if not request_id:
            return None
        with self._lock:
            job_id = self._request_index.get((kind, owner, request_id))
            record = self._records.get(job_id) if job_id else None
            return self._snapshot(record) if record is not None else None

    def active_snapshot(self, *, kind: JobKind) -> JobSnapshot | None:
        """Return the active job, if any, without exposing its owner."""
        with self._lock:
            job_id = self._active.get(kind)
            record = self._records.get(job_id) if job_id else None
            if record is None or record.status in TERMINAL_STATUSES:
                return None
            return self._snapshot(record)

    def cancel(self, job_id: str, *, owner: str) -> JobSnapshot:
        record = self._owned_record(job_id, owner)
        with self._lock:
            if (
                not record.commit_started
                and record.status not in TERMINAL_STATUSES
                and record.status != "cancelling"
            ):
                record.cancel_event.set()
                self._append_event(
                    record,
                    kind="state",
                    status="cancelling",
                    data={"message": "Stopping response..."},
                )
                # ``starting`` means start_reserved is mid-preparation and
                # will create the worker task; it owns finalization from
                # here, and ``_run`` will observe the cancel_event set above.
                if record.task is None and not record.starting:
                    record.prepared = True
                    record.reservation_token = None
                    self._finalize_cancellation(record)
                    if self._active.get(record.kind) == record.job_id:
                        self._active.pop(record.kind, None)
            return self._snapshot(record)

    async def events(
        self,
        job_id: str,
        *,
        owner: str,
        after_sequence: int = 0,
    ):
        """Yield retained and newly published events in sequence order.

        The cursor is a monotonic lower bound, not a durable replay promise:
        old events may have been evicted by the count/byte retention limits.
        A reconnect with a cursor older than the oldest retained event starts
        at that oldest event, while sequence IDs continue to identify the
        original ordering.  Terminal status and its final event are always
        retained for a job that remains in this registry.
        """
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
        """Request cancellation and wait for owned workers to finish safely.

        A worker that has already begun committing its result (see
        :meth:`JobProgressSink.begin_commit`) is awaited without a bound --
        that commit must finish so persisted state and the retained event
        stream stay consistent. A worker that has not committed is only
        cooperative on a best-effort basis: it may be blocked inside a
        synchronous call (a model HTTP request with no read deadline, for
        example) that never polls ``cancel_event``. Waiting on it
        indefinitely would hang app shutdown -- and the llama-server child
        process it is talking to -- for as long as that call takes, so the
        first wait below is capped at ``shutdown_grace_seconds``. Anything
        still pending after that grace period is re-checked: a worker that
        committed *during* the grace period still gets the unbounded wait
        it is owed; a worker that never committed is abandoned so shutdown
        can proceed with the rest of teardown.
        """
        with self._lock:
            self._accepting = False
            records = [
                record
                for record in self._records.values()
                if record.status not in TERMINAL_STATUSES
            ]
            for record in records:
                if record.commit_started:
                    continue
                if record.status != "cancelling":
                    record.cancel_event.set()
                    self._append_event(
                        record,
                        kind="state",
                        status="cancelling",
                        data={"message": "Stopping response during shutdown..."},
                    )
                # See cancel(): a record still preparing has no task yet, but
                # start_reserved's phase three will close it out once it sees
                # that admission has stopped accepting work.
                if record.task is None and not record.starting:
                    record.prepared = True
                    record.reservation_token = None
                    self._finalize_cancellation(record)
                    if self._active.get(record.kind) == record.job_id:
                        self._active.pop(record.kind, None)
            tasks = [record.task for record in self._records.values() if record.task]
        pending = {task for task in tasks if task is not asyncio.current_task()}
        if not pending:
            return
        _, still_pending = await asyncio.wait(pending, timeout=self._shutdown_grace_seconds)
        if not still_pending:
            return
        with self._lock:
            committed_still_pending = [
                record.task
                for record in self._records.values()
                if record.task in still_pending and record.commit_started
            ]
        abandoned = len(still_pending) - len(committed_still_pending)
        if abandoned:
            logging.warning(
                "Cortex shutdown: %d job worker(s) did not observe cancellation within "
                "%.0fs and were abandoned so shutdown could proceed.",
                abandoned,
                self._shutdown_grace_seconds,
            )
        if committed_still_pending:
            # Cancelling an asyncio task does not stop its ``to_thread``
            # worker. Waiting for the task lets the worker observe the event,
            # complete its cleanup, and finalize the cancellation itself.
            await asyncio.gather(*committed_still_pending, return_exceptions=True)

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
                if record.status == "cancelling" or record.cancel_event.is_set():
                    self._finalize_cancellation(record)
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
                if not record.commit_started and (
                    record.status == "cancelling" or record.cancel_event.is_set()
                ):
                    self._finalize_cancellation(record)
                    return
                data = dict(
                    serialize_result(result) if serialize_result else _serialize(result)
                )
                record.result = data
                self._append_event(
                    record,
                    kind="completed",
                    status="succeeded",
                    data=data,
                )
        except asyncio.CancelledError:
            with self._lock:
                if (
                    record.status not in TERMINAL_STATUSES
                    and not record.commit_started
                    and (record.status == "cancelling" or record.cancel_event.is_set())
                ):
                    self._finalize_cancellation(record)
            raise
        except Exception as exc:
            with self._lock:
                if record.status in TERMINAL_STATUSES:
                    return
                if not record.commit_started and (
                    record.status == "cancelling" or record.cancel_event.is_set()
                ):
                    self._finalize_cancellation(record)
                    return
                logging.error(
                    "Cortex %s job failed (%s).", record.kind, type(exc).__name__
                )
                message = (
                    getattr(exc, "user_message", None)
                    or "Job failed. Please try again."
                )
                record.error = str(message)
                error_details = getattr(exc, "error_details", None)
                self._append_event(
                    record,
                    kind="error",
                    status="failed",
                    data={
                        "message": record.error,
                        "details": error_details or type(exc).__name__,
                    },
                )
        finally:
            with self._lock:
                if self._active.get(record.kind) == record.job_id:
                    self._active.pop(record.kind, None)

    def _finalize_cancellation(self, record: _JobRecord) -> None:
        """Publish the terminal cancellation only after the worker has exited."""
        if record.status in TERMINAL_STATUSES:
            return
        record.error = "Job cancelled."
        self._append_event(
            record,
            kind="state",
            status="cancelled",
            data={"message": "Job cancelled."},
        )

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
                key = (record.kind, record.owner, record.request_id)
                if self._request_index.get(key) == record.job_id:
                    self._request_index.pop(key, None)

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
        retained_data, retained_bytes = _retain_event_data(
            data,
            max_bytes=self._max_event_bytes,
        )
        event = JobEvent(
            sequence=record.sequence,
            job_id=record.job_id,
            thread_id=record.thread_id,
            kind=kind,
            status=status,
            phase=phase,
            data=retained_data,
        )
        record.events.append(event)
        record.event_bytes += retained_bytes
        while (
            len(record.events) > self._max_event_count
            or record.event_bytes > self._max_event_bytes
        ):
            # Keep at least the newest event.  Since terminal status is set
            # before this method returns, this also guarantees that a
            # terminal event survives aggressive retention settings.
            if len(record.events) == 1:
                break
            evicted = record.events.pop(0)
            record.event_bytes -= _event_data_bytes(evicted.data) or 0
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
            result=dict(record.result) if record.result is not None else None,
        )


def _serialize(result: Any) -> Mapping[str, Any]:
    if isinstance(result, Mapping):
        return result
    if is_dataclass(result):
        return asdict(result)
    return {"result": result}


def _event_data_bytes(data: Mapping[str, Any]) -> int | None:
    """Return the bounded accounting size without exposing payloads in logs."""
    try:
        return len(
            json.dumps(
                dict(data),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        return None


def _retain_event_data(
    data: Mapping[str, Any] | None,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any], int]:
    """Copy one event payload, compacting invalid or oversized values safely."""
    try:
        candidate = dict(data or {})
    except (
        TypeError,
        ValueError,
        OverflowError,
        UnicodeError,
        RecursionError,
    ):
        candidate = {}
    candidate_bytes = _event_data_bytes(candidate)
    if candidate_bytes is not None and candidate_bytes <= max_bytes:
        return candidate, candidate_bytes

    # Do not attempt to partially preserve arbitrary model/user text.  A
    # compact marker leaves the event's sequence/kind/status usable for SSE;
    # the job snapshot still carries terminal result/error state.
    fallback = {"message": _EVENT_DATA_TRUNCATED, "truncated": True}
    fallback_bytes = _event_data_bytes(fallback)
    if fallback_bytes is not None and fallback_bytes <= max_bytes:
        return fallback, fallback_bytes
    return {}, 0
