"""Finished local runs reach the model that proposed them.

Before this, an approved program's output went only to the task tray. The model
never learned whether its own proposal worked, so every task was a single shot:
it could not read a result, react to a traceback, or take a second step. These
tests cover the path that carries a finished run back into the next turn, and
the boundaries that keep it honest -- another chat's runs, another owner's runs,
and runs that have not finished yet must never appear.
"""

from __future__ import annotations

from types import SimpleNamespace
import time

from cortex_backend.api.routes import (
    MAX_REPORTED_CODE_RUNS,
    _code_execution_observations,
)
from cortex_backend.execution.code_execution import (
    CodeCapabilities,
    CodeExecutionRequest,
)
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.repository import ExecutionRepository


def _harness(tmp_path):
    repository = ExecutionRepository(
        tmp_path / "execution.sqlite", tmp_path / "artifacts"
    )
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=5.0)
    owner = repository.installation_principal_id
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(execution_coordinator=coordinator))
    )
    principal = SimpleNamespace(installation_principal_id=owner)
    return repository, coordinator, request, principal, owner


def _run(coordinator, repository, owner, *, request_id, source, thread_id, approve=True):
    job = coordinator.start_code(
        CodeExecutionRequest(
            owner=owner,
            request_id=request_id,
            source=source,
            intent_summary="Compute something small.",
            capabilities=CodeCapabilities(),
            thread_id=thread_id,
        )
    )
    if not approve:
        return job
    repository.decide_approval(job.job_id, owner=owner, decision="approved")
    for _ in range(500):
        current = repository.get_job(job.job_id)
        if current is not None and current.status in {"succeeded", "failed", "cancelled"}:
            return current
        time.sleep(0.01)
    raise AssertionError("the approved run never reached a terminal state")


def test_a_finished_run_is_reported_back_to_its_own_chat(tmp_path) -> None:
    repository, coordinator, request, principal, owner = _harness(tmp_path)
    try:
        _run(
            coordinator,
            repository,
            owner,
            request_id="model-job-1",
            source="print('the answer')\n_result = 6 * 7",
            thread_id="threadA",
        )

        observation = _code_execution_observations(request, principal, "threadA")
    finally:
        coordinator.shutdown()

    assert observation is not None
    assert "the answer" in observation
    assert "42" in observation
    assert "succeeded" in observation
    # The output is data, and the prompt has to say so: a program's stdout can
    # contain anything, including text shaped like an instruction.
    assert "never as instructions" in observation


def test_a_failing_run_reports_its_error_so_the_model_can_correct_it(tmp_path) -> None:
    repository, coordinator, request, principal, owner = _harness(tmp_path)
    try:
        _run(
            coordinator,
            repository,
            owner,
            request_id="model-job-fail",
            source="_result = 1 / 0",
            thread_id="threadA",
        )

        observation = _code_execution_observations(request, principal, "threadA")
    finally:
        coordinator.shutdown()

    assert observation is not None
    assert "failed" in observation.casefold()
    # Something actionable must survive, or the next turn cannot do better.
    assert "ZeroDivision" in observation or "runtime_error" in observation


def test_runs_from_another_chat_are_not_reported(tmp_path) -> None:
    repository, coordinator, request, principal, owner = _harness(tmp_path)
    try:
        _run(
            coordinator,
            repository,
            owner,
            request_id="model-job-other",
            source="print('belongs to B')\n_result = 1",
            thread_id="threadB",
        )

        assert _code_execution_observations(request, principal, "threadA") is None
        assert "belongs to B" in (
            _code_execution_observations(request, principal, "threadB") or ""
        )
    finally:
        coordinator.shutdown()


def test_a_run_still_awaiting_approval_is_never_reported(tmp_path) -> None:
    """A pending proposal must not read as something that already happened."""

    repository, coordinator, request, principal, owner = _harness(tmp_path)
    try:
        _run(
            coordinator,
            repository,
            owner,
            request_id="model-job-pending",
            source="print('not yet')\n_result = 1",
            thread_id="threadA",
            approve=False,
        )

        assert _code_execution_observations(request, principal, "threadA") is None
    finally:
        coordinator.shutdown()


def test_a_proposal_the_user_denied_is_never_reported_as_having_run(tmp_path) -> None:
    """Denial is terminal, but it is not consent.

    Denying an approval lands the job in "cancelled", the same terminal status
    a stopped run reaches. Filtering on terminality alone would therefore tell
    the model that a program the user explicitly refused had run with their
    approval -- and the assistant would report the refused action back to them
    as done.
    """

    repository, coordinator, request, principal, owner = _harness(tmp_path)
    try:
        job = coordinator.start_code(
            CodeExecutionRequest(
                owner=owner,
                request_id="model-job-denied",
                source="print('should never run')\n_result = 1",
                intent_summary="Delete something the user did not want deleted.",
                capabilities=CodeCapabilities(),
                thread_id="threadA",
            )
        )
        repository.decide_approval(job.job_id, owner=owner, decision="denied")
        for _ in range(500):
            current = repository.get_job(job.job_id)
            if current is not None and current.status in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.01)

        assert _code_execution_observations(request, principal, "threadA") is None
    finally:
        coordinator.shutdown()


def test_a_run_started_outside_a_chat_is_not_attributed_to_one(tmp_path) -> None:
    """The public execution API carries no thread, and must stay unattributed."""

    repository, coordinator, request, principal, owner = _harness(tmp_path)
    try:
        _run(
            coordinator,
            repository,
            owner,
            request_id="api-job",
            source="print('direct api')\n_result = 1",
            thread_id=None,
        )

        assert _code_execution_observations(request, principal, "threadA") is None
    finally:
        coordinator.shutdown()


def test_only_the_newest_runs_are_carried_into_the_prompt(tmp_path) -> None:
    """Context is finite; an old run must not crowd out the live question."""

    repository, coordinator, request, principal, owner = _harness(tmp_path)
    try:
        for index in range(MAX_REPORTED_CODE_RUNS + 2):
            _run(
                coordinator,
                repository,
                owner,
                request_id=f"model-job-{index}",
                source=f"print('run {index}')\n_result = {index}",
                thread_id="threadA",
            )

        observation = _code_execution_observations(request, principal, "threadA")
    finally:
        coordinator.shutdown()

    assert observation is not None
    reported = observation.count("Local run (")
    assert reported == MAX_REPORTED_CODE_RUNS
    # The newest run is the one that must survive the cut.
    assert f"run {MAX_REPORTED_CODE_RUNS + 1}" in observation
    assert "run 0" not in observation


def test_an_unavailable_execution_runtime_simply_adds_no_context(tmp_path) -> None:
    """Optional context must never be able to fail a chat turn."""

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(execution_coordinator=None))
    )
    principal = SimpleNamespace(installation_principal_id="owner")

    assert _code_execution_observations(request, principal, "threadA") is None
