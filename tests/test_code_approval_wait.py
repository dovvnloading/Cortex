"""A code job waiting for its approval must cost nothing while it waits.

Every pending code job used to own a thread that read the job from SQLite twenty
times a second and asked for the expiry sweep, a write transaction, four times a
second, for the whole five-minute approval window. A task left in the tray cost
roughly six thousand selects and twelve hundred write transactions, and that
load widened the race windows elsewhere in the store.

The waiter now sleeps until something it could act on happens: a decision, an
expiry, a cancellation or shutdown. The repository announces a decision or an
expiry, and each waiter also wakes at its own approval's deadline.
"""

from __future__ import annotations

from collections.abc import Callable
import threading
import time

import pytest

from cortex_backend.execution import local_runtime
from cortex_backend.execution.code_execution import CODE_EXECUTION_PROFILE, CodeExecutionRequest
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.models import ExecutionJob
from cortex_backend.execution.repository import ApprovalTransitionError, ExecutionRepository
from support import FrozenClock, wait_until


def _submit(coordinator: LocalExecutionCoordinator, name: str, source: str = "_result = 7") -> ExecutionJob:
    repository = coordinator.repository
    return coordinator.start_code(
        CodeExecutionRequest(
            owner=repository.installation_principal_id,
            request_id=name,
            source=source,
            intent_summary="Wait for a decision that never comes.",
        )
    )


def _let_the_waiter_settle(coordinator: LocalExecutionCoordinator, job_ids: list[str]) -> None:
    wait_until(
        lambda: set(job_ids) <= coordinator.active_code_job_ids(),
        describe=f"the code threads for {job_ids} to launch",
    )
    # Long enough for the old loop to have polled many times; the new one is
    # parked in a wait by now.
    threading.Event().wait(0.3)


def _count_calls(repository: ExecutionRepository, monkeypatch: pytest.MonkeyPatch, *names: str) -> dict[str, int]:
    counts = dict.fromkeys(names, 0)

    def wrap(name: str) -> None:
        original: Callable[..., object] = getattr(repository, name)

        def counted(*args: object, **kwargs: object) -> object:
            counts[name] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(repository, name, counted)

    for name in names:
        wrap(name)
    return counts


