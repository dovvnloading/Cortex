"""'Allow once' is one launch, in the process the user answered in.

The README promises that permissions are not carried into the next run. An
approval used to be a row that stayed ``approved`` forever: startup recovery
relaunched every non-terminal code job, the worker ran it as soon as it saw the
row, and an approval that sat unclaimed for a week -- or a program that crashed
mid-run -- executed again on the next launch with no prompt.

The approval is now spent by the same transaction that takes the lease, and is
refused if it was spent before, decided before this process started, or decided
longer ago than the grant window.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

import pytest

from cortex_backend.execution.code_execution import (
    CODE_EXECUTION_PROFILE,
    CodeExecutionError,
    CodeExecutionRequest,
)
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.models import ExecutionJob
from cortex_backend.execution.repository import (
    APPROVAL_GRANT_SECONDS,
    ApprovalExpiredError,
    ApprovalTransitionError,
    ExecutionRepository,
    ExecutionRepositoryError,
    ExecutionTransitionConflict,
    LeaseConflict,
)
from support import wait_until


def _repository(tmp_path: Path) -> ExecutionRepository:
    return ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")


def _pending_job(
    repository: ExecutionRepository, *, job_id: str = "job-1", source: str = "_result = 40 + 2"
) -> tuple[ExecutionJob, str]:
    """A code job with a pending approval, built without any coordinator thread."""
    owner = repository.installation_principal_id
    request = CodeExecutionRequest(
        owner=owner,
        request_id=f"request-{job_id}",
        source=source,
        intent_summary="synthetic check",
    )
    job, _ = repository.create_job(
        job_id=job_id,
        owner=owner,
        request_id=request.request_id,
        profile=CODE_EXECUTION_PROFILE,
        payload=request.payload(),
    )
    repository.request_approval(
        job.job_id,
        owner=owner,
        scope_digest=request.approval_scope_digest,
        reason=request.intent_summary,
    )
    return job, owner


def _approved_job(repository: ExecutionRepository, *, job_id: str = "job-1") -> tuple[ExecutionJob, str]:
    job, owner = _pending_job(repository, job_id=job_id)
    repository.decide_approval(job.job_id, owner=owner, decision="approved")
    return job, owner


def _uses(repository: ExecutionRepository, job_id: str) -> int:
    with repository.connect() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM execution_approval_uses WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
        )


def _lease_rows(repository: ExecutionRepository, job_id: str) -> int:
    with repository.connect() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM execution_leases WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
        )


def _set_decided_at(repository: ExecutionRepository, job_id: str, value: str | None) -> None:
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_approvals SET decided_at = ? WHERE job_id = ?", (value, job_id)
        )


def _assert_lapsed(repository: ExecutionRepository, job_id: str) -> None:
    """The refusal is durable: cancelled, approval expired, and nothing spent or leased."""
    job = repository.get_job(job_id)
    assert job is not None
    assert job.status == "cancelled"
    assert job.error == "approval_expired"
    assert job.approval_state == "expired"
    assert job.result is None
    last = repository.events(job_id)[-1]
    assert (last.event, last.status) == ("code.cancelled", "cancelled")
    assert last.data["approval_state"] == "expired"
    assert _uses(repository, job_id) == 0
    assert _lease_rows(repository, job_id) == 0


# --- the repository primitive ------------------------------------------------


def test_an_approval_is_spent_by_the_launch_that_claims_the_lease(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job, _ = _approved_job(repository)

    repository.claim_approved_lease(job.job_id, lease_owner="worker-1", ttl_seconds=30)
    assert _uses(repository, job.job_id) == 1
    assert _lease_rows(repository, job.job_id) == 1

    # A relaunch -- after a crash, say -- finds the approval already spent.
    repository.release_lease(job.job_id, lease_owner="worker-1")
    with pytest.raises(ApprovalExpiredError):
        repository.claim_approved_lease(job.job_id, lease_owner="worker-2", ttl_seconds=30)

    refused = repository.get_job(job.job_id)
    assert refused is not None
    assert (refused.status, refused.error, refused.approval_state) == (
        "cancelled",
        "approval_expired",
        "expired",
    )
    assert repository.events(job.job_id)[-1].event == "code.cancelled"
    assert _lease_rows(repository, job.job_id) == 0


def test_a_grant_from_a_previous_process_is_refused(tmp_path: Path) -> None:
    first = _repository(tmp_path)
    job, _ = _approved_job(first)

    restarted = _repository(tmp_path)
    with pytest.raises(ApprovalExpiredError):
        restarted.claim_approved_lease(job.job_id, lease_owner="worker", ttl_seconds=30)

    _assert_lapsed(restarted, job.job_id)


def test_a_grant_older_than_the_window_is_refused_and_one_inside_it_is_not(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    # Pretend this process has been up for a day, so only the window is in play.
    repository._opened_at = datetime.now(timezone.utc) - timedelta(days=1)
    inside, _ = _approved_job(repository, job_id="inside")
    outside, _ = _approved_job(repository, job_id="outside")
    _set_decided_at(
        repository,
        "inside",
        (datetime.now(timezone.utc) - timedelta(seconds=APPROVAL_GRANT_SECONDS - 30)).isoformat(),
    )
    _set_decided_at(
        repository,
        "outside",
        (datetime.now(timezone.utc) - timedelta(seconds=APPROVAL_GRANT_SECONDS + 30)).isoformat(),
    )

    repository.claim_approved_lease(inside.job_id, lease_owner="worker", ttl_seconds=30)
    with pytest.raises(ApprovalExpiredError):
        repository.claim_approved_lease(outside.job_id, lease_owner="worker", ttl_seconds=30)

    assert _uses(repository, "inside") == 1
    _assert_lapsed(repository, "outside")


@pytest.mark.parametrize(
    "decided_at",
    [None, "not-a-timestamp", "2026-01-01T00:00:00"],
    ids=["missing", "unparseable", "naive"],
)
def test_an_approval_without_a_trustworthy_decision_time_fails_closed(
    tmp_path: Path, decided_at: str | None
) -> None:
    repository = _repository(tmp_path)
    job, _ = _approved_job(repository)
    _set_decided_at(repository, job.job_id, decided_at)

    with pytest.raises(ApprovalExpiredError):
        repository.claim_approved_lease(job.job_id, lease_owner="worker", ttl_seconds=30)

    _assert_lapsed(repository, job.job_id)


def test_claiming_without_an_approved_decision_spends_nothing(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    pending, _ = _pending_job(repository, job_id="pending")
    with pytest.raises(ApprovalTransitionError) as refused:
        repository.claim_approved_lease("pending", lease_owner="worker", ttl_seconds=30)
    assert not isinstance(refused.value, ApprovalExpiredError)
    assert repository.get_job(pending.job_id).approval_state == "pending"  # type: ignore[union-attr]
    assert _uses(repository, "pending") == 0
    assert _lease_rows(repository, "pending") == 0

    owner = repository.installation_principal_id
    repository.create_job(
        job_id="no-approval",
        owner=owner,
        request_id="request-no-approval",
        profile=CODE_EXECUTION_PROFILE,
        payload={},
    )
    with pytest.raises(ApprovalTransitionError):
        repository.claim_approved_lease("no-approval", lease_owner="worker", ttl_seconds=30)
    assert _uses(repository, "no-approval") == 0


def test_a_denied_job_cannot_claim_a_lease(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job, owner = _pending_job(repository)
    repository.decide_approval(job.job_id, owner=owner, decision="denied")

    with pytest.raises(ExecutionRepositoryError, match="Terminal"):
        repository.claim_approved_lease(job.job_id, lease_owner="worker", ttl_seconds=30)

    assert _uses(repository, job.job_id) == 0
    assert repository.get_job(job.job_id).approval_state == "denied"  # type: ignore[union-attr]


def test_a_missing_job_cannot_claim_a_lease(tmp_path: Path) -> None:
    with pytest.raises(ExecutionRepositoryError, match="does not exist"):
        _repository(tmp_path).claim_approved_lease("missing", lease_owner="worker", ttl_seconds=30)


def test_a_cancellation_already_committed_stops_the_launch_and_keeps_the_approval(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    job, _ = _approved_job(repository)
    repository.request_cancel(job.job_id)

    with pytest.raises(ExecutionTransitionConflict):
        repository.claim_approved_lease(job.job_id, lease_owner="worker", ttl_seconds=30)

    current = repository.get_job(job.job_id)
    assert current is not None
    assert (current.status, current.approval_state) == ("cancelling", "approved")
    assert _uses(repository, job.job_id) == 0
    assert _lease_rows(repository, job.job_id) == 0


def test_a_live_foreign_lease_blocks_the_claim_without_spending_the_approval(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job, _ = _approved_job(repository)
    repository.claim_lease(job.job_id, lease_owner="other-coordinator", ttl_seconds=30)

    with pytest.raises(LeaseConflict):
        repository.claim_approved_lease(job.job_id, lease_owner="worker", ttl_seconds=30)
    assert _uses(repository, job.job_id) == 0
    assert repository.get_job(job.job_id).approval_state == "approved"  # type: ignore[union-attr]

    repository.release_lease(job.job_id, lease_owner="other-coordinator")
    repository.claim_approved_lease(job.job_id, lease_owner="worker", ttl_seconds=30)
    assert _uses(repository, job.job_id) == 1


def test_an_expired_foreign_lease_does_not_block_the_claim(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job, _ = _approved_job(repository)
    repository.claim_lease(job.job_id, lease_owner="crashed-coordinator", ttl_seconds=0.01)
    time.sleep(0.03)

    repository.claim_approved_lease(job.job_id, lease_owner="worker", ttl_seconds=30)

    assert _uses(repository, job.job_id) == 1


def test_concurrent_claims_spend_the_approval_exactly_once(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job, _ = _approved_job(repository)

    def claim(owner: str) -> str:
        try:
            repository.claim_approved_lease(job.job_id, lease_owner=owner, ttl_seconds=30)
            return "claimed"
        except (LeaseConflict, ApprovalExpiredError) as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(claim, [f"worker-{index}" for index in range(4)]))

    assert outcomes.count("claimed") == 1
    assert _uses(repository, job.job_id) == 1
    assert _lease_rows(repository, job.job_id) == 1
    assert repository.get_job(job.job_id).status == "queued"  # type: ignore[union-attr]


def test_spending_the_approval_and_taking_the_lease_commit_together(tmp_path: Path) -> None:
    """If the lease cannot be written, the approval must not be spent."""
    repository = _repository(tmp_path)
    job, _ = _approved_job(repository)
    with repository.connect() as connection:
        connection.execute(
            "CREATE TRIGGER refuse_lease BEFORE INSERT ON execution_leases "
            "BEGIN SELECT RAISE(ABORT, 'synthetic lease failure'); END"
        )

    with pytest.raises(ExecutionRepositoryError):
        repository.claim_approved_lease(job.job_id, lease_owner="worker", ttl_seconds=30)
    assert _uses(repository, job.job_id) == 0
    assert repository.get_job(job.job_id).approval_state == "approved"  # type: ignore[union-attr]

    with repository.connect() as connection:
        connection.execute("DROP TRIGGER refuse_lease")
    repository.claim_approved_lease(job.job_id, lease_owner="worker", ttl_seconds=30)
    assert _uses(repository, job.job_id) == 1


def test_claiming_rejects_a_non_positive_ttl(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job, _ = _approved_job(repository)
    with pytest.raises(ValueError):
        repository.claim_approved_lease(job.job_id, lease_owner="worker", ttl_seconds=0)
    assert _uses(repository, job.job_id) == 0


def test_the_use_record_is_additive_and_is_removed_with_its_job(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    # A store created before the table existed gains it without a version bump.
    with repository.connect() as connection:
        connection.execute("DROP TABLE execution_approval_uses")
    reopened = _repository(tmp_path)
    with reopened.connect() as connection:
        assert connection.execute(
            "SELECT version FROM execution_schema WHERE id = 1"
        ).fetchone()[0] == 3

    job, _ = _approved_job(reopened)
    reopened.claim_approved_lease(job.job_id, lease_owner="worker", ttl_seconds=30)
    reopened.release_lease(job.job_id, lease_owner="worker")
    reopened.transition(job.job_id, status="succeeded", event="code.completed")

    result = reopened.cleanup_expired(
        now="9999-01-01T00:00:00+00:00", terminal_job_retention_seconds=0
    )

    assert result.jobs == 1
    assert _uses(reopened, job.job_id) == 0


# --- through the coordinator, as the app runs it -----------------------------


def _restart(tmp_path: Path) -> tuple[ExecutionRepository, LocalExecutionCoordinator]:
    repository = _repository(tmp_path)
    return repository, LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)


def test_an_approval_granted_before_a_restart_does_not_run_after_it(tmp_path: Path) -> None:
    """The reported defect, end to end.

    The user clicked Allow, and the process died before the worker claimed the
    job. On the next launch startup recovery relaunched it and it ran to
    completion with no prompt.
    """
    before = _repository(tmp_path)
    job, _ = _approved_job(before, job_id="approved-before-restart")

    repository, coordinator = _restart(tmp_path)
    try:
        coordinator.startup_recover()
        final = coordinator.wait(job.job_id, timeout=10.0)
    finally:
        coordinator.shutdown()

    assert final.status == "cancelled"
    assert final.error == "approval_expired"
    assert final.result is None
    events = [event.event for event in repository.events(job.job_id)]
    assert "code.started" not in events
    assert "code.completed" not in events
    assert repository.get_approval(job.job_id).state == "expired"  # type: ignore[union-attr]
    assert not (repository.artifact_root / ".code_workspaces" / job.job_id).exists()


def test_a_crash_mid_run_does_not_run_the_program_again(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job, _ = _approved_job(repository, job_id="crashed-mid-run")
    repository.claim_approved_lease(job.job_id, lease_owner="dead-worker", ttl_seconds=0.01)
    repository.transition(
        job.job_id, status="running", event="code.started", phase="prepare", data={}
    )
    time.sleep(0.05)

    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    try:
        assert coordinator.startup_recover() == [job.job_id]
        final = coordinator.wait(job.job_id, timeout=10.0)
    finally:
        coordinator.shutdown()

    assert final.status == "cancelled"
    assert final.error == "approval_expired"
    events = [event.event for event in repository.events(job.job_id)]
    assert events.count("code.started") == 1
    assert "code.completed" not in events


def test_a_fresh_approval_after_a_restart_still_runs(tmp_path: Path) -> None:
    """Refusing stale consent must not break consent given now."""
    before = _repository(tmp_path)
    job, owner = _pending_job(before, job_id="pending-across-restart")

    repository, coordinator = _restart(tmp_path)
    try:
        coordinator.startup_recover()
        repository.decide_approval(job.job_id, owner=owner, decision="approved")
        final = coordinator.wait(job.job_id, timeout=10.0)
    finally:
        coordinator.shutdown()

    assert final.status == "succeeded"
    assert final.result is not None and final.result["value"] == 42
    assert _uses(repository, job.job_id) == 1


def test_a_cancellation_landing_during_the_claim_wins_and_spends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    owner = repository.installation_principal_id
    original_claim = repository.claim_approved_lease
    cancelled_during_claim: list[bool] = []

    def cancel_then_claim(job_id: str, **kwargs):  # type: ignore[no-untyped-def]
        repository.request_cancel(job_id)
        cancelled_during_claim.append(True)
        return original_claim(job_id, **kwargs)

    monkeypatch.setattr(repository, "claim_approved_lease", cancel_then_claim)
    try:
        job = coordinator.start_code(
            CodeExecutionRequest(
                owner=owner,
                request_id="cancel-during-claim",
                source="_result = 1",
                intent_summary="synthetic check",
            )
        )
        repository.decide_approval(job.job_id, owner=owner, decision="approved")
        final = coordinator.wait(job.job_id, timeout=10.0)
    finally:
        coordinator.shutdown()

    assert cancelled_during_claim == [True]
    assert final.status == "cancelled"
    assert final.error == "cancelled"
    assert _uses(repository, job.job_id) == 0
    assert "code.started" not in [event.event for event in repository.events(job.job_id)]


# --- a crashed run's workspace ------------------------------------------------
#
# A hard crash mid-run skips the run's own cleanup. The relaunch after restart is
# refused at the approval gate, which is before the workspace is reset or cleaned,
# so the crashed run's directory used to stay under .code_workspaces for good.


def _leave_a_crashed_workspace(repository: ExecutionRepository, job_id: str) -> Path:
    workspace = repository.artifact_root / ".code_workspaces" / job_id
    (workspace / "nested").mkdir(parents=True)
    (workspace / "half-written.txt").write_text("left by the crashed run", encoding="utf-8")
    (workspace / "nested" / "more.txt").write_text("also left", encoding="utf-8")
    return workspace


def _crash_mid_run(repository: ExecutionRepository, job_id: str) -> Path:
    """Leave a job as a hard crash does: approval spent, lease dead, workspace on disk."""

    job, _ = _approved_job(repository, job_id=job_id)
    lease_until = datetime.fromisoformat(
        repository.claim_approved_lease(job.job_id, lease_owner="dead-worker", ttl_seconds=0.01)
    )
    repository.transition(
        job.job_id, status="running", event="code.started", phase="prepare", data={}
    )
    workspace = _leave_a_crashed_workspace(repository, job.job_id)
    wait_until(
        lambda: datetime.now(timezone.utc) > lease_until,
        timeout=5.0,
        describe="the crashed run's lease to lapse",
    )
    return workspace


def test_a_refused_relaunch_removes_the_workspace_a_crashed_run_left(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    workspace = _crash_mid_run(repository, "crashed-workspace")

    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    try:
        assert coordinator.startup_recover() == ["crashed-workspace"]
        final = coordinator.wait("crashed-workspace", timeout=10.0)
        wait_until(
            lambda: not workspace.exists(),
            timeout=5.0,
            describe="the crashed run's workspace to be removed",
        )
    finally:
        coordinator.shutdown()

    assert (final.status, final.error) == ("cancelled", "approval_expired")
    assert not workspace.exists()
    # Only that job's directory goes; the shared root stays for the next run.
    assert workspace.parent.is_dir()


def test_an_expired_approval_removes_a_stale_workspace(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job, _ = _pending_job(repository, job_id="expired-workspace")
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_approvals SET expires_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00+00:00", job.job_id),
        )
    workspace = _leave_a_crashed_workspace(repository, job.job_id)

    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    try:
        coordinator._launch_code(job.job_id)
        final = coordinator.wait(job.job_id, timeout=10.0)
        wait_until(
            lambda: not workspace.exists(),
            timeout=5.0,
            describe="the stale workspace to be removed",
        )
    finally:
        coordinator.shutdown()

    assert (final.status, final.approval_state) == ("cancelled", "expired")


def test_an_approval_that_lapses_while_the_job_waits_removes_a_stale_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    job, _ = _pending_job(repository, job_id="lapses-while-waiting")
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_approvals SET expires_at = ? WHERE job_id = ?",
            ((datetime.now(timezone.utc) + timedelta(seconds=1.0)).isoformat(), job.job_id),
        )
    workspace = _leave_a_crashed_workspace(repository, job.job_id)
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    waits: list[str] = []
    original_await = coordinator._await_approval

    def counted_await(job_id: str, cancel_event):  # type: ignore[no-untyped-def]
        waits.append(job_id)
        return original_await(job_id, cancel_event)

    monkeypatch.setattr(coordinator, "_await_approval", counted_await)
    try:
        coordinator._launch_code(job.job_id)
        final = coordinator.wait(job.job_id, timeout=10.0)
        wait_until(
            lambda: not workspace.exists(),
            timeout=5.0,
            describe="the stale workspace to be removed",
        )
    finally:
        coordinator.shutdown()

    assert waits == [job.job_id], "the approval should have lapsed while the job was waiting"
    assert (final.status, final.approval_state) == ("cancelled", "expired")


def test_an_approval_changed_under_the_claim_removes_a_stale_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    job, _ = _approved_job(repository, job_id="changed-under-claim")
    workspace = _leave_a_crashed_workspace(repository, job.job_id)

    def refuse(job_id: str, **kwargs: object) -> str:
        raise ApprovalTransitionError("Execution is not approved.")

    monkeypatch.setattr(repository, "claim_approved_lease", refuse)
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    try:
        coordinator._launch_code(job.job_id)
        final = coordinator.wait(job.job_id, timeout=10.0)
        wait_until(
            lambda: not workspace.exists(),
            timeout=5.0,
            describe="the stale workspace to be removed",
        )
    finally:
        coordinator.shutdown()

    assert (final.status, final.error) == ("failed", "approval_required")


def test_a_denied_approval_removes_a_stale_workspace(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job, owner = _pending_job(repository, job_id="denied-workspace")
    workspace = _leave_a_crashed_workspace(repository, job.job_id)
    repository.decide_approval(job.job_id, owner=owner, decision="denied")

    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    try:
        coordinator._launch_code(job.job_id)
        wait_until(
            lambda: not workspace.exists(),
            timeout=5.0,
            describe="the stale workspace to be removed",
        )
    finally:
        coordinator.shutdown()


def test_a_workspace_that_cannot_be_removed_does_not_change_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    workspace = _crash_mid_run(repository, "stubborn-workspace")
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    attempts: list[str] = []

    def refuse(job_id: str) -> None:
        attempts.append(job_id)
        raise CodeExecutionError("workspace_cleanup_failed")

    monkeypatch.setattr(coordinator, "_cleanup_code_workspace", refuse)
    try:
        coordinator.startup_recover()
        final = coordinator.wait("stubborn-workspace", timeout=10.0)
        wait_until(
            lambda: "stubborn-workspace" not in coordinator.active_code_job_ids(),
            timeout=5.0,
            describe="the refused job's thread to exit",
        )
    finally:
        coordinator.shutdown()

    assert attempts == ["stubborn-workspace"]
    assert (final.status, final.error) == ("cancelled", "approval_expired")
    assert workspace.exists()  # left for a later pass, never a reason to reopen the job


def test_a_refusal_never_follows_a_link_out_of_the_workspace_root(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job, _ = _approved_job(repository, job_id="linked-workspace")
    repository.claim_approved_lease(job.job_id, lease_owner="dead-worker", ttl_seconds=0.01)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the run's to delete", encoding="utf-8")
    root = repository.artifact_root / ".code_workspaces"
    root.mkdir(parents=True)
    try:
        (root / job.job_id).symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")
    repository.release_lease(job.job_id, lease_owner="dead-worker")

    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    try:
        coordinator._launch_code(job.job_id)
        final = coordinator.wait(job.job_id, timeout=10.0)
        wait_until(
            lambda: job.job_id not in coordinator.active_code_job_ids(),
            timeout=5.0,
            describe="the refused job's thread to exit",
        )
    finally:
        coordinator.shutdown()

    assert final.status == "cancelled"
    assert (root / job.job_id).is_symlink()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "not the run's to delete"
