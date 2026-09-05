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

from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.repository import ExecutionRepository
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