def test_a_job_waiting_for_approval_does_not_poll_the_store(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Counting from before the job is submitted keeps this independent of how
    # quickly the waiter's thread gets scheduled on a busy machine.
    counts = _count_calls(coordinator.repository, monkeypatch, "get_job", "expire_approvals")
    job = _submit(coordinator, "wait-does-not-poll")
    _let_the_waiter_settle(coordinator, [job.job_id])

    threading.Event().wait(0.6)

    # Setting up costs a handful of reads. The old loop added about twenty reads
    # a second and a write transaction every quarter second on top of that.
    assert counts["expire_approvals"] == 0, counts
    assert counts["get_job"] <= 6, counts


def test_many_waiting_jobs_are_as_quiet_as_one(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _count_calls(coordinator.repository, monkeypatch, "get_job", "expire_approvals")
    jobs = [_submit(coordinator, f"wait-many-{index}") for index in range(6)]
    _let_the_waiter_settle(coordinator, [job.job_id for job in jobs])

    threading.Event().wait(0.5)

    assert counts["expire_approvals"] == 0, counts
    assert counts["get_job"] <= 6 * len(jobs), counts


def test_a_decision_wakes_the_waiter_without_waiting_for_a_recheck(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the safety-net recheck pushed far out, only the signal can wake it.
    monkeypatch.setattr(local_runtime, "_APPROVAL_RECHECK_SECONDS", 60.0)
    repository = coordinator.repository
    job = _submit(coordinator, "wait-decision-wakes")
    _let_the_waiter_settle(coordinator, [job.job_id])

    repository.decide_approval(job.job_id, owner=repository.installation_principal_id, decision="approved")

    completed = coordinator.wait(job.job_id, timeout=5.0)
    assert completed.status == "succeeded"
    assert completed.result is not None and completed.result["value"] == 7


def test_a_denial_wakes_the_waiter_and_nothing_runs(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_runtime, "_APPROVAL_RECHECK_SECONDS", 60.0)
    repository = coordinator.repository
    job = _submit(coordinator, "wait-denial-wakes")
    _let_the_waiter_settle(coordinator, [job.job_id])

    repository.decide_approval(job.job_id, owner=repository.installation_principal_id, decision="denied")

    wait_until(
        lambda: job.job_id not in coordinator.active_code_job_ids(),
        describe="the denied job's thread to exit",
    )
    final = repository.get_job(job.job_id)
    assert final is not None
    assert (final.status, final.approval_state, final.result) == ("cancelled", "denied", None)


def test_an_approval_still_expires_at_its_deadline(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expiry must not depend on polling: the waiter wakes at the approval's own deadline."""

    monkeypatch.setattr(local_runtime, "_APPROVAL_RECHECK_SECONDS", 60.0)
    repository = coordinator.repository
    owner = repository.installation_principal_id
    request = CodeExecutionRequest(
        owner=owner,
        request_id="wait-expires-at-deadline",
        source="_result = 1",
        intent_summary="Let the approval lapse.",
    )
    job, _ = repository.create_job(
        job_id="expiring-approval",
        owner=owner,
        request_id=request.request_id,
        profile=CODE_EXECUTION_PROFILE,
        payload=request.payload(),
    )
    repository.request_approval(
        job.job_id, owner=owner, scope_digest=request.approval_scope_digest, reason="synthetic", ttl_seconds=0.4
    )
    counts = _count_calls(repository, monkeypatch, "expire_approvals")
    coordinator._launch_code(job.job_id)

    expired = coordinator.wait(job.job_id, timeout=5.0)

    assert (expired.status, expired.approval_state, expired.error) == (
        "cancelled",
        "expired",
        "approval_expired",
    )
    assert repository.get_approval(job.job_id).state == "expired"  # type: ignore[union-attr]
    assert counts["expire_approvals"] <= 2, counts
    assert repository.events(job.job_id)[-1].event == "code.cancelled"


def test_shutdown_releases_waiting_jobs_at_once(
    coordinator_factory: Callable[..., LocalExecutionCoordinator], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_runtime, "_APPROVAL_RECHECK_SECONDS", 60.0)
    coordinator = coordinator_factory(code_timeout_seconds=3.0)
    jobs = [_submit(coordinator, f"wait-shutdown-{index}") for index in range(3)]
    _let_the_waiter_settle(coordinator, [job.job_id for job in jobs])

    started = time.monotonic()
    coordinator.shutdown(timeout=5.0)
    elapsed = time.monotonic() - started

    assert coordinator.active_code_job_ids() == frozenset()
    assert elapsed < 3.0, f"shutdown took {elapsed:.1f}s with the recheck set to 60s"


def test_a_cancellation_wakes_the_waiter(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_runtime, "_APPROVAL_RECHECK_SECONDS", 60.0)
    repository = coordinator.repository
    job = _submit(coordinator, "wait-cancel-wakes")
    _let_the_waiter_settle(coordinator, [job.job_id])

    cancelled = coordinator.cancel(job.job_id, owner=repository.installation_principal_id)

    assert cancelled.status == "cancelled"
    wait_until(
        lambda: job.job_id not in coordinator.active_code_job_ids(),
        describe="the cancelled job's thread to exit",
    )


# --- the repository's half of the contract ----------------------------------


def test_the_repository_announces_decisions_and_expiries(
    execution_repository: ExecutionRepository, frozen_clock: FrozenClock
) -> None:
    repository = execution_repository
    owner = repository.installation_principal_id
    versions = [repository.approval_changes.version]

    def next_version() -> int:
        versions.append(repository.approval_changes.version)
        return versions[-1] - versions[-2]

    def pending(job_id: str) -> None:
        request = CodeExecutionRequest(
            owner=owner, request_id=f"req-{job_id}", source="_result = 1", intent_summary="synthetic"
        )
        repository.create_job(
            job_id=job_id,
            owner=owner,
            request_id=request.request_id,
            profile=CODE_EXECUTION_PROFILE,
            payload=request.payload(),
        )
        repository.request_approval(
            job_id, owner=owner, scope_digest=request.approval_scope_digest, reason="synthetic"
        )

    pending("approved-job")
    assert next_version() == 0  # asking for an approval is not a change a waiter acts on
    repository.decide_approval("approved-job", owner=owner, decision="approved")
    assert next_version() >= 1

    pending("denied-job")
    repository.decide_approval("denied-job", owner=owner, decision="denied")
    assert next_version() >= 1

    pending("expiring-job")
    next_version()
    frozen_clock.advance(301)
    assert repository.expire_approvals() == ["expiring-job"]
    assert next_version() >= 1

    assert repository.expire_approvals() == []
    assert next_version() == 0  # a sweep that expired nothing changes nothing

    pending("late-job")
    next_version()
    frozen_clock.advance(301)
    with pytest.raises(ApprovalTransitionError):
        repository.decide_approval("late-job", owner=owner, decision="approved")
    assert next_version() >= 1  # a decision that found the approval expired still settled it


def test_pending_approval_seconds_follows_the_repository_clock(
    execution_repository: ExecutionRepository, frozen_clock: FrozenClock
) -> None:
    repository = execution_repository
    owner = repository.installation_principal_id
    request = CodeExecutionRequest(
        owner=owner, request_id="req-seconds", source="_result = 1", intent_summary="synthetic"
    )
    repository.create_job(
        job_id="seconds-job",
        owner=owner,
        request_id=request.request_id,
        profile=CODE_EXECUTION_PROFILE,
        payload=request.payload(),
    )
    assert repository.pending_approval_seconds("seconds-job") is None  # no approval yet
    repository.request_approval(
        "seconds-job", owner=owner, scope_digest=request.approval_scope_digest, reason="synthetic", ttl_seconds=200
    )

    assert repository.pending_approval_seconds("seconds-job") == pytest.approx(200, abs=1)
    frozen_clock.advance(150)
    assert repository.pending_approval_seconds("seconds-job") == pytest.approx(50, abs=1)
    frozen_clock.advance(100)
    remaining = repository.pending_approval_seconds("seconds-job")
    assert remaining is not None and remaining < 0  # overdue, and still pending on disk

    assert repository.pending_approval_seconds("no-such-job") is None


def test_a_change_between_reading_the_counter_and_waiting_is_not_missed(
    execution_repository: ExecutionRepository,
) -> None:
    signal = execution_repository.approval_changes
    seen = signal.version

    signal.bump()  # lands after the read and before the wait

    started = time.monotonic()
    assert signal.wait(seen, timeout=5.0) is True
    assert time.monotonic() - started < 1.0


def test_waiting_on_an_unchanged_counter_times_out(execution_repository: ExecutionRepository) -> None:
    signal = execution_repository.approval_changes

    assert signal.wait(signal.version, timeout=0.05) is False


def test_a_bump_wakes_a_thread_that_is_already_waiting(execution_repository: ExecutionRepository) -> None:
    signal = execution_repository.approval_changes
    seen = signal.version
    woke: list[bool] = []
    parked = threading.Event()

    def waiter() -> None:
        parked.set()
        woke.append(signal.wait(seen, timeout=10.0))

    thread = threading.Thread(target=waiter, name="cortex-test-approval-waiter")
    thread.start()
    assert parked.wait(timeout=5.0)
    signal.bump()
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert woke == [True]
