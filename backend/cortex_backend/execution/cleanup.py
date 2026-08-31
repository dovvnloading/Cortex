"""Supervised retention cleanup for durable execution data."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import Event, Lock, Thread
from uuid import uuid4

from .repository import (
    DEFAULT_TERMINAL_JOB_RETENTION_SECONDS,
    ExecutionCleanupResult,
    ExecutionRepository,
    LeaseConflict,
)


_LOGGER = logging.getLogger("cortex.execution.cleanup")


@dataclass(frozen=True, slots=True)
class CleanupMetrics:
    """Process-local counters; no user data or paths are recorded."""

    runs: int = 0
    successes: int = 0
    failures: int = 0
    lease_conflicts: int = 0
    skipped_overlap: int = 0
    artifacts: int = 0
    jobs: int = 0
    events: int = 0
    last_error: str | None = None


class ExecutionCleanupSupervisor:
    """Run bounded cleanup in a restartable, lease-protected daemon thread."""

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        interval_seconds: float = 300.0,
        lease_seconds: float = 120.0,
        terminal_job_retention_seconds: int = DEFAULT_TERMINAL_JOB_RETENTION_SECONDS,
        batch_size: int = 100,
    ) -> None:
        if not isinstance(repository, ExecutionRepository):
            raise TypeError("repository must be an ExecutionRepository")
        if interval_seconds <= 0 or lease_seconds <= 0:
            raise ValueError("cleanup intervals and lease duration must be positive")
        if isinstance(batch_size, bool) or not 1 <= batch_size <= 10_000:
            raise ValueError("batch_size must be between 1 and 10000")
        if (
            isinstance(terminal_job_retention_seconds, bool)
            or not isinstance(terminal_job_retention_seconds, int)
            or terminal_job_retention_seconds < 0
        ):
            raise ValueError("terminal_job_retention_seconds must be non-negative")
        self.repository = repository
        self.interval_seconds = float(interval_seconds)
        self.lease_seconds = float(lease_seconds)
        self.terminal_job_retention_seconds = terminal_job_retention_seconds
        self.batch_size = batch_size
        self._owner = f"cleanup-{uuid4().hex}"
        self._stop_event = Event()
        self._run_lock = Lock()
        self._metrics_lock = Lock()
        self._metrics = CleanupMetrics()
        self._thread: Thread | None = None
        self._renew_stop = Event()
        self._lease_lost = Event()
        self._renew_thread: Thread | None = None

    @property
    def metrics(self) -> CleanupMetrics:
        with self._metrics_lock:
            return self._metrics

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start at most one cleanup thread; safe to call after stop."""

        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._stop_event.clear()
        thread = Thread(
            target=self._run,
            name="cortex-execution-cleanup",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the worker and release its lease without raising cleanup errors."""

        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        if thread is None or not thread.is_alive():
            self._thread = None
            if self._stop_lease_renewal():
                try:
                    self.repository.release_cleanup_lease(lease_owner=self._owner)
                except Exception as exc:
                    _LOGGER.warning(
                        "Cortex cleanup lease release failed (%s).", type(exc).__name__
                    )
            else:
                _LOGGER.warning(
                    "Cortex cleanup lease renewal did not stop before shutdown; "
                    "the lease will expire naturally."
                )

    def run_once(self) -> bool:
        """Attempt one pass, returning false when another supervisor owns it."""

        if not self._run_lock.acquire(blocking=False):
            self._increment(skipped_overlap=1)
            return False
        try:
            self._increment(runs=1)
            try:
                self.repository.claim_cleanup_lease(
                    lease_owner=self._owner,
                    ttl_seconds=self.lease_seconds,
                )
            except LeaseConflict:
                self._increment(lease_conflicts=1)
                return False
            except Exception as exc:
                self._record_failure(exc)
                return False
            try:
                self._start_lease_renewal()
                if self._lease_lost.is_set():
                    self._record_failure(RuntimeError("cleanup lease lost"))
                    return False
                result = self.repository.cleanup_expired(
                    terminal_job_retention_seconds=self.terminal_job_retention_seconds,
                    limit=self.batch_size,
                )
                if self._lease_lost.is_set():
                    raise RuntimeError("cleanup lease lost")
                if not isinstance(result, ExecutionCleanupResult):
                    raise RuntimeError("cleanup returned an invalid result")
                self._increment(
                    successes=1,
                    artifacts=result.artifacts,
                    jobs=result.jobs,
                    events=result.events,
                )
                return True
            except Exception as exc:
                self._record_failure(exc)
                return False
            finally:
                if self._stop_lease_renewal():
                    try:
                        self.repository.release_cleanup_lease(lease_owner=self._owner)
                    except Exception as exc:
                        _LOGGER.warning(
                            "Cortex cleanup lease release failed (%s).", type(exc).__name__
                        )
                else:
                    _LOGGER.warning(
                        "Cortex cleanup lease renewal did not stop after the pass; "
                        "the lease will expire naturally."
                    )
        finally:
            self._run_lock.release()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self.interval_seconds)

    def _start_lease_renewal(self) -> None:
        existing = self._renew_thread
        if existing is not None:
            if existing.is_alive():
                raise RuntimeError("cleanup lease renewal is still stopping")
            self._renew_thread = None
        self._renew_stop.clear()
        self._lease_lost.clear()
        self._renew_thread = Thread(
            target=self._renew_lease,
            name="cortex-execution-cleanup-lease",
            daemon=True,
        )
        self._renew_thread.start()

    def _stop_lease_renewal(self) -> bool:
        self._renew_stop.set()
        thread = self._renew_thread
        if thread is None:
            return True
        thread.join(timeout=max(1.0, min(self.lease_seconds, 5.0)))
        if thread.is_alive():
            return False
        self._renew_thread = None
        return True

    def _renew_lease(self) -> None:
        # Refresh well before expiry so a slow filesystem operation cannot
        # overlap another supervisor's pass.
        interval = max(0.01, min(self.lease_seconds / 3.0, 30.0))
        # Renew once immediately. Waiting for the first interval would leave
        # a very short lease exposed during thread startup/scheduling, which
        # can make an otherwise healthy slow cleanup appear to lose ownership.
        while not self._renew_stop.is_set():
            try:
                if not self.repository.renew_cleanup_lease(
                    lease_owner=self._owner,
                    ttl_seconds=self.lease_seconds,
                ):
                    self._lease_lost.set()
                    return
            except Exception as exc:
                self._lease_lost.set()
                _LOGGER.warning(
                    "Cortex cleanup lease renewal failed (%s).", type(exc).__name__
                )
                return
            if self._renew_stop.wait(interval):
                return

    def _record_failure(self, exc: Exception) -> None:
        self._increment(failures=1, last_error=type(exc).__name__)
        _LOGGER.warning("Cortex retention cleanup failed (%s).", type(exc).__name__)

    def _increment(self, **changes: int | str) -> None:
        with self._metrics_lock:
            values = {
                field: getattr(self._metrics, field)
                for field in self._metrics.__dataclass_fields__
            }
            for field, value in changes.items():
                if field == "last_error":
                    values[field] = str(value)
                else:
                    values[field] = int(values[field]) + int(value)
            self._metrics = CleanupMetrics(**values)


CleanupSupervisor = ExecutionCleanupSupervisor

__all__ = ["CleanupMetrics", "CleanupSupervisor", "ExecutionCleanupSupervisor"]
