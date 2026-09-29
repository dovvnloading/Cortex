"""Durable execution store: idempotency, replay, leases, and retention."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import dataclasses
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
import time

import pytest

from cortex_backend.execution.models import ExecutionJob
from cortex_backend.execution.repository import (
    ArtifactLimitError,
    ExecutionIntegrityError,
    ExecutionRepository,
    ExecutionRepositoryError,
    LeaseConflict,
)
from cortex_backend.testing import DurableFakeCoordinator
from cortex_backend.testing import FakeExecutionPlan
from cortex_backend.execution.repository import ApprovalPolicyError, ApprovalTransitionError
from support import wait_until


def _repository(tmp_path):
    return ExecutionRepository(
        tmp_path / "execution.sqlite",
        tmp_path / "artifacts",
        max_artifact_bytes=64,
    )


def test_durable_idempotency_event_replay_and_restart_recovery(tmp_path, frozen_clock):
    repository = _repository(tmp_path)
    with repository.connect() as connection:
        assert connection.execute("SELECT version FROM execution_schema WHERE id = 1").fetchone()[0] == 3
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "execution_approvals",
        "execution_supervisor_leases",
        "execution_installation_principal",
    } <= tables
    first, created = repository.create_job(
        job_id="job-1",
        owner="session-a",
        request_id="request-1",
        profile="fake.v1",
        payload={"provider": "fake-v1"},
    )
    assert created is True
    duplicate, duplicate_created = repository.create_job(
        job_id="job-duplicate",
        owner="session-a",
        request_id="request-1",
        profile="fake.v1",
        payload={"provider": "fake-v1"},
    )
    assert duplicate_created is False
    assert duplicate.job_id == first.job_id

    repository.claim_lease(first.job_id, lease_owner="dead-coordinator", ttl_seconds=30)
    frozen_clock.advance(31)
    assert repository.recover_expired_leases() == [first.job_id]

    restarted = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts", max_artifact_bytes=64)
    recovered_events = restarted.events(first.job_id)
    assert [event.sequence for event in recovered_events] == list(range(1, len(recovered_events) + 1))
    assert recovered_events[-1].event == "recovered"
    assert restarted.get_job(first.job_id).status == "queued"


def test_leases_reject_live_foreign_owner_and_allow_expiry_recovery(tmp_path):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-lease",
        owner="session-a",
        request_id="request-lease",
        profile="fake.v1",
        payload={},
    )
    repository.claim_lease(job.job_id, lease_owner="coordinator-a", ttl_seconds=10)
    with pytest.raises(LeaseConflict):
        repository.claim_lease(job.job_id, lease_owner="coordinator-b", ttl_seconds=10)


def test_lease_expiry_is_driven_by_the_repository_clock_not_wall_time(tmp_path, frozen_clock):
    """An hour-long lease lapses the instant the clock says so, with no waiting."""
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-clock-lease",
        owner="session-a",
        request_id="request-clock-lease",
        profile="fake.v1",
        payload={},
    )
    repository.claim_lease(job.job_id, lease_owner="coordinator-a", ttl_seconds=3600)

    frozen_clock.advance(3599)
    with pytest.raises(LeaseConflict):
        repository.claim_lease(job.job_id, lease_owner="coordinator-b", ttl_seconds=3600)
    assert repository.recover_expired_leases() == []
    assert repository.lease_holder(job.job_id) == "coordinator-a"

    frozen_clock.advance(2)
    assert repository.recover_expired_leases() == [job.job_id]
    assert repository.lease_holder(job.job_id) is None
    repository.claim_lease(job.job_id, lease_owner="coordinator-b", ttl_seconds=3600)
    assert repository.lease_holder(job.job_id) == "coordinator-b"


def test_an_approval_expires_by_the_repository_clock_not_wall_time(tmp_path, frozen_clock):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-clock-approval",
        owner="session-a",
        request_id="request-clock-approval",
        profile="artifact.extended.v1",
        payload={},
    )
    repository.request_approval(
        job.job_id, owner="session-a", scope_digest="scope", reason="test", ttl_seconds=300
    )

    frozen_clock.advance(299)
    assert repository.get_job(job.job_id).approval_state == "pending"
    assert repository.expire_approvals() == []

    frozen_clock.advance(2)
    assert repository.get_job(job.job_id).approval_state == "expired"
    assert repository.expire_approvals() == [job.job_id]


def test_lease_holder_names_the_recorded_owner_until_release(tmp_path, frozen_clock):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-lease-holder",
        owner="session-a",
        request_id="request-lease-holder",
        profile="fake.v1",
        payload={},
    )
    assert repository.lease_holder(job.job_id) is None
    assert repository.lease_holder("job-that-does-not-exist") is None

    repository.claim_lease(job.job_id, lease_owner="coordinator-a", ttl_seconds=30)
    assert repository.lease_holder(job.job_id) == "coordinator-a"

    # It reports the row, not liveness: an expired lease still names its owner.
    frozen_clock.advance(31)
    assert repository.lease_holder(job.job_id) == "coordinator-a"

    repository.release_lease(job.job_id, lease_owner="someone-else")
    assert repository.lease_holder(job.job_id) == "coordinator-a"
    repository.release_lease(job.job_id, lease_owner="coordinator-a")
    assert repository.lease_holder(job.job_id) is None


def test_a_lease_lapses_in_real_time_and_the_restarted_coordinator_finishes_the_job(tmp_path):
    """The one test that runs on the real clock, so the frozen-clock ones stay honest.

    It never asserts that the lease is still live, so a stalled machine cannot
    fail it; it waits for the wall clock to pass the expiry the repository
    reported and then bounds the recovery with coordinator.wait.
    """
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-real-time-lease",
        owner="session-a",
        request_id="request-real-time-lease",
        profile="fake.v1",
        payload={
            "provider": "fake-v1",
            "outcome": "success",
            "steps": 2,
            "step_delay_seconds": 0.01,
            "failure_message": "Deterministic fake execution failed.",
        },
    )
    expires_at = datetime.fromisoformat(
        repository.claim_lease(job.job_id, lease_owner="dead-worker", ttl_seconds=0.25)
    )
    wait_until(
        lambda: datetime.now(timezone.utc) > expires_at,
        describe="the wall clock to pass the lease expiry",
    )

    coordinator = DurableFakeCoordinator(repository)
    try:
        finished = coordinator.wait(job.job_id, timeout=10.0)
        assert finished.status == "succeeded"
        assert [event.event for event in repository.events(job.job_id)].count("recovered") == 1
    finally:
        coordinator.shutdown()


def test_expired_lease_does_not_resurrect_a_persisted_cancellation(tmp_path, frozen_clock):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-cancelled-before-restart",
        owner="session-a",
        request_id="request-cancelled-before-restart",
        profile="fake.v1",
        payload={},
    )
    repository.claim_lease(
        job.job_id,
        lease_owner="dead-coordinator",
        ttl_seconds=30,
    )
    repository.request_cancel(job.job_id)
    frozen_clock.advance(31)

    assert repository.recover_expired_leases() == [job.job_id]
    recovered = repository.get_job(job.job_id)
    assert recovered is not None
    assert recovered.status == "cancelled"
    assert recovered.error == "Execution cancelled."


def test_artifact_store_is_hash_verified_bounded_and_cleaned(tmp_path):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-artifact",
        owner="session-a",
        request_id="request-artifact",
        profile="fake.v1",
        payload={},
    )
    content = b"stored-artifact"
    artifact = repository.publish_artifact(
        job.job_id,
        name="result.txt",
        content=content,
        mime_type="text/plain",
        retention_seconds=1,
    )
    assert artifact.sha256 == hashlib.sha256(content).hexdigest()
    assert repository.read_artifact(artifact.artifact_id) == content
    with pytest.raises(ArtifactLimitError):
        repository.publish_artifact(job.job_id, name="too-large.bin", content=b"x" * 65)
    with pytest.raises(ExecutionRepositoryError):
        repository.publish_artifact(job.job_id, name="..\\escape.txt", content=b"no")
    future = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    assert repository.cleanup_expired(now=future).artifacts == 1
    with pytest.raises(ExecutionRepositoryError):
        repository.read_artifact(artifact.artifact_id)


def test_purging_a_terminal_job_keeps_fresh_artifacts_for_retention(tmp_path):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-terminal-artifact",
        owner="session-a",
        request_id="request-terminal-artifact",
        profile="fake.v1",
        payload={},
    )
    artifact = repository.publish_artifact(
        job.job_id,
        name="result.txt",
        content=b"keep this file accounted for",
        mime_type="text/plain",
        retention_seconds=3_600,
    )
    repository.transition(
        job.job_id,
        status="succeeded",
        event="completed",
        phase="completed",
        data={"ok": True},
    )

    assert repository.cleanup_expired(
        now=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    ).artifacts == 0
    assert Path(artifact.path).exists()
    assert repository.get_artifact(artifact.artifact_id) is not None


def test_terminal_state_is_immutable_and_wait_has_a_real_timeout(tmp_path):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-terminal",
        owner="session-a",
        request_id="request-terminal",
        profile="fake.v1",
        payload={},
    )
    finished = repository.transition(
        job.job_id,
        status="succeeded",
        event="completed",
        phase="completed",
        data={"value": 42},
        result={"value": 42},
    )
    late = repository.transition(
        job.job_id,
        status="failed",
        event="failed",
        phase="failed",
        data={"message": "late"},
        error="late",
    )
    assert late == finished
    assert [event.event for event in repository.events(job.job_id)] == ["queued", "completed"]

    coordinator = DurableFakeCoordinator(repository)
    try:
        waiting, _ = repository.create_job(
            job_id="job-waiting",
            owner="session-a",
            request_id="request-waiting",
            profile="fake.v1",
            payload={},
        )
        with pytest.raises(TimeoutError):
            coordinator.wait(waiting.job_id, timeout=0)
    finally:
        coordinator.shutdown()


def test_transition_on_an_already_terminal_job_reports_real_approval_state(tmp_path):
    """A second transition() call on an already-terminal job must report the
    real approval_state, not silently fall back to "not_required".

    transition()'s terminal-state early return (the race guard against a
    late or duplicate transition call on a job that already reached a
    terminal status) used to re-read the row with a bare
    `SELECT * FROM execution_jobs`, which has no execution_approvals join
    and therefore no approval_state column -- so _job_from_row() defaulted
    to "not_required" even for a job that had actually been approved before
    it finished. Same pattern as the fix for create_job()'s
    duplicate-request fallback.
    """
    repository = _repository(tmp_path)
    job, created = repository.create_job(
        job_id="job-terminal-approval-retry",
        owner="session-a",
        request_id="request-terminal-approval-retry",
        profile="artifact.extended.v1",
        payload={},
    )
    assert created is True
    assert (
        repository.request_approval(
            job.job_id,
            owner="session-a",
            scope_digest="scope",
            reason="test",
            ttl_seconds=10,
        )
        == "pending"
    )
    assert (
        repository.decide_approval(
            job.job_id,
            owner="session-a",
            decision="approved",
        )
        == "approved"
    )

    finished = repository.transition(
        job.job_id,
        status="succeeded",
        event="completed",
        phase="completed",
        data={"value": 42},
        result={"value": 42},
    )
    assert finished.status == "succeeded"

    # A second, late transition call on the now-terminal job hits the
    # race-guard early return -- it must not lose the real, already-decided
    # approval state.
    late = repository.transition(
        job.job_id,
        status="failed",
        event="failed",
        phase="failed",
        data={"message": "late"},
        error="late",
    )
    assert late.job_id == finished.job_id
    assert late.status == finished.status
    assert late.approval_state == "approved"


def test_transition_to_terminal_status_reports_real_approval_state_on_first_call(tmp_path):
    """The FIRST transition() call that moves an approved job into a
    terminal status must report the real approval_state, not silently fall
    back to "not_required".

    transition()'s normal (non-terminal-branch) success path used to re-read
    the just-updated row with a bare `SELECT * FROM execution_jobs`, which
    has no execution_approvals join and therefore no approval_state column
    -- so _job_from_row() defaulted to "not_required" even when the job had
    just been approved. This hits every job's first terminal transition, not
    only a retried/duplicate one. Same pattern as the fixes for
    create_job()'s duplicate-request fallback and transition()'s
    already-terminal race guard.
    """
    repository = _repository(tmp_path)
    job, created = repository.create_job(
        job_id="job-first-terminal-approval",
        owner="session-a",
        request_id="request-first-terminal-approval",
        profile="artifact.extended.v1",
        payload={},
    )
    assert created is True
    assert (
        repository.request_approval(
            job.job_id,
            owner="session-a",
            scope_digest="scope",
            reason="test",
            ttl_seconds=10,
        )
        == "pending"
    )
    assert (
        repository.decide_approval(
            job.job_id,
            owner="session-a",
            decision="approved",
        )
        == "approved"
    )

    # This is the job's FIRST transition into a terminal status -- it takes
    # the normal success path, not the already-terminal race guard.
    finished = repository.transition(
        job.job_id,
        status="succeeded",
        event="completed",
        phase="completed",
        data={"value": 42},
        result={"value": 42},
    )
    assert finished.status == "succeeded"
    assert finished.approval_state == "approved"


def _advancing_get_job(repository, monkeypatch):
    """Make every get_job() call move the job on, as a racing worker would."""
    original = repository.get_job
    calls: list[str] = []

    def advancing(job_id, *, owner=None):
        calls.append(job_id)
        with repository.connect() as connection:
            connection.execute(
                "UPDATE execution_jobs SET status = 'running' WHERE job_id = ?",
                (job_id,),
            )
        return original(job_id, owner=owner)

    monkeypatch.setattr(repository, "get_job", advancing)
    return calls


def test_request_cancel_returns_the_cancelling_row_it_committed(tmp_path, monkeypatch):
    """transition() must answer from its own transaction, not a later read.

    It used to commit and then re-read through get_job(), so the return value
    was "whatever the row says now". A worker advancing the job in that gap
    made the 202 body -- and every coordinator decision built on the result --
    describe a state this call never wrote: a stop the user had just been told
    about came back as ``running``.
    """
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-snapshot",
        owner="session-a",
        request_id="request-snapshot",
        profile="artifact.extended.v1",
        payload={},
    )
    repository.request_approval(
        job.job_id, owner="session-a", scope_digest="scope", reason="test", ttl_seconds=10
    )
    repository.decide_approval(job.job_id, owner="session-a", decision="approved")
    calls = _advancing_get_job(repository, monkeypatch)

    cancelling = repository.request_cancel(job.job_id)

    assert cancelling.status == "cancelling"
    # The snapshot carries the same approval join get_job() reports.
    assert cancelling.approval_state == "approved"
    # request_cancel reads once to see whether the job is already terminal;
    # transition() itself must not read again after committing.
    assert calls == [job.job_id]


def test_transition_snapshot_matches_the_committed_event(tmp_path):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-snapshot-event",
        owner="session-a",
        request_id="request-snapshot-event",
        profile="fake.v1",
        payload={},
    )

    running = repository.transition(
        job.job_id,
        status="running",
        event="started",
        phase="run",
        data={"step": 1},
        result={"partial": True},
    )

    events = repository.events(job.job_id)
    assert running.status == "running"
    assert running.sequence == events[-1].sequence
    assert running.result == {"partial": True}
    assert running.approval_state == "not_required"
    assert running == repository.get_job(job.job_id)


def test_terminal_transition_race_reports_the_settled_job_without_a_second_read(
    tmp_path, monkeypatch
):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-snapshot-terminal",
        owner="session-a",
        request_id="request-snapshot-terminal",
        profile="fake.v1",
        payload={},
    )
    finished = repository.transition(
        job.job_id, status="succeeded", event="completed", result={"value": 1}
    )
    calls = _advancing_get_job(repository, monkeypatch)

    late = repository.transition(job.job_id, status="failed", event="failed", error="late")

    assert late == finished
    assert calls == []


def test_fake_coordinator_success_failure_and_replay(tmp_path):
    repository = _repository(tmp_path)
    coordinator = DurableFakeCoordinator(repository)
    try:
        success = coordinator.start(owner="session-a", request_id="success")
        finished = coordinator.wait(success.job_id)
        assert finished.status == "succeeded"
        assert finished.result == {"provider": "fake-v1", "value": 42, "steps": 3}
        events = repository.events(success.job_id)
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert events[-1].event == "completed"

        duplicate = coordinator.start(owner="session-a", request_id="success")
        assert duplicate.job_id == success.job_id

        failure = coordinator.start(
            owner="session-a",
            request_id="failure",
            plan=FakeExecutionPlan(outcome="failure"),
        )
        assert coordinator.wait(failure.job_id).status == "failed"
    finally:
        coordinator.shutdown()


def test_fake_coordinator_cancellation_is_terminal_and_ordered(tmp_path):
    repository = _repository(tmp_path)
    coordinator = DurableFakeCoordinator(repository)
    try:
        job = coordinator.start(
            owner="session-a",
            request_id="cancel",
            plan=FakeExecutionPlan(steps=10, step_delay_seconds=0.03),
        )
        for _ in range(100):
            current = repository.get_job(job.job_id)
            if current is not None and current.status == "running":
                break
            time.sleep(0.005)
        coordinator.cancel(job.job_id, owner="session-a")
        finished = coordinator.wait(job.job_id)
        assert finished.status == "cancelled"
        events = repository.events(job.job_id)
        assert events[-1].status == "cancelled"
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    finally:
        coordinator.shutdown()


def test_job_listings_carry_what_the_per_job_lookups_would_return(tmp_path, frozen_clock):
    """``list_job_listings`` is ``list_jobs`` plus the reads the tray used to repeat.

    For every job, in every state, the listing's newest event and approval must
    equal what ``events`` and ``get_approval`` return for it, in the order
    ``list_jobs`` uses, for the owner asked about and nobody else.
    """

    repository = _repository(tmp_path)

    def make(job_id: str, *, owner: str = "session-a", profile: str = "fake.v1") -> None:
        repository.create_job(
            job_id=job_id, owner=owner, request_id=f"request-{job_id}", profile=profile, payload={}
        )
        frozen_clock.advance(1)

    make("queued")
    make("running")
    repository.transition(
        "running", status="running", event="started", phase="running", data={"message": "Working."}
    )
    frozen_clock.advance(1)
    make("finished")
    repository.transition(
        "finished", status="succeeded", event="completed", phase="completed", data={"message": "Done."}
    )
    frozen_clock.advance(1)
    make("pending", profile="artifact.extended.v1")
    repository.request_approval(
        "pending", owner="session-a", scope_digest="scope", reason="Needs a look.", ttl_seconds=100
    )
    make("decided", profile="artifact.extended.v1")
    repository.request_approval(
        "decided", owner="session-a", scope_digest="scope", reason="Approved one.", ttl_seconds=100
    )
    repository.decide_approval("decided", owner="session-a", decision="approved")
    make("lapsed", profile="artifact.extended.v1")
    repository.request_approval(
        "lapsed", owner="session-a", scope_digest="scope", reason="Left waiting.", ttl_seconds=5
    )
    frozen_clock.advance(10)
    make("someone-elses", owner="session-b")
    # A job whose newest event is gone reads as having none, exactly as
    # events(after_sequence=sequence - 1) does.
    make("orphaned")
    make("behind")
    repository.transition(
        "behind", status="running", event="started", phase="running", data={"message": "Was working."}
    )
    with repository.connect() as connection:
        for job_id in ("orphaned", "behind"):
            connection.execute(
                "DELETE FROM execution_events WHERE job_id = ? AND sequence = "
                "(SELECT MAX(sequence) FROM execution_events WHERE job_id = ?)",
                (job_id, job_id),
            )

    for include_terminal in (False, True):
        expected = repository.list_jobs(owner="session-a", include_terminal=include_terminal)
        listings = repository.list_job_listings(owner="session-a", include_terminal=include_terminal)

        assert [listing.job for listing in listings] == expected
        assert "someone-elses" not in {listing.job.job_id for listing in listings}
        for listing in listings:
            job = listing.job
            events = repository.events(job.job_id, after_sequence=max(0, job.sequence - 1))
            assert listing.latest_event == (events[-1] if events else None), job.job_id
            assert listing.approval == repository.get_approval(job.job_id, owner="session-a"), (
                job.job_id
            )

    by_id = {listing.job.job_id: listing for listing in listings}
    assert by_id["running"].latest_event.phase == "running"
    assert by_id["running"].latest_event.data["message"] == "Working."
    assert by_id["finished"].latest_event.data["message"] == "Done."
    assert by_id["queued"].approval is None
    assert by_id["orphaned"].latest_event is None
    # An older event is still there, but it is behind the job's own sequence.
    assert repository.events("behind") and by_id["behind"].latest_event is None
    assert by_id["pending"].approval.state == "pending"
    assert by_id["pending"].approval.reason == "Needs a look."
    assert by_id["decided"].approval.state == "approved"
    assert by_id["lapsed"].approval.state == "expired"
    assert by_id["lapsed"].job.approval_state == "expired"


@pytest.mark.parametrize("limit", [0, -1, 201])
def test_job_listings_refuse_an_unusable_limit_or_owner(tmp_path, limit):
    repository = _repository(tmp_path)

    with pytest.raises(ValueError, match="limit"):
        repository.list_job_listings(owner="session-a", limit=limit)
    with pytest.raises(ValueError, match="owner"):
        repository.list_job_listings(owner="")


def test_approval_state_is_profile_gated_strict_and_expires_before_cleanup(tmp_path, frozen_clock):
    repository = _repository(tmp_path)
    fake_job, _ = repository.create_job(
        job_id="job-approval-fake",
        owner="session-a",
        request_id="request-approval-fake",
        profile="fake.v1",
        payload={"provider": "fake-v1"},
    )
    assert fake_job.approval_state == "not_required"
    with pytest.raises(ApprovalPolicyError):
        repository.request_approval(
            fake_job.job_id,
            owner="session-a",
            scope_digest="scope",
            reason="test",
        )

    extended, _ = repository.create_job(
        job_id="job-approval-extended",
        owner="session-a",
        request_id="request-approval-extended",
        profile="artifact.extended.v1",
        payload={},
    )
    assert repository.request_approval(
        extended.job_id,
        owner="session-a",
        scope_digest="scope",
        reason="test",
        ttl_seconds=10,
    ) == "pending"
    assert repository.get_job(extended.job_id).approval_state == "pending"
    with pytest.raises(ApprovalTransitionError):
        repository.transition(
            extended.job_id,
            status="succeeded",
            event="completed",
            phase="completed",
            data={"value": 42},
            result={"value": 42},
        )
    assert repository.decide_approval(
        extended.job_id,
        owner="session-a",
        decision="approved",
    ) == "approved"
    with pytest.raises(ApprovalTransitionError):
        repository.decide_approval(
            extended.job_id,
            owner="session-a",
            decision="denied",
        )

    expiring, _ = repository.create_job(
        job_id="job-approval-expiring",
        owner="session-a",
        request_id="request-approval-expiring",
        profile="artifact.extended.v1",
        payload={},
    )
    assert repository.request_approval(
        expiring.job_id,
        owner="session-a",
        scope_digest="scope",
        reason="test",
        ttl_seconds=30,
    ) == "pending"
    assert repository.get_job(expiring.job_id).approval_state == "pending"
    frozen_clock.advance(31)
    assert repository.get_job(expiring.job_id).approval_state == "expired"
    assert repository.get_approval(expiring.job_id).state == "expired"
    with pytest.raises(ApprovalTransitionError, match="expired"):
        repository.decide_approval(
            expiring.job_id,
            owner="session-a",
            decision="approved",
        )
    expired_job = repository.get_job(expiring.job_id)
    assert expired_job.approval_state == "expired"
    assert expired_job.status == "cancelled"
    assert expired_job.error == "approval_expired"
    assert repository.expire_approvals() == []
    with pytest.raises(ApprovalTransitionError):
        repository.decide_approval(
            expiring.job_id,
            owner="session-a",
            decision="denied",
        )

    cleanup, _ = repository.create_job(
        job_id="job-approval-cleanup",
        owner="session-a",
        request_id="request-approval-cleanup",
        profile="artifact.extended.v1",
        payload={},
    )
    repository.request_approval(
        cleanup.job_id,
        owner="session-a",
        scope_digest="scope",
        reason="test cleanup",
        ttl_seconds=30,
    )
    frozen_clock.advance(31)
    assert repository.expire_approvals() == [cleanup.job_id]
    cleaned = repository.get_job(cleanup.job_id)
    assert cleaned.status == "cancelled"
    assert cleaned.approval_state == "expired"
    assert cleaned.error == "approval_expired"
    assert repository.events(cleanup.job_id)[-1].event == "cancelled"


def test_create_job_duplicate_request_reports_real_approval_state(tmp_path, frozen_clock):
    """A retried POST for a job that requires approval must report the real
    approval_state, not silently fall back to "not_required".

    create_job()'s duplicate-request fallback (triggered by the UNIQUE
    (owner, request_id) constraint) used to re-read the existing row with a
    bare `SELECT * FROM execution_jobs`, which has no execution_approvals
    join and therefore no approval_state column -- so _job_from_row()
    defaulted to "not_required" even for a job sitting in "pending" or
    "expired" approval.
    """
    repository = _repository(tmp_path)
    job, created = repository.create_job(
        job_id="job-approval-retry",
        owner="session-a",
        request_id="request-approval-retry",
        profile="artifact.extended.v1",
        payload={},
    )
    assert created is True
    assert (
        repository.request_approval(
            job.job_id,
            owner="session-a",
            scope_digest="scope",
            reason="test",
            ttl_seconds=10,
        )
        == "pending"
    )

    retried, retried_created = repository.create_job(
        job_id="job-approval-retry-duplicate",
        owner="session-a",
        request_id="request-approval-retry",
        profile="artifact.extended.v1",
        payload={},
    )
    assert retried_created is False
    assert retried.job_id == job.job_id
    assert retried.approval_state == "pending"

    # A pending approval that has since expired must also survive the retry
    # path -- not just plain "pending".
    expiring, _ = repository.create_job(
        job_id="job-approval-retry-expiring",
        owner="session-a",
        request_id="request-approval-retry-expiring",
        profile="artifact.extended.v1",
        payload={},
    )
    assert (
        repository.request_approval(
            expiring.job_id,
            owner="session-a",
            scope_digest="scope",
            reason="test",
            ttl_seconds=30,
        )
        == "pending"
    )
    frozen_clock.advance(31)

    retried_expired, retried_expired_created = repository.create_job(
        job_id="job-approval-retry-expiring-duplicate",
        owner="session-a",
        request_id="request-approval-retry-expiring",
        profile="artifact.extended.v1",
        payload={},
    )
    assert retried_expired_created is False
    assert retried_expired.job_id == expiring.job_id
    assert retried_expired.approval_state == "expired"


def test_concurrent_approval_decisions_commit_exactly_once(tmp_path):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-approval-race",
        owner="session-a",
        request_id="request-approval-race",
        profile="artifact.extended.v1",
        payload={},
    )
    repository.request_approval(
        job.job_id,
        owner="session-a",
        scope_digest="immutable-scope",
        reason="Race the decision boundary.",
        ttl_seconds=10,
    )

    def decide(decision):
        try:
            return repository.decide_approval(
                job.job_id,
                owner="session-a",
                decision=decision,
            )
        except ApprovalTransitionError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(decide, ("approved", "denied")))

    assert outcomes.count("rejected") == 1
    committed = repository.get_approval(job.job_id).state
    assert committed in {"approved", "denied"}
    assert outcomes.count(committed) == 1
    events = repository.events(job.job_id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert [event.data.get("approval_state") for event in events].count(committed) == 1
    final = repository.get_job(job.job_id)
    if committed == "denied":
        assert final.status == "cancelled"
        assert final.error == "approval_denied"
    else:
        assert final.status == "queued"


def test_recovery_supervisor_reclaims_stale_fake_job_once_and_blocks_live_peer(tmp_path, frozen_clock):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-restart",
        owner="session-a",
        request_id="request-restart",
        profile="fake.v1",
        payload={
            "provider": "fake-v1",
            "outcome": "success",
            "steps": 2,
            "step_delay_seconds": 0.01,
            "failure_message": "Deterministic fake execution failed.",
        },
    )
    repository.claim_lease(job.job_id, lease_owner="dead-worker", ttl_seconds=30)
    frozen_clock.advance(31)

    coordinator = DurableFakeCoordinator(repository)
    try:
        with pytest.raises(LeaseConflict):
            DurableFakeCoordinator(repository)
        finished = coordinator.wait(job.job_id)
        assert finished.status == "succeeded"
        events = repository.events(job.job_id)
        assert [event.event for event in events].count("recovered") == 1
        assert events[-1].event == "completed"
        assert coordinator.startup_recover() == []
    finally:
        coordinator.shutdown()


def test_recovery_supervisor_fails_closed_on_malformed_payload(tmp_path, frozen_clock):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-restart-invalid",
        owner="session-a",
        request_id="request-restart-invalid",
        profile="fake.v1",
        payload={"provider": "host-process"},
    )
    repository.claim_lease(job.job_id, lease_owner="dead-worker", ttl_seconds=30)
    frozen_clock.advance(31)

    coordinator = DurableFakeCoordinator(repository)
    try:
        failed = repository.get_job(job.job_id)
        assert failed.status == "failed"
        assert failed.error == "recovery_invalid_payload"
        assert repository.events(job.job_id)[-1].event == "failed"
    finally:
        coordinator.shutdown()


def test_supervisor_lease_expiry_is_reclaimable(tmp_path, frozen_clock):
    repository = _repository(tmp_path)
    repository.claim_supervisor_lease(lease_owner="dead-supervisor", ttl_seconds=30)
    with pytest.raises(LeaseConflict):
        repository.claim_supervisor_lease(lease_owner="new-supervisor", ttl_seconds=10)
    frozen_clock.advance(31)
    repository.claim_supervisor_lease(lease_owner="new-supervisor", ttl_seconds=10)
    repository.release_supervisor_lease(lease_owner="new-supervisor")


def test_a_store_failure_is_not_reported_as_a_duplicate_request(tmp_path, monkeypatch):
    """Only a constraint failure may mean "this request already has a job".

    ``connect()`` turns every ``sqlite3.Error`` into one repository error, and
    ``create_job`` answered any of them by looking the request id up. With a job
    already stored for that id, a database that merely could not write (locked,
    disk error) was therefore reported as ``created=False`` -- success, with an
    existing job -- so the caller was told its request was accepted when the
    write had failed.
    """

    repository = _repository(tmp_path)
    first, _ = repository.create_job(
        job_id="job-1",
        owner="session-a",
        request_id="request-1",
        profile="fake.v1",
        payload={},
    )
    real_connect = sqlite3.connect

    class RefusesJobInserts(sqlite3.Connection):
        def execute(self, sql, *args):
            if "INSERT INTO execution_jobs" in sql:
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *args)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: real_connect(*args, factory=RefusesJobInserts, **kwargs),
    )
    with pytest.raises(ExecutionRepositoryError) as failure:
        repository.create_job(
            job_id="job-2",
            owner="session-a",
            request_id="request-1",
            profile="fake.v1",
            payload={},
        )
    assert not isinstance(failure.value, ExecutionIntegrityError)
    monkeypatch.undo()

    # A genuine duplicate is still answered with the job that already exists.
    duplicate, created = repository.create_job(
        job_id="job-3",
        owner="session-a",
        request_id="request-1",
        profile="fake.v1",
        payload={},
    )
    assert created is False
    assert duplicate.job_id == first.job_id


def test_a_job_id_collision_is_an_integrity_error_not_a_duplicate_request(tmp_path):
    repository = _repository(tmp_path)
    repository.create_job(
        job_id="job-1",
        owner="session-a",
        request_id="request-1",
        profile="fake.v1",
        payload={},
    )

    # Same job id, different request: nothing is stored for request-2, so there
    # is no existing job to hand back and the constraint failure must surface.
    with pytest.raises(ExecutionIntegrityError):
        repository.create_job(
            job_id="job-1",
            owner="session-a",
            request_id="request-2",
            profile="fake.v1",
            payload={},
        )


def test_a_job_record_declares_no_lease_fields_it_never_fills(tmp_path):
    """``ExecutionJob`` declared ``lease_owner`` and ``lease_expires_at`` and nothing filled them in.

    They read as "this job holds no lease" when they meant "this was never
    populated", which is how a snapshot was once misread as evidence about
    lease state. The lease table is read through ``lease_holder``.
    """

    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-lease-fields",
        owner="session-a",
        request_id="request-lease-fields",
        profile="fake.v1",
        payload={},
    )
    repository.claim_lease(job.job_id, lease_owner="coordinator-a", ttl_seconds=30)

    held = repository.get_job(job.job_id)

    assert held is not None
    assert repository.lease_holder(job.job_id) == "coordinator-a"
    names = {field.name for field in dataclasses.fields(ExecutionJob)}
    assert names.isdisjoint({"lease_owner", "lease_expires_at"})
    assert not hasattr(held, "lease_owner")


def _job(repository, job_id, *, profile="fake.v1"):
    job, _ = repository.create_job(
        job_id=job_id,
        owner="session-a",
        request_id=f"request-{job_id}",
        profile=profile,
        payload={},
    )
    return job


def test_retire_abandoned_job_fails_only_a_job_nothing_is_working_on(tmp_path, frozen_clock):
    repository = _repository(tmp_path)
    idle = _job(repository, "job-idle")
    leased = _job(repository, "job-leased")
    awaiting = _job(repository, "job-awaiting", profile="code.exec.v1")
    done = _job(repository, "job-done")
    repository.transition(
        done.job_id, status="succeeded", event="completed", phase="completed", data={}, result={"v": 1}
    )
    # Asked for while the clock is still at the start, so by the time it has
    # moved on the job has been quiet for longer than the idle limit and only
    # the pending approval keeps it from being retired.
    repository.request_approval(awaiting.job_id, owner="session-a", scope_digest="digest", reason="run")
    frozen_clock.advance(200)
    young = _job(repository, "job-young")
    repository.claim_lease(leased.job_id, lease_owner="worker", ttl_seconds=30)

    def retire(job_id):
        return repository.retire_abandoned_job(
            job_id, idle_seconds=120, error="interrupted", message="Interrupted."
        )

    retired = retire(idle.job_id)
    assert retired is not None and (retired.status, retired.error) == ("failed", "interrupted")
    assert repository.events(idle.job_id)[-1].event == "failed"
    assert [job.status for job in map(retire, (leased.job_id, awaiting.job_id, young.job_id))] == [
        "queued",
        "queued",
        "queued",
    ]
    assert retire(done.job_id).status == "succeeded"
    assert retire("job-that-does-not-exist") is None

    # Idempotent: a second call neither rewrites nor appends.
    before = repository.events(idle.job_id)
    assert retire(idle.job_id).status == "failed"
    assert repository.events(idle.job_id) == before


def test_retire_abandoned_job_takes_an_expired_lease_and_clears_it(tmp_path, frozen_clock):
    repository = _repository(tmp_path)
    job = _job(repository, "job-dead-worker")
    repository.claim_lease(job.job_id, lease_owner="dead-worker", ttl_seconds=30)

    frozen_clock.advance(60)  # the lease is expired, the job has been quiet for a minute
    still_recent = repository.retire_abandoned_job(
        job.job_id, idle_seconds=120, error="interrupted", message="Interrupted."
    )
    assert still_recent.status == "queued"

    frozen_clock.advance(100)
    retired = repository.retire_abandoned_job(
        job.job_id, idle_seconds=120, error="interrupted", message="Interrupted."
    )
    assert retired.status == "failed"
    assert repository.lease_holder(job.job_id) is None


def test_retire_abandoned_job_honours_a_stop_and_rejects_a_bad_idle_time(tmp_path, frozen_clock):
    repository = _repository(tmp_path)
    job = _job(repository, "job-cancelling")
    repository.request_cancel(job.job_id)
    frozen_clock.advance(500)

    retired = repository.retire_abandoned_job(
        job.job_id, idle_seconds=120, error="interrupted", message="Interrupted."
    )

    # The user already pressed Stop: an abandoned job ends as they asked.
    assert (retired.status, retired.error) == ("cancelled", "cancelled")
    assert repository.events(job.job_id)[-1].event == "cancelled"
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            repository.retire_abandoned_job(
                job.job_id, idle_seconds=bad, error="interrupted", message="Interrupted."
            )


# -- job ids become directory names; artifact access is validated and can be owner-scoped ----


@pytest.mark.parametrize(
    "job_id",
    ["../escape", "..\\escape", "a/b", "a\\b", "", ".hidden", "x" * 201, "nul\x00byte", "C:evil"],
)
def test_create_job_refuses_a_job_id_that_could_leave_the_artifact_root(tmp_path, job_id):
    repository = _repository(tmp_path)

    with pytest.raises(ValueError, match="job_id"):
        repository.create_job(
            job_id=job_id,
            owner="session-a",
            request_id="request-1",
            profile="fake.v1",
            payload={},
        )

    assert repository.list_jobs(owner="session-a", include_terminal=True) == []


@pytest.mark.parametrize("owner", ["", "x" * 201, "line\nbreak", "nul\x00byte", None, 7])
def test_create_job_refuses_an_owner_that_is_not_a_bounded_printable_string(tmp_path, owner):
    repository = _repository(tmp_path)

    with pytest.raises(ValueError, match="owner"):
        repository.create_job(
            job_id="job-1",
            owner=owner,
            request_id="request-1",
            profile="fake.v1",
            payload={},
        )


def test_publish_confines_the_job_directory_before_it_creates_anything(tmp_path):
    """The confinement check used to run after the mkdir.

    A job row that predates the id check and names a path outside the root
    made its directories there and only then failed.
    """

    repository = _repository(tmp_path)
    now = "2030-01-01T00:00:00+00:00"
    with repository.connect() as connection:
        connection.execute(
            """
            INSERT INTO execution_jobs
            (job_id, owner, request_id, profile, status, sequence, payload_json, created_at, updated_at)
            VALUES (?, 'session-a', 'request-old', 'fake.v1', 'queued', 0, '{}', ?, ?)
            """,
            ("../escaped-directory", now, now),
        )

    with pytest.raises(ExecutionRepositoryError):
        repository.publish_artifact("../escaped-directory", name="out.txt", content=b"x")

    assert not (tmp_path / "escaped-directory").exists()
    assert not (tmp_path / "artifacts" / ".." / "escaped-directory").exists()


def test_an_expired_artifact_is_absent_from_get_as_well_as_from_read(tmp_path, frozen_clock):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-expiry", owner="session-a", request_id="r", profile="fake.v1", payload={}
    )
    artifact = repository.publish_artifact(
        job.job_id, name="out.txt", content=b"synthetic", mime_type="text/plain", retention_seconds=60
    )
    assert repository.get_artifact(artifact.artifact_id, owner="session-a") is not None

    frozen_clock.advance(61)

    assert repository.get_artifact(artifact.artifact_id, owner="session-a") is None
    with pytest.raises(ExecutionRepositoryError, match="expired"):
        repository.read_artifact(artifact.artifact_id)


def test_read_and_delete_can_be_scoped_to_an_owner_and_validate_the_artifact_id(tmp_path):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-scope", owner="session-a", request_id="r", profile="fake.v1", payload={}
    )
    artifact = repository.publish_artifact(
        job.job_id, name="out.txt", content=b"synthetic", mime_type="text/plain"
    )

    # Another owner sees nothing, and cannot delete it.
    with pytest.raises(ExecutionRepositoryError, match="does not exist"):
        repository.read_artifact(artifact.artifact_id, owner="session-b")
    repository.delete_artifact(artifact.artifact_id, owner="session-b")
    assert repository.read_artifact(artifact.artifact_id, owner="session-a") == b"synthetic"
    assert repository.read_artifact(artifact.artifact_id) == b"synthetic"

    # A malformed id is refused up front, exactly as get_artifact already did.
    for bad in ("../x", "", "a/b", "x" * 201, None):
        assert repository.get_artifact(bad) is None
        with pytest.raises(ExecutionRepositoryError, match="does not exist"):
            repository.read_artifact(bad)
        repository.delete_artifact(bad)

    repository.delete_artifact(artifact.artifact_id, owner="session-a")
    assert repository.get_artifact(artifact.artifact_id) is None
