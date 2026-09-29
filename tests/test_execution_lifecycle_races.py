"""Two lifecycle races that cost a user real work.

Both are timing gaps between two transactions, and both were reachable with
ordinary use: a double-submitted form, and reopening the app after a crash.
"""

from __future__ import annotations

import time

import pytest

from cortex_backend.execution.code_execution import CodeExecutionRequest
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.repository import ExecutionRepository, LeaseConflict


def test_a_duplicate_submission_does_not_fail_the_job_it_duplicates(coordinator) -> None:
    """start_code writes the job and its approval in two transactions.

    A duplicate landing in that gap saw a live job with no approval row yet,
    relaunched it, and the second worker failed the whole job
    "approval_required" -- destroying the submission the user was waiting on.
    A client retry, a double click, or a StrictMode double-invoke is enough.
    """
    repository = coordinator.repository
    owner = repository.installation_principal_id
    request = CodeExecutionRequest(
        owner=owner, request_id="dup-1", source="_result = 1", intent_summary="add"
    )
    # The exact gap: the job row committed, the approval row not yet.
    job, created = repository.create_job(
        job_id="job-1",
        owner=owner,
        request_id="dup-1",
        profile="code.exec.v1",
        payload=request.payload(),
    )
    assert created and job.approval_state == "not_required"

    coordinator.start_code(request)
    time.sleep(0.4)
    final = repository.get_job("job-1", owner=owner)

    assert final is not None
    assert final.status != "failed", (
        f"the duplicate failed the original job: {final.error}"
    )


def test_startup_reclaims_a_supervisor_lease_left_by_a_killed_process(coordinator_factory) -> None:
    """A crash leaves the lease held for up to its full 60s TTL.

    The next launch gets a fresh owner id, so the claim used to raise
    LeaseConflict; the lifecycle catches that, marks itself blocked, and every
    execution capability -- safe compute, code, image recipes -- stays off for
    the whole session. Reopening the app promptly after a crash is exactly
    what a user does.

    Reclaiming is safe because the launcher holds an OS-level per-profile
    instance lock for its lifetime, so no live process can hold this lease.
    """
    killed = coordinator_factory()
    killed.startup_recover()  # and then the process dies without releasing

    # The new process reaches the same files through its own repository object.
    relaunched = LocalExecutionCoordinator(
        ExecutionRepository(killed.repository.db_path, killed.repository.artifact_root)
    )
    try:
        relaunched.startup_recover()
    finally:
        relaunched.shutdown()


def test_a_live_supervisor_still_refuses_a_concurrent_claim(coordinator) -> None:
    """The lease must still do its job for anything that is not a restart."""
    coordinator.startup_recover()
    with pytest.raises(LeaseConflict):
        coordinator.repository.claim_supervisor_lease(lease_owner="someone-else", ttl_seconds=30.0)
