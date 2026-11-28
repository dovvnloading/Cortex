"""Stop must win over code-job admission and approval races (issues #579, #622)."""

from __future__ import annotations

import dataclasses
from threading import Event, Thread

import pytest

from cortex_backend.execution.code_execution import CodeExecutionRequest
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.models import ExecutionJob
from cortex_backend.execution.repository import ApprovalTransitionError


def _request(coordinator: LocalExecutionCoordinator, name: str) -> CodeExecutionRequest:
    return CodeExecutionRequest(
        owner=coordinator.repository.installation_principal_id,
        request_id=name,
        source="_result = 7",
        intent_summary="Compute a small number.",
    )


def test_stop_after_a_concurrent_approval_still_cancels_the_job(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No worker thread: the job stays approved-but-unlaunched, which is the
    # window the stale-snapshot cancel used to lose.
    monkeypatch.setattr(coordinator, "_launch_code", lambda job_id: None)
    repository = coordinator.repository
    owner = repository.installation_principal_id
    job = coordinator.start_code(_request(coordinator, "approve-then-stop"))
    assert job.approval_state == "pending"

    # Stop reads the job while it is still pending ...
    stale = repository.get_job(job.job_id, owner=owner)
    assert stale is not None
    # ... another client approves before Stop writes its denial.
    repository.decide_approval(job.job_id, owner=owner, decision="approved")

    original_get_job = repository.get_job
    calls = {"n": 0}

    def first_read_is_stale(job_id: str, owner: str | None = None) -> ExecutionJob | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return stale
        return original_get_job(job_id, owner=owner)

    monkeypatch.setattr(repository, "get_job", first_read_is_stale)

    result = coordinator.cancel(job.job_id, owner=owner)

    monkeypatch.setattr(repository, "get_job", original_get_job)
    current = repository.get_job(job.job_id, owner=owner)
    assert current is not None
    assert current.approval_state == "approved"
    assert current.status == "cancelling"
    assert result.status == "cancelling"


def test_stop_between_admission_and_approval_creates_no_prompt(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = coordinator.repository
    owner = repository.installation_principal_id
    launched: list[str] = []
    monkeypatch.setattr(coordinator, "_launch_code", launched.append)
    original_request_approval = repository.request_approval

    def stop_first(job_id: str, **kwargs: object) -> object:
        # Another client presses Stop after the job row committed but before
        # the approval row is written.
        coordinator.cancel(job_id, owner=owner)
        return original_request_approval(job_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repository, "request_approval", stop_first)

    job = coordinator.start_code(_request(coordinator, "stop-before-approval"))

    assert job.status == "cancelled"
    assert repository.get_approval(job.job_id, owner=owner) is None
    assert launched == []


def test_a_cancelling_job_cannot_request_approval(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator, "_launch_code", lambda job_id: None)
    repository = coordinator.repository
    owner = repository.installation_principal_id
    job, created = repository.create_job(
        job_id="cancelling-before-approval",
        owner=owner,
        request_id="cancelling-before-approval",
        profile="code.exec.v1",
        payload=_request(coordinator, "cancelling-before-approval").payload(),
    )
    assert created
    repository.request_cancel(job.job_id)

    with pytest.raises(ApprovalTransitionError):
        repository.request_approval(
            job.job_id, owner=owner, scope_digest="digest", reason="Compute."
        )


def test_the_approval_wait_ends_on_a_durable_stop(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator, "_launch_code", lambda job_id: None)
    repository = coordinator.repository
    owner = repository.installation_principal_id
    job = coordinator.start_code(_request(coordinator, "durable-stop-wait"))
    pending = repository.get_job(job.job_id, owner=owner)
    assert pending is not None

    # The waiter re-reads the job; a durable Stop it never got an event for
    # (status cancelling while the approval is still pending) must end it.
    cancelling = dataclasses.replace(pending, status="cancelling")
    calls = {"n": 0}
    original_get_job = repository.get_job

    def read(job_id: str, owner: str | None = None) -> ExecutionJob | None:
        calls["n"] += 1
        return cancelling if job_id == job.job_id else original_get_job(job_id, owner=owner)

    results: list[ExecutionJob | None] = []
    with monkeypatch.context() as patch:
        patch.setattr(repository, "get_job", read)
        # Bounded: a regression parks the waiter instead of failing the test.
        waiter = Thread(
            target=lambda: results.append(coordinator._await_approval(job.job_id, Event())),
            daemon=True,
        )
        waiter.start()
        waiter.join(timeout=5)

    assert not waiter.is_alive(), "the approval wait ignored a durable Stop"
    assert results == [cancelling]
    assert calls["n"] == 1
