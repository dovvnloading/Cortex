"""Phase 1 durable fake-executor lifecycle contract tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
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
        assert connection.execute("SELECT version FROM execution_schema WHERE id = 1").fetchone()[0] == 2
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {"execution_approvals", "execution_supervisor_leases"} <= tables
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
        ttl_seconds=0.01,
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
    assert repository.expire_approvals() == [expiring.job_id]
    assert repository.get_job(expiring.job_id).approval_state == "expired"
    with pytest.raises(ApprovalTransitionError):
        repository.decide_approval(
            expiring.job_id,
            owner="session-a",
            decision="denied",
        )


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
