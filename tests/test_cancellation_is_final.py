"""Work a user cancelled must never be reported as succeeded.

Each coordinator reads the job status, decides it was not cancelled, and then
writes its terminal status. A Stop committing between those two steps was
simply overwritten: the API had already answered "cancelling", and the job
then reported "succeeded" with an event log reading
['queued', 'started', 'cancelling', 'completed'].

The code-execution path already guarded its write with expected_status. These
tests hold the other two to the same rule.
"""

from __future__ import annotations

from pathlib import Path
import time

import pytest

from cortex_backend.execution.code_execution import CodeExecutionRequest
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.repository import (
    ExecutionRepository,
    ExecutionTransitionConflict,
)
from cortex_backend.execution.scratch_compute import ScratchComputeRequest


class _CancelsDuringTheCheck:
    """Commit the cancel in the gap the coordinator cannot see.

    get_job is what the pre-write check calls. Answering it once with the
    live row and committing a cancel immediately afterwards reproduces the
    interleaving exactly, without depending on thread timing.
    """

    def __init__(self, inner: ExecutionRepository, job_id: str) -> None:
        self._inner = inner
        self._job_id = job_id
        self._armed = True

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def get_job(self, job_id: str, *, owner: str | None = None):
        job = self._inner.get_job(job_id, owner=owner)
        if self._armed and job is not None and job.status == "running":
            self._armed = False
            self._inner.request_cancel(self._job_id)
        return job


@pytest.fixture
def repository(tmp_path: Path) -> ExecutionRepository:
    return ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")


def test_a_scratch_result_cannot_overwrite_a_committed_cancellation(repository) -> None:
    coordinator = LocalExecutionCoordinator(repository)
    owner = repository.installation_principal_id
    request = ScratchComputeRequest(owner=owner, request_id="cancel-1", expression="2 + 2")

    job = coordinator.start_scratch(request)

    # Swap in the racing repository for the worker's terminal write.
    coordinator.repository = _CancelsDuringTheCheck(repository, job.job_id)
    deadline = time.monotonic() + 10
    final = None
    while time.monotonic() < deadline:
        final = repository.get_job(job.job_id, owner=owner)
        if final is not None and final.status in {"succeeded", "failed", "cancelled", "cancelling"}:
            if final.status != "cancelling":
                break
        time.sleep(0.02)
    coordinator.shutdown()

    assert final is not None
    assert final.status != "succeeded", (
        "a cancelled computation reported success; the terminal write "
        "overwrote the cancellation the API had already acknowledged"
    )


def test_the_store_refuses_a_running_write_once_cancellation_is_committed(
    repository,
) -> None:
    """The pre-run writes were unguarded, so Stop could be silently undone.

    Only the terminal writes carried expected_status. The two "running"
    transitions a code job makes on its way to the worker did not, and
    transition() refused only *terminal* rows -- "cancelling" is not terminal.
    A worker that read the job just before the cancel committed therefore
    overwrote it, and the stopped program ran to completion and was recorded
    as succeeded.

    The invariant now lives in the store, so every profile and every
    capability added later inherits it.
    """
    owner = repository.installation_principal_id
    repository.create_job(
        job_id="cancelled-before-running",
        owner=owner,
        request_id="stop-then-run",
        profile="code.exec.v1",
        payload={
            "schema_version": 1,
            "language": "python",
            "source": "_result = 1",
            "intent_summary": "probe",
            "capabilities": {"filesystem": False, "process": False, "network": False},
            "source_digest": "unused",
        },
    )
    repository.request_cancel("cancelled-before-running")
    assert repository.get_job("cancelled-before-running").status == "cancelling"

    with pytest.raises(ExecutionTransitionConflict):
        repository.transition(
            "cancelled-before-running",
            status="running",
            event="code.started",
            phase="prepare",
            data={"message": "should never be recorded"},
        )
    assert repository.get_job("cancelled-before-running").status == "cancelling"

    # A terminal status is still reachable, or the job could never finish.
    repository.transition(
        "cancelled-before-running",
        status="cancelled",
        event="code.cancelled",
        phase="cancelled",
        data={"message": "Local code execution was cancelled."},
        error="cancelled",
    )
    assert repository.get_job("cancelled-before-running").status == "cancelled"


def test_a_code_job_cancelled_outside_the_coordinator_never_runs(repository) -> None:
    """A cancel that does not set the in-process event must still stop the job.

    Another process, a recovery pass, or a direct request_cancel all reach the
    store without touching the coordinator's Event, which is what made this
    failure invisible: every in-process check passed and the program ran.
    """
    coordinator = LocalExecutionCoordinator(repository)
    owner = repository.installation_principal_id
    try:
        job = coordinator.start_code(
            CodeExecutionRequest(
                owner=owner,
                request_id="outside-cancel",
                source="_result = 40 + 2",
                intent_summary="add two numbers",
            )
        )
        repository.decide_approval(job.job_id, owner=owner, decision="approved")
        repository.request_cancel(job.job_id)

        final = coordinator.wait(job.job_id, timeout=10.0)
    finally:
        coordinator.shutdown()

    assert final.status == "cancelled"
    events = [event.event for event in repository.events(job.job_id)]
    assert "code.completed" not in events, (
        f"a cancelled program ran to completion: {events}"
    )


def test_cancelling_twice_is_idempotent(repository) -> None:
    """Pressing Stop again must not be an error.

    Making "cancelling" a one-way state has to keep it reachable from itself:
    request_cancel() writes status="cancelling", so a second Stop -- or a
    recovery pass re-requesting one -- would otherwise raise a transition
    conflict, which the execution router reports to the user as "Execution job
    not found".
    """
    owner = repository.installation_principal_id
    repository.create_job(
        job_id="cancel-twice",
        owner=owner,
        request_id="stop-stop",
        profile="code.exec.v1",
        payload={
            "schema_version": 1,
            "language": "python",
            "source": "_result = 1",
            "intent_summary": "probe",
            "capabilities": {"filesystem": False, "process": False, "network": False},
            "source_digest": "unused",
        },
    )

    assert repository.request_cancel("cancel-twice").status == "cancelling"
    assert repository.request_cancel("cancel-twice").status == "cancelling"

    # Still one-way, and still able to finish.
    with pytest.raises(ExecutionTransitionConflict):
        repository.transition(
            "cancel-twice",
            status="running",
            event="code.started",
            phase="prepare",
            data={},
        )
    repository.transition(
        "cancel-twice",
        status="cancelled",
        event="code.cancelled",
        phase="cancelled",
        data={},
        error="cancelled",
    )
    # A cancel after the job is terminal stays idempotent too.
    assert repository.request_cancel("cancel-twice").status == "cancelled"
