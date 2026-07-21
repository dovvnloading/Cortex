"""Durable background coordinator backed only by the Phase 1 fake provider."""

from __future__ import annotations

from threading import Event, Lock, Thread
import time
from typing import Any, Mapping
from uuid import uuid4

from .fake import FakeExecutionCancelled, FakeExecutionFailure, FakeExecutionPlan, FakeExecutionProvider
from .models import ExecutionJob, TerminalExecutionStatus
from .repository import ExecutionRepository, LeaseConflict


class DurableFakeCoordinator:
    """Run deterministic fake plans in background threads with durable state."""

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        provider: FakeExecutionProvider | None = None,
        lease_seconds: float = 30.0,
        supervisor_lease_seconds: float = 30.0,
    ) -> None:
        if supervisor_lease_seconds <= 0:
            raise ValueError("supervisor_lease_seconds must be positive")
        self.repository = repository
        self.provider = provider or FakeExecutionProvider()
        self.lease_seconds = lease_seconds
        self.supervisor_lease_seconds = supervisor_lease_seconds
        self._supervisor_owner = f"fake-supervisor-{uuid4().hex}"
        self._supervisor_lease_active = False
        self._lock = Lock()
        self._threads: dict[str, Thread] = {}
        self._cancel_events: dict[str, Event] = {}
        self.startup_recover()

    def start(
        self,
        *,
        owner: str,
        request_id: str,
        profile: str = "fake.v1",
        plan: FakeExecutionPlan | None = None,
    ) -> ExecutionJob:
        selected = plan or FakeExecutionPlan()
        job, created = self.repository.create_job(
            job_id=uuid4().hex,
            owner=owner,
            request_id=request_id,
            profile=profile,
            payload={
                "provider": "fake-v1",
                "outcome": selected.outcome,
                "steps": selected.steps,
                "step_delay_seconds": selected.step_delay_seconds,
                "failure_message": selected.failure_message,
            },
        )
        if created:
            self._launch(job.job_id, selected)
        return job

    def resume(self, job_id: str, *, plan: FakeExecutionPlan | None = None) -> ExecutionJob:
        """Resume a queued/recovered job after a simulated process restart."""
        job = self.repository.get_job(job_id)
        if job is None:
            raise ValueError("execution job does not exist")
        if job.status in TerminalExecutionStatus:
            return job
        selected = plan or FakeExecutionPlan()
        self._launch(job_id, selected)
        return self.repository.get_job(job_id) or job

    def cancel(self, job_id: str, *, owner: str) -> ExecutionJob:
        job = self.repository.get_job(job_id, owner=owner)
        if job is None:
            raise ValueError("execution job does not exist or is not owned by caller")
        with self._lock:
            event = self._cancel_events.get(job_id)
            if event is not None:
                event.set()
        updated = self.repository.request_cancel(job_id)
        return updated

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

    def shutdown(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            events = list(self._cancel_events.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        for thread in threads:
            thread.join(timeout=timeout)
        if self._supervisor_lease_active:
            self.repository.release_supervisor_lease(lease_owner=self._supervisor_owner)
            self._supervisor_lease_active = False

    def startup_recover(self) -> list[str]:
        """Claim the single supervisor lease and resume only recoverable fake jobs."""
        if self._supervisor_lease_active:
            return []
        self.repository.claim_supervisor_lease(
            lease_owner=self._supervisor_owner,
            ttl_seconds=self.supervisor_lease_seconds,
        )
        self._supervisor_lease_active = True
        recovered = self.repository.recover_expired_leases()
        self.repository.expire_approvals()
        for job_id in recovered:
            job = self.repository.get_job(job_id)
            if job is None or job.status in TerminalExecutionStatus:
                continue
            if job.approval_state in {"pending", "denied", "expired"}:
                continue
            try:
                if job.profile != "fake.v1":
                    raise ValueError("unsupported recovery profile")
                plan = self._plan_from_payload(job.payload)
            except (TypeError, ValueError, KeyError):
                self.repository.transition(
                    job_id,
                    status="failed",
                    event="failed",
                    phase="recovery",
                    data={"message": "Execution recovery payload is invalid."},
                    error="recovery_invalid_payload",
                )
                continue
            self._launch(job_id, plan)
        return recovered

    def _launch(self, job_id: str, plan: FakeExecutionPlan) -> None:
        with self._lock:
            existing = self._threads.get(job_id)
            if existing is not None and existing.is_alive():
                return
            cancel_event = self._cancel_events.setdefault(job_id, Event())
            thread = Thread(
                target=self._run,
                args=(job_id, plan, cancel_event),
                name=f"cortex-execution-fake-{job_id}",
                daemon=True,
            )
            self._threads[job_id] = thread
            thread.start()

    @staticmethod
    def _plan_from_payload(payload: Mapping[str, Any]) -> FakeExecutionPlan:
        if payload.get("provider") != "fake-v1":
            raise ValueError("unsupported recovery provider")
        return FakeExecutionPlan(
            outcome=payload["outcome"],
            steps=payload["steps"],
            step_delay_seconds=payload["step_delay_seconds"],
            failure_message=payload.get(
                "failure_message", "Deterministic fake execution failed."
            ),
        )

    def _run(self, job_id: str, plan: FakeExecutionPlan, cancel_event: Event) -> None:
        lease_owner = f"fake-coordinator-{uuid4().hex}"
        try:
            self.repository.claim_lease(
                job_id,
                lease_owner=lease_owner,
                ttl_seconds=self.lease_seconds,
            )
            current = self.repository.get_job(job_id)
            if current is None:
                return
            if current.status == "cancelling" or cancel_event.is_set():
                self.repository.transition(
                    job_id,
                    status="cancelled",
                    event="cancelled",
                    phase="cancelled",
                    data={"message": "Execution cancelled before start."},
                    error="Execution cancelled.",
                )
                return
            self.repository.transition(
                job_id,
                status="running",
                event="started",
                phase="prepare",
                data={"message": "Fake execution started."},
            )

            def publish(phase: str, message: str, data: Mapping[str, Any]) -> None:
                self.repository.transition(
                    job_id,
                    status="running",
                    event="progress",
                    phase=phase,
                    data={"message": message, **dict(data)},
                )

            result = self.provider.run(plan, cancel_event, publish)
            current = self.repository.get_job(job_id)
            if cancel_event.is_set() or (current is not None and current.status == "cancelling"):
                self.repository.transition(
                    job_id,
                    status="cancelled",
                    event="cancelled",
                    phase="cancelled",
                    data={"message": "Execution cancelled."},
                    error="Execution cancelled.",
                )
            else:
                self.repository.transition(
                    job_id,
                    status="succeeded",
                    event="completed",
                    phase="completed",
                    data=result,
                    result=result,
                )
        except FakeExecutionCancelled:
            self.repository.transition(
                job_id,
                status="cancelled",
                event="cancelled",
                phase="cancelled",
                data={"message": "Execution cancelled."},
                error="Execution cancelled.",
            )
        except FakeExecutionFailure as exc:
            self.repository.transition(
                job_id,
                status="failed",
                event="failed",
                phase="failed",
                data={"message": str(exc)},
                error=str(exc),
            )
        except LeaseConflict as exc:
            self.repository.transition(
                job_id,
                status="failed",
                event="failed",
                phase="recovery",
                data={"message": "Execution lease unavailable."},
                error=str(exc),
            )
        except Exception as exc:
            self.repository.transition(
                job_id,
                status="failed",
                event="failed",
                phase="failed",
                data={"message": "Fake execution coordinator failed."},
                error=type(exc).__name__,
            )
        finally:
            self.repository.release_lease(job_id, lease_owner=lease_owner)
