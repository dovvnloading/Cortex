"""How every profile records a run that did not succeed, and that a Stop is never overwritten."""

from __future__ import annotations

from threading import Event

from cortex_backend.execution.finish import UnsuccessfulJobWording, finish_unsuccessful_job
from cortex_backend.execution.repository import ExecutionRepository, ExecutionTransitionConflict

WORDING = UnsuccessfulJobWording(
    cancelled_event="cancelled",
    failed_event="failed",
    cancelled_message="Test run was cancelled.",
    failed_message="Test run failed safely.",
)


def _running_job(repository: ExecutionRepository, name: str = "job-finish", profile: str = "scratch.auto.v1"):
    job, _ = repository.create_job(
        job_id=name,
        owner=repository.installation_principal_id,
        request_id=f"request-{name}",
        profile=profile,
        payload={},
    )
    return repository.transition(
        job.job_id, status="running", event="started", phase="prepare", data={"message": "go"}
    )


def _stop_before_the_first_failure_write(repository: ExecutionRepository, monkeypatch) -> list[str]:
    """Commit a Stop after the caller has read the job and before it writes ``failed``."""

    real_transition = repository.transition
    injected: list[str] = []

    def transition_after_a_stop(job_id, **kwargs):
        if kwargs.get("status") == "failed" and not injected:
            injected.append(job_id)
            repository.request_cancel(job_id)
        return real_transition(job_id, **kwargs)

    monkeypatch.setattr(repository, "transition", transition_after_a_stop)
    return injected


def test_a_failure_is_recorded_as_failed_when_nobody_asked_to_stop(execution_repository):
    job = _running_job(execution_repository)

    finish_unsuccessful_job(
        execution_repository,
        job.job_id,
        failure_code="worker_failed",
        cancel_requested=Event().is_set,
        wording=WORDING,
    )

    finished = execution_repository.get_job(job.job_id)
    assert finished is not None
    assert (finished.status, finished.error) == ("failed", "worker_failed")
    assert execution_repository.events(job.job_id)[-1].event == "failed"


def test_an_in_process_stop_is_recorded_as_cancelled(execution_repository):
    job = _running_job(execution_repository)
    stop = Event()
    stop.set()

    finish_unsuccessful_job(
        execution_repository,
        job.job_id,
        failure_code="worker_failed",
        cancel_requested=stop.is_set,
        wording=WORDING,
    )

    finished = execution_repository.get_job(job.job_id)
    assert finished is not None
    assert (finished.status, finished.error) == ("cancelled", "cancelled")


def test_a_stop_committed_between_the_read_and_the_write_is_not_overwritten(
    execution_repository, monkeypatch
):
    job = _running_job(execution_repository)
    injected = _stop_before_the_first_failure_write(execution_repository, monkeypatch)

    finish_unsuccessful_job(
        execution_repository,
        job.job_id,
        failure_code="worker_failed",
        cancel_requested=Event().is_set,
        wording=WORDING,
    )

    finished = execution_repository.get_job(job.job_id)
    assert injected == [job.job_id]
    assert finished is not None
    assert (finished.status, finished.error) == ("cancelled", "cancelled")
    assert execution_repository.events(job.job_id)[-1].event == "cancelled"


def test_a_finished_or_missing_job_is_left_alone(execution_repository):
    job = _running_job(execution_repository)
    execution_repository.transition(
        job.job_id,
        status="succeeded",
        event="completed",
        phase="completed",
        data={"message": "done"},
        result={"value": "1"},
    )
    before = execution_repository.events(job.job_id)

    finish_unsuccessful_job(
        execution_repository,
        job.job_id,
        failure_code="worker_failed",
        cancel_requested=Event().is_set,
        wording=WORDING,
    )
    finish_unsuccessful_job(
        execution_repository,
        "job-that-does-not-exist",
        failure_code="worker_failed",
        cancel_requested=Event().is_set,
        wording=WORDING,
    )

    assert execution_repository.events(job.job_id) == before
    assert execution_repository.get_job(job.job_id).status == "succeeded"


def test_persistent_conflicts_give_up_quietly_after_a_bounded_number_of_reads(
    execution_repository, monkeypatch, caplog
):
    job = _running_job(execution_repository)
    attempts: list[str] = []

    def always_conflicts(job_id, **kwargs):
        attempts.append(job_id)
        raise ExecutionTransitionConflict("someone else moved it")

    monkeypatch.setattr(execution_repository, "transition", always_conflicts)

    with caplog.at_level("WARNING", logger="cortex.execution.finish"):
        finish_unsuccessful_job(
            execution_repository,
            job.job_id,
            failure_code="worker_failed",
            cancel_requested=Event().is_set,
            wording=WORDING,
        )

    assert len(attempts) == 3
    assert any("non-terminal" in record.getMessage() for record in caplog.records)
    assert job.job_id not in caplog.text


def test_an_unexpected_store_error_is_contained(execution_repository, monkeypatch, caplog):
    job = _running_job(execution_repository)

    def broken(job_id, **kwargs):
        raise RuntimeError("simulated persistence outage")

    monkeypatch.setattr(execution_repository, "transition", broken)

    with caplog.at_level("WARNING", logger="cortex.execution.finish"):
        finish_unsuccessful_job(
            execution_repository,
            job.job_id,
            failure_code="worker_failed",
            cancel_requested=Event().is_set,
            wording=WORDING,
        )

    assert any("RuntimeError" in record.getMessage() for record in caplog.records)
    assert "simulated persistence outage" not in caplog.text


def test_the_scratch_profile_does_not_overwrite_a_stop_with_a_failure(
    coordinator, monkeypatch
):
    """``_finish_scratch_failure`` read the job and then wrote with no guard.

    A Stop committing between the two was recorded as ``failed`` where the user
    asked for ``cancelled``; the code profile already re-decided after a
    conflict.
    """

    repository = coordinator.repository
    job = _running_job(repository, "job-scratch-finish")
    injected = _stop_before_the_first_failure_write(repository, monkeypatch)

    coordinator._finish_scratch_failure(job.job_id, Event(), "worker_failed")

    finished = repository.get_job(job.job_id)
    assert injected == [job.job_id]
    assert finished is not None
    assert (finished.status, finished.error) == ("cancelled", "cancelled")
    assert repository.events(job.job_id)[-1].event == "cancelled"


def test_the_code_profile_keeps_its_own_event_names(coordinator):
    repository = coordinator.repository
    job = _running_job(repository, "job-code-finish", profile="code.exec.v1")

    coordinator._finish_code_failure(job.job_id, Event(), "runtime_error")

    finished = repository.get_job(job.job_id)
    assert finished is not None
    assert (finished.status, finished.error) == ("failed", "runtime_error")
    assert repository.events(job.job_id)[-1].event == "code.failed"
