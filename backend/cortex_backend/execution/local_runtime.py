"""One lifecycle owner for the local execution capabilities.

It coordinates three intentionally narrow background capabilities, each of
which does its work in a short-lived child process defined in its own module:

* ``scratch.auto.v1`` evaluates a safe decimal expression;
* ``code.exec.v1`` runs one validated, user-approved program; and
* ``recipe.image.v1`` runs the fixed image provider after attachment staging
  has copied bytes into the artifact store.

Only ``code.exec.v1`` accepts source, and only behind an explicit approval.
None of the three accepts a path, shell command, package name, or network
instruction. This coordinator owns durable state, leases, cancellation, and
artifact publication; the workers receive only immutable input and return
compact validated output.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import hmac
import json
import shutil
from threading import Event, Lock, Thread
import time
from typing import Any
from uuid import uuid4

from .artifact_boundary import ArtifactBoundary
from .code_execution import (
    CODE_EXECUTION_PAYLOAD_SCHEMA,
    CODE_EXECUTION_PROFILE,
    CodeCapabilities,
    CodeExecutionError,
    CodeExecutionRequest,
    MAX_CODE_TIMEOUT_SECONDS,
)
from .lifecycle import RuntimeHealth
from .local_code_attempt import (
    DEFAULT_CODE_STARTUP_TIMEOUT_SECONDS,
    DEFAULT_CODE_TIMEOUT_SECONDS,
    LocalCodeAttempt,
)
from .local_recipe_attempt import (
    DEFAULT_IMAGE_TIMEOUT_SECONDS,
    LocalRecipeWorkerAttempt,
)
from .local_scratch_attempt import (
    DEFAULT_SCRATCH_STARTUP_TIMEOUT_SECONDS,
    DEFAULT_SCRATCH_TIMEOUT_SECONDS,
    LocalScratchAttempt,
)
from .models import ExecutionJob, TerminalExecutionStatus
from .recipe_coordinator import (
    RECIPE_IMAGE_PROFILE,
    RecipeExecutionCoordinator,
    RecipeExecutionError,
    RecipeImageRequest,
)
from .recipe_provider import RecipeImageProvider
from .repository import (
    ExecutionRepository,
    ExecutionTransitionConflict,
    LeaseConflict,
)
from .scratch_compute import (
    SCRATCH_COMPUTE_PROFILE,
    SCRATCH_PAYLOAD_SCHEMA,
    ScratchComputeError,
    ScratchComputeRequest,
    scratch_result_payload,
    validate_scratch_expression,
)






_LOGGER = logging.getLogger("cortex.execution.local_runtime")


class LocalExecutionCoordinator:
    """One lifecycle owner for the normal local image and compute profiles."""

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        lease_seconds: float = 60.0,
        supervisor_lease_seconds: float = 60.0,
        scratch_timeout_seconds: float = DEFAULT_SCRATCH_TIMEOUT_SECONDS,
        scratch_startup_timeout_seconds: float = DEFAULT_SCRATCH_STARTUP_TIMEOUT_SECONDS,
        image_timeout_seconds: float = DEFAULT_IMAGE_TIMEOUT_SECONDS,
        code_timeout_seconds: float = DEFAULT_CODE_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(repository, ExecutionRepository):
            raise TypeError("repository must be an ExecutionRepository")
        if lease_seconds <= 0 or supervisor_lease_seconds <= 0:
            raise ValueError("lease durations must be positive")
        self.repository = repository
        self.artifact_boundary = ArtifactBoundary(repository)
        self.lease_seconds = float(lease_seconds)
        self.supervisor_lease_seconds = float(supervisor_lease_seconds)
        self.scratch_timeout_seconds = float(scratch_timeout_seconds)
        self.scratch_startup_timeout_seconds = float(scratch_startup_timeout_seconds)
        self.image_timeout_seconds = float(image_timeout_seconds)
        self.code_timeout_seconds = min(float(code_timeout_seconds), MAX_CODE_TIMEOUT_SECONDS)
        self._supervisor_owner = f"local-supervisor-{uuid4().hex}"
        self._supervisor_lease_active = False
        self._supervisor_stop_event = Event()
        self._supervisor_thread: Thread | None = None
        self._scratch_lock = Lock()
        self._scratch_threads: dict[str, Thread] = {}
        self._scratch_cancel_events: dict[str, Event] = {}
        self._scratch_attempts: dict[str, LocalScratchAttempt] = {}
        self._code_lock = Lock()
        self._code_threads: dict[str, Thread] = {}
        self._code_cancel_events: dict[str, Event] = {}
        self._code_attempts: dict[str, LocalCodeAttempt] = {}
        self._recipe = RecipeExecutionCoordinator(
            repository,
            lambda job: LocalRecipeWorkerAttempt(
                job,
                timeout_seconds=self.image_timeout_seconds,
            ),
            artifact_boundary=self.artifact_boundary,
            lease_seconds=self.lease_seconds,
            supervisor_lease_seconds=self.supervisor_lease_seconds,
            auto_recover=False,
        )
        self._image_health = self._probe_image_provider()

    @property
    def scratch_available(self) -> bool:
        return True

    @property
    def code_execution_available(self) -> bool:
        return True

    @property
    def image_transform_available(self) -> bool:
        return self._image_health.available

    @property
    def image_health(self) -> RuntimeHealth:
        return self._image_health

    @staticmethod
    def _probe_image_provider() -> RuntimeHealth:
        provider = RecipeImageProvider()
        try:
            return provider.start(
                RuntimeHealth.ready("The local image provider is being checked.")
            )
        except Exception:
            return RuntimeHealth.blocked(
                "image_provider_unavailable",
                "The fixed image transformation provider is unavailable.",
            )
        finally:
            try:
                provider.stop()
            except Exception:
                pass

    def start_image_transform(self, request: RecipeImageRequest) -> ExecutionJob:
        if not self.image_transform_available:
            raise RecipeExecutionError("provider_unavailable")
        return self._recipe.start_image_transform(request)

    def start_scratch(self, request: ScratchComputeRequest) -> ExecutionJob:
        if not isinstance(request, ScratchComputeRequest):
            raise TypeError("request must be a ScratchComputeRequest")
        payload = self._scratch_payload(request)
        job, created = self.repository.create_job(
            job_id=uuid4().hex,
            owner=request.owner,
            request_id=request.request_id,
            profile=SCRATCH_COMPUTE_PROFILE,
            payload=payload,
        )
        if not created:
            if (
                job.profile != SCRATCH_COMPUTE_PROFILE
                or self._canonical_payload(job.payload) != self._canonical_payload(payload)
            ):
                raise ScratchComputeError("request_conflict")
            return job
        self._launch_scratch(job.job_id, request)
        return job

    def start_code(self, request: CodeExecutionRequest) -> ExecutionJob:
        if not isinstance(request, CodeExecutionRequest):
            raise TypeError("request must be a CodeExecutionRequest")
        payload = request.payload()
        job, created = self.repository.create_job(
            job_id=uuid4().hex,
            owner=request.owner,
            request_id=request.request_id,
            profile=CODE_EXECUTION_PROFILE,
            payload=payload,
        )
        if not created:
            if (
                job.profile != CODE_EXECUTION_PROFILE
                or self._canonical_payload(job.payload) != self._canonical_payload(payload)
            ):
                raise CodeExecutionError("request_conflict")
            if job.status not in TerminalExecutionStatus and job.status != "cancelling":
                self._launch_code(job.job_id)
            return job
        try:
            self.repository.request_approval(
                job.job_id,
                owner=request.owner,
                scope_digest=request.approval_scope_digest,
                reason=request.intent_summary.strip(),
            )
        except Exception as exc:
            try:
                self.repository.transition(
                    job.job_id,
                    status="failed",
                    event="code.failed",
                    phase="approval",
                    data={"message": "Code execution approval could not be created."},
                    error="approval_unavailable",
                )
            except Exception:
                pass
            if isinstance(exc, CodeExecutionError):
                raise
            raise CodeExecutionError("approval_unavailable") from None
        self._launch_code(job.job_id)
        return self.repository.get_job(job.job_id, owner=request.owner) or job

    def _code_workspace(self, job_id: str) -> str:
        """Create one non-shared workspace for a single approved run."""

        root = self.repository.artifact_root / ".code_workspaces"
        try:
            if root.exists() and (root.is_symlink() or getattr(root, "is_junction", lambda: False)()):
                raise CodeExecutionError("workspace_invalid")
            root.mkdir(parents=True, exist_ok=True)
            workspace = root / job_id
            if workspace.exists() and (workspace.is_symlink() or getattr(workspace, "is_junction", lambda: False)()):
                raise CodeExecutionError("workspace_invalid")
            if workspace.exists():
                # A prior attempt for this job_id can leave stale contents
                # behind: a hard process crash skips _run_code's finally
                # block (which normally calls _cleanup_code_workspace), and
                # startup_recover() re-launches the same job_id afterward.
                # exist_ok=True below would then silently reuse whatever
                # files that crashed attempt left, letting them leak into
                # what is supposed to be a fresh, isolated run. Clear it
                # first so every attempt -- first launch or crash-recovery
                # relaunch -- starts from a genuinely empty directory.
                shutil.rmtree(workspace)
            workspace.mkdir(parents=True, exist_ok=True)
            resolved = workspace.resolve(strict=True)
            artifact_root = self.repository.artifact_root.resolve(strict=True)
            if not resolved.is_relative_to(artifact_root) or not resolved.is_dir():
                raise CodeExecutionError("workspace_invalid")
            return str(resolved)
        except CodeExecutionError:
            raise
        except (OSError, RuntimeError):
            raise CodeExecutionError("workspace_invalid") from None

    def _cleanup_code_workspace(self, job_id: str) -> None:
        root = self.repository.artifact_root / ".code_workspaces"
        workspace = root / job_id
        try:
            if not workspace.exists():
                return
            if workspace.is_symlink() or getattr(workspace, "is_junction", lambda: False)():
                raise CodeExecutionError("workspace_invalid")
            resolved = workspace.resolve(strict=True)
            if not resolved.is_relative_to(root.resolve(strict=True)):
                raise CodeExecutionError("workspace_invalid")
            shutil.rmtree(resolved)
        except CodeExecutionError:
            raise
        except (OSError, RuntimeError):
            raise CodeExecutionError("workspace_cleanup_failed") from None

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
        if job.profile == RECIPE_IMAGE_PROFILE:
            return self._recipe.cancel(job_id, owner=owner)
        if job.profile == SCRATCH_COMPUTE_PROFILE:
            with self._scratch_lock:
                event = self._scratch_cancel_events.get(job_id)
                attempt = self._scratch_attempts.get(job_id)
            if event is not None:
                event.set()
            if attempt is not None:
                attempt.cancel()
            return self.repository.request_cancel(job_id)
        if job.profile == CODE_EXECUTION_PROFILE:
            if job.approval_state == "pending":
                try:
                    self.repository.decide_approval(
                        job_id,
                        owner=owner,
                        decision="denied",
                    )
                except Exception:
                    pass
                return self.repository.get_job(job_id, owner=owner) or job
            with self._code_lock:
                event = self._code_cancel_events.get(job_id)
                attempt = self._code_attempts.get(job_id)
            if event is not None:
                event.set()
            if attempt is not None:
                attempt.cancel()
            return self.repository.request_cancel(job_id)
        return self.repository.request_cancel(job_id)

    def startup_recover(self) -> list[str]:
        if self._supervisor_lease_active:
            return []
        existing_thread = self._supervisor_thread
        if existing_thread is not None and existing_thread.is_alive():
            # A timed-out shutdown may leave a lease renewal blocked inside
            # SQLite. Do not reuse its stop event or start another heartbeat
            # until that thread has actually exited; callers can safely retry.
            raise RuntimeError("Execution supervisor is still stopping.")
        self._supervisor_thread = None
        self.repository.claim_supervisor_lease(
            lease_owner=self._supervisor_owner,
            ttl_seconds=self.supervisor_lease_seconds,
        )
        self._supervisor_lease_active = True
        try:
            recovered = self.repository.recover_expired_leases()
            self.repository.expire_approvals()
            self._recipe.recover_jobs(recovered)
            for job_id in recovered:
                job = self.repository.get_job(job_id)
                if job is None or job.profile != SCRATCH_COMPUTE_PROFILE:
                    continue
                self._recover_scratch(job)
            try:
                owner = self.repository.installation_principal_id
                for job in self.repository.list_jobs(
                    owner=owner,
                    include_terminal=False,
                    limit=200,
                ):
                    if job.profile == CODE_EXECUTION_PROFILE:
                        self._launch_code(job.job_id)
            except Exception:
                pass
        except Exception:
            self.repository.release_supervisor_lease(
                lease_owner=self._supervisor_owner
            )
            self._supervisor_lease_active = False
            raise
        try:
            self._start_supervisor_heartbeat()
        except Exception:
            self.repository.release_supervisor_lease(
                lease_owner=self._supervisor_owner
            )
            self._supervisor_lease_active = False
            raise
        return recovered

    def shutdown(self, *, timeout: float = 5.0) -> None:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        self._supervisor_stop_event.set()
        supervisor_thread = self._supervisor_thread
        with self._scratch_lock:
            events = list(self._scratch_cancel_events.values())
            attempts = list(self._scratch_attempts.values())
            threads = list(self._scratch_threads.values())
        for event in events:
            event.set()
        for attempt in attempts:
            attempt.cancel()
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._code_lock:
            code_events = list(self._code_cancel_events.values())
            code_attempts = list(self._code_attempts.values())
            code_threads = list(self._code_threads.values())
        for event in code_events:
            event.set()
        for attempt in code_attempts:
            attempt.cancel()
        for thread in code_threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._recipe.shutdown(timeout=max(0.0, deadline - time.monotonic()))
        if supervisor_thread is not None:
            supervisor_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        supervisor_stopped = supervisor_thread is None or not supervisor_thread.is_alive()
        self._supervisor_thread = None if supervisor_stopped else supervisor_thread
        if self._supervisor_lease_active:
            if supervisor_stopped:
                self.repository.release_supervisor_lease(
                    lease_owner=self._supervisor_owner
                )
            else:
                # Releasing while a renewal is still inside SQLite can race:
                # the late renewal may recreate a supposedly released lease.
                # Leave the record to expire instead of opening split-brain
                # recovery; the stop event makes the thread exit afterward.
                _LOGGER.warning(
                    "Cortex execution supervisor did not stop before shutdown timeout; "
                    "its lease will expire naturally."
                )
            self._supervisor_lease_active = False

    def _start_supervisor_heartbeat(self) -> None:
        thread = self._supervisor_thread
        if thread is not None and thread.is_alive():
            return
        self._supervisor_stop_event.clear()
        thread = Thread(
            target=self._renew_supervisor_lease,
            name="cortex-execution-supervisor-lease",
            daemon=True,
        )
        self._supervisor_thread = thread
        thread.start()

    def _renew_supervisor_lease(self) -> None:
        interval = max(0.001, min(self.supervisor_lease_seconds / 3.0, 5.0))
        while not self._supervisor_stop_event.wait(interval):
            if not self._supervisor_lease_active:
                return
            try:
                self.repository.claim_supervisor_lease(
                    lease_owner=self._supervisor_owner,
                    ttl_seconds=self.supervisor_lease_seconds,
                )
            except LeaseConflict:
                self._supervisor_lease_active = False
                _LOGGER.error(
                    "Cortex execution supervisor lost its durable lease to another coordinator."
                )
                return
            except Exception as exc:
                # A transient SQLite failure should be retried before the lease
                # expires. Never log database paths or lease owner tokens.
                _LOGGER.warning(
                    "Cortex execution supervisor lease renewal failed (%s).",
                    type(exc).__name__,
                )

    @staticmethod
    def _canonical_payload(payload: Mapping[str, Any]) -> str:
        return json.dumps(dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _scratch_payload(request: ScratchComputeRequest) -> Mapping[str, str]:
        return {
            "schema_version": SCRATCH_PAYLOAD_SCHEMA,
            "expression": request.expression.strip(),
        }

    def _launch_code(self, job_id: str) -> None:
        with self._code_lock:
            existing = self._code_threads.get(job_id)
            if existing is not None and existing.is_alive():
                return
            cancel_event = self._code_cancel_events.setdefault(job_id, Event())
            thread = Thread(
                target=self._run_code,
                args=(job_id, cancel_event),
                name=f"cortex-code-{job_id}",
                daemon=True,
            )
            self._code_threads[job_id] = thread
            thread.start()

    def _run_code(self, job_id: str, cancel_event: Event) -> None:
        lease_owner = f"code-coordinator-{uuid4().hex}"
        attempt: LocalCodeAttempt | None = None
        lease_claimed = False
        with self._code_lock:
            self._code_cancel_events[job_id] = cancel_event
        try:
            current = self.repository.get_job(job_id)
            if current is None or current.profile != CODE_EXECUTION_PROFILE:
                return
            if current.approval_state == "expired":
                self.repository.expire_approvals()
                return
            next_approval_expiry_check = 0.0
            while current.approval_state == "pending" and not cancel_event.is_set():
                now = time.monotonic()
                if now >= next_approval_expiry_check:
                    # Pending approvals must expire during a live coordinator,
                    # not only during process startup/recovery.
                    self.repository.expire_approvals()
                    next_approval_expiry_check = now + 0.25
                time.sleep(0.05)
                current = self.repository.get_job(job_id)
                if current is None:
                    return
            if current.approval_state == "expired":
                self.repository.expire_approvals()
                return
            if cancel_event.is_set() or current.status in {"cancelled", "cancelling"} or current.approval_state in {"denied", "expired"}:
                # A denial/expiry already reaches a terminal status through
                # decide_approval()/expire_approvals(); this call is then a
                # safe no-op. A cancellation requested while still pending or
                # approved-but-unleased is not yet terminal anywhere else --
                # without finishing it here, the job is left in "cancelling"
                # forever, since nothing else will ever revisit it.
                self._finish_code_failure(job_id, cancel_event, "cancelled")
                return
            if current.approval_state != "approved":
                self._finish_code_failure(job_id, cancel_event, "approval_required")
                return
            self.repository.claim_lease(
                job_id,
                lease_owner=lease_owner,
                ttl_seconds=self.lease_seconds,
            )
            lease_claimed = True
            current = self.repository.get_job(job_id)
            if current is None or current.status in {"cancelled", "cancelling"}:
                # Same reasoning as above: the lease was already claimed (and
                # is released in the finally block below), but the job's
                # status is not yet terminal on its own.
                if current is not None:
                    self._finish_code_failure(job_id, cancel_event, "cancelled")
                return
            request = self._code_request_from_job(current)
            workspace = self._code_workspace(job_id)
            approval_scope = self.repository.get_approval_scope_digest(
                job_id,
                owner=current.owner,
            )
            if approval_scope is None or not hmac.compare_digest(
                approval_scope,
                request.approval_scope_digest,
            ):
                raise CodeExecutionError("approval_scope_mismatch")
            self.repository.transition(
                job_id,
                status="running",
                event="code.started",
                phase="prepare",
                data={"message": "Local code execution started."},
            )
            self.repository.transition(
                job_id,
                status="running",
                event="code.output",
                phase="worker",
                data={"message": "Running in an isolated local worker."},
            )
            attempt = LocalCodeAttempt(timeout_seconds=self.code_timeout_seconds)
            with self._code_lock:
                self._code_attempts[job_id] = attempt
            result = attempt.evaluate(
                request.source,
                request.capabilities,
                workspace,
                cancel_event,
            )
            current = self.repository.get_job(job_id)
            if cancel_event.is_set() or (current is not None and current.status == "cancelling"):
                raise CodeExecutionError("cancelled")
            payload = result.as_payload()
            try:
                completed = self.repository.transition(
                    job_id,
                    status="succeeded",
                    event="code.completed",
                    phase="completed",
                    data={"message": "Local code execution completed."},
                    result=payload,
                    expected_status="running",
                )
            except ExecutionTransitionConflict:
                latest = self.repository.get_job(job_id)
                if cancel_event.is_set() or (
                    latest is not None
                    and latest.status in {"cancelling", "cancelled"}
                ):
                    raise CodeExecutionError("cancelled") from None
                raise CodeExecutionError("completion_conflict") from None
            if completed.status != "succeeded":
                if cancel_event.is_set() or completed.status in {
                    "cancelling",
                    "cancelled",
                }:
                    raise CodeExecutionError("cancelled")
                raise CodeExecutionError("completion_conflict")
        except CodeExecutionError as exc:
            self._finish_code_failure(job_id, cancel_event, exc.code)
        except LeaseConflict:
            # Another live coordinator owns this exact attempt. It is not a job
            # failure and must not overwrite that coordinator's eventual result.
            return
        except Exception:
            self._finish_code_failure(job_id, cancel_event, "coordinator_failed")
        finally:
            if attempt is not None:
                attempt.close()
            with self._code_lock:
                self._code_attempts.pop(job_id, None)
                self._code_cancel_events.pop(job_id, None)
                self._code_threads.pop(job_id, None)
            if lease_claimed:
                try:
                    try:
                        self._cleanup_code_workspace(job_id)
                    except CodeExecutionError:
                        # Cleanup failure never reopens the job or grants access;
                        # the orphan remains under the validated artifact root for
                        # a later maintenance pass.
                        pass
                finally:
                    try:
                        self.repository.release_lease(job_id, lease_owner=lease_owner)
                    except Exception:
                        pass

    @staticmethod
    def _code_request_from_job(job: ExecutionJob) -> CodeExecutionRequest:
        payload = job.payload
        if payload.get("schema_version") != CODE_EXECUTION_PAYLOAD_SCHEMA or payload.get("language") != "python":
            raise CodeExecutionError("recovery_invalid_payload")
        source = payload.get("source")
        intent = payload.get("intent_summary")
        expected_digest = payload.get("source_digest")
        if not isinstance(source, str) or not isinstance(intent, str):
            raise CodeExecutionError("recovery_invalid_payload")
        try:
            request = CodeExecutionRequest(
                owner=job.owner,
                request_id=job.request_id,
                source=source,
                intent_summary=intent,
                capabilities=CodeCapabilities.from_mapping(payload.get("capabilities")),
            )
            if expected_digest != request.source_digest:
                raise CodeExecutionError("recovery_invalid_payload")
            return request
        except CodeExecutionError as exc:
            if exc.code == "process_capability_unavailable":
                raise
            raise CodeExecutionError("recovery_invalid_payload") from None
        except (TypeError, ValueError):
            raise CodeExecutionError("recovery_invalid_payload") from None

    def _finish_code_failure(self, job_id: str, cancel_event: Event, failure_code: str) -> None:
        # Re-evaluate after a guarded-transition conflict so a cancellation
        # that commits first cannot be overwritten by a late worker failure.
        for _ in range(3):
            current = self.repository.get_job(job_id)
            if current is None or current.status in TerminalExecutionStatus:
                return
            cancelled = (
                failure_code == "cancelled"
                or cancel_event.is_set()
                or current.status == "cancelling"
            )
            try:
                self.repository.transition(
                    job_id,
                    status="cancelled" if cancelled else "failed",
                    event="code.cancelled" if cancelled else "code.failed",
                    phase="cancelled" if cancelled else "failed",
                    data={
                        "message": (
                            "Local code execution was cancelled."
                            if cancelled
                            else "Local code execution failed safely."
                        )
                    },
                    error="cancelled" if cancelled else failure_code,
                    expected_status=current.status,
                )
                return
            except ExecutionTransitionConflict:
                continue
            except Exception:
                return

    @staticmethod
    def _scratch_request_from_job(job: ExecutionJob) -> ScratchComputeRequest:
        payload = job.payload
        if set(payload) != {"schema_version", "expression"} or payload.get(
            "schema_version"
        ) != SCRATCH_PAYLOAD_SCHEMA:
            raise ScratchComputeError("recovery_invalid_payload")
        expression = payload.get("expression")
        if not isinstance(expression, str):
            raise ScratchComputeError("recovery_invalid_payload")
        try:
            validate_scratch_expression(expression)
            return ScratchComputeRequest(
                owner=job.owner,
                request_id=job.request_id,
                expression=expression,
            )
        except (ScratchComputeError, TypeError, ValueError):
            raise ScratchComputeError("recovery_invalid_payload") from None

    def _recover_scratch(self, job: ExecutionJob) -> None:
        if job.status in TerminalExecutionStatus:
            return
        if job.status == "cancelling":
            try:
                self.repository.transition(
                    job.job_id,
                    status="cancelled",
                    event="cancelled",
                    phase="recovery",
                    data={"message": "Safe computation cancellation was recovered."},
                    error="cancelled",
                )
            except Exception:
                pass
            return
        try:
            request = self._scratch_request_from_job(job)
        except ScratchComputeError:
            self._fail_scratch_recovery(job.job_id, "recovery_invalid_payload")
            return
        self._launch_scratch(job.job_id, request)

    def _launch_scratch(self, job_id: str, request: ScratchComputeRequest) -> None:
        with self._scratch_lock:
            existing = self._scratch_threads.get(job_id)
            if existing is not None and existing.is_alive():
                return
            event = self._scratch_cancel_events.setdefault(job_id, Event())
            thread = Thread(
                target=self._run_scratch,
                args=(job_id, request, event),
                name=f"cortex-scratch-{job_id}",
                daemon=True,
            )
            self._scratch_threads[job_id] = thread
            thread.start()

    def _run_scratch(
        self,
        job_id: str,
        request: ScratchComputeRequest,
        cancel_event: Event,
    ) -> None:
        lease_owner = f"scratch-coordinator-{uuid4().hex}"
        attempt: LocalScratchAttempt | None = None
        with self._scratch_lock:
            self._scratch_cancel_events[job_id] = cancel_event
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
                self._cancel_scratch_before_start(job_id)
                return
            self.repository.transition(
                job_id,
                status="running",
                event="started",
                phase="prepare",
                data={"message": "Safe computation started."},
            )
            self.repository.transition(
                job_id,
                status="running",
                event="progress",
                phase="worker",
                data={"message": "Computing in an isolated local worker."},
            )
            attempt = LocalScratchAttempt(
                timeout_seconds=self.scratch_timeout_seconds,
                startup_timeout_seconds=self.scratch_startup_timeout_seconds,
            )
            with self._scratch_lock:
                self._scratch_attempts[job_id] = attempt
            result = attempt.evaluate(request.expression, cancel_event)
            current = self.repository.get_job(job_id)
            if cancel_event.is_set() or (current is not None and current.status == "cancelling"):
                raise ScratchComputeError("cancelled")
            self.repository.transition(
                job_id,
                status="succeeded",
                event="completed",
                phase="completed",
                data={"message": "Safe computation completed."},
                result=scratch_result_payload(result.value),
            )
        except ScratchComputeError as exc:
            self._finish_scratch_failure(job_id, cancel_event, exc.code)
        except LeaseConflict:
            self._fail_scratch_recovery(job_id, "lease_unavailable")
        except Exception:
            self._finish_scratch_failure(job_id, cancel_event, "coordinator_failed")
        finally:
            if attempt is not None:
                attempt.close()
            with self._scratch_lock:
                self._scratch_attempts.pop(job_id, None)
                self._scratch_cancel_events.pop(job_id, None)
                self._scratch_threads.pop(job_id, None)
            try:
                self.repository.release_lease(job_id, lease_owner=lease_owner)
            except Exception:
                pass

    def _cancel_scratch_before_start(self, job_id: str) -> None:
        self.repository.transition(
            job_id,
            status="cancelled",
            event="cancelled",
            phase="cancelled",
            data={"message": "Safe computation cancelled before start."},
            error="cancelled",
        )

    def _finish_scratch_failure(
        self,
        job_id: str,
        cancel_event: Event,
        failure_code: str,
    ) -> None:
        current = self.repository.get_job(job_id)
        cancelled = (
            failure_code == "cancelled"
            or cancel_event.is_set()
            or (current is not None and current.status == "cancelling")
        )
        try:
            self.repository.transition(
                job_id,
                status="cancelled" if cancelled else "failed",
                event="cancelled" if cancelled else "failed",
                phase="cancelled" if cancelled else "failed",
                data={
                    "message": (
                        "Safe computation was cancelled."
                        if cancelled
                        else "Safe computation failed safely."
                    )
                },
                error="cancelled" if cancelled else failure_code,
            )
        except Exception:
            pass

    def _fail_scratch_recovery(self, job_id: str, code: str) -> None:
        try:
            self.repository.transition(
                job_id,
                status="failed",
                event="failed",
                phase="recovery",
                data={"message": "Safe computation recovery metadata is invalid."},
                error=code,
            )
        except Exception:
            pass


__all__ = [
    "DEFAULT_CODE_TIMEOUT_SECONDS",
    "DEFAULT_CODE_STARTUP_TIMEOUT_SECONDS",
    "DEFAULT_IMAGE_TIMEOUT_SECONDS",
    "DEFAULT_SCRATCH_TIMEOUT_SECONDS",
    "DEFAULT_SCRATCH_STARTUP_TIMEOUT_SECONDS",
    "LocalExecutionCoordinator",
    "LocalRecipeWorkerAttempt",
]
