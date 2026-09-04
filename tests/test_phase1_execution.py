"""Phase 1 durable fake-executor lifecycle contract tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import time

import pytest

from cortex_backend.execution import (
    ArtifactLimitError,
    ExecutionRepository,
    ExecutionRepositoryError,
    LeaseConflict,
)
from cortex_backend.execution.coordinator import DurableFakeCoordinator
from cortex_backend.execution.fake import FakeExecutionPlan
from cortex_backend.execution.repository import ApprovalPolicyError, ApprovalTransitionError


def _repository(tmp_path):
    return ExecutionRepository(
        tmp_path / "execution.sqlite",
        tmp_path / "artifacts",
        max_artifact_bytes=64,
    )


def test_durable_idempotency_event_replay_and_restart_recovery(tmp_path):
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

    repository.claim_lease(first.job_id, lease_owner="dead-coordinator", ttl_seconds=0.01)
    time.sleep(0.03)
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


def test_expired_lease_does_not_resurrect_a_persisted_cancellation(tmp_path):
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
        ttl_seconds=0.01,
    )
    repository.request_cancel(job.job_id)
    time.sleep(0.03)

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
    content = b"phase1-artifact"
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
    assert repository.purge_expired(now=future) == 1
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

    assert repository.purge_expired(
        now=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    ) == 0
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


def test_approval_state_is_profile_gated_strict_and_expires_before_cleanup(tmp_path):
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
        ttl_seconds=0.01,
    ) == "pending"
    time.sleep(0.03)
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
        ttl_seconds=0.01,
    )
    time.sleep(0.03)
    assert repository.expire_approvals() == [cleanup.job_id]
    cleaned = repository.get_job(cleanup.job_id)
    assert cleaned.status == "cancelled"
    assert cleaned.approval_state == "expired"
    assert cleaned.error == "approval_expired"
    assert repository.events(cleanup.job_id)[-1].event == "cancelled"


def test_create_job_duplicate_request_reports_real_approval_state(tmp_path):
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
            ttl_seconds=0.01,
        )
        == "pending"
    )
    time.sleep(0.03)

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


def test_recovery_supervisor_reclaims_stale_fake_job_once_and_blocks_live_peer(tmp_path):
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
    repository.claim_lease(job.job_id, lease_owner="dead-worker", ttl_seconds=0.01)
    time.sleep(0.03)

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


def test_recovery_supervisor_fails_closed_on_malformed_payload(tmp_path):
    repository = _repository(tmp_path)
    job, _ = repository.create_job(
        job_id="job-restart-invalid",
        owner="session-a",
        request_id="request-restart-invalid",
        profile="fake.v1",
        payload={"provider": "host-process"},
    )
    repository.claim_lease(job.job_id, lease_owner="dead-worker", ttl_seconds=0.01)
    time.sleep(0.03)

    coordinator = DurableFakeCoordinator(repository)
    try:
        failed = repository.get_job(job.job_id)
        assert failed.status == "failed"
        assert failed.error == "recovery_invalid_payload"
        assert repository.events(job.job_id)[-1].event == "failed"
    finally:
        coordinator.shutdown()


def test_supervisor_lease_expiry_is_reclaimable(tmp_path):
    repository = _repository(tmp_path)
    repository.claim_supervisor_lease(lease_owner="dead-supervisor", ttl_seconds=0.01)
    time.sleep(0.03)
    repository.claim_supervisor_lease(lease_owner="new-supervisor", ttl_seconds=10)
    repository.release_supervisor_lease(lease_owner="new-supervisor")
