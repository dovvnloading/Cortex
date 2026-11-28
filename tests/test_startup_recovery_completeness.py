"""Startup recovery reaches every job the previous process left (#577, #623).

- A job lease left by a process that has exited is reclaimed at startup even
  when its timestamp has not expired yet (Cortex reopened quickly), so the
  job is not stranded until some later restart (#577).
- A scratch or image job admitted but never leased is relaunched too: no
  lease recovery can find it (#577).
- Recovery scans every unfinished job, not a 200-row task-tray listing (#623).
"""

from __future__ import annotations

import pytest

from cortex_backend.execution.code_execution import CodeExecutionRequest
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.scratch_compute import SCRATCH_COMPUTE_PROFILE, SCRATCH_PAYLOAD_SCHEMA
from support import wait_until


def _scratch_job(coordinator: LocalExecutionCoordinator, name: str, expression: str = "6 * 7"):
    repository = coordinator.repository
    job, created = repository.create_job(
        job_id=name,
        owner=repository.installation_principal_id,
        request_id=name,
        profile=SCRATCH_COMPUTE_PROFILE,
        payload={"schema_version": SCRATCH_PAYLOAD_SCHEMA, "expression": expression},
    )
    assert created
    return job


def _status(coordinator: LocalExecutionCoordinator, job_id: str) -> str:
    job = coordinator.repository.get_job(job_id)
    assert job is not None
    return job.status


def test_a_job_with_a_dead_process_live_lease_is_recovered_at_startup(
    coordinator: LocalExecutionCoordinator,
) -> None:
    job = _scratch_job(coordinator, "scratch-live-dead-lease")
    # The previous process claimed it 60 seconds' worth and then exited.
    coordinator.repository.claim_lease(job.job_id, lease_owner="previous-process", ttl_seconds=60)

    recovered = coordinator.startup_recover()

    assert job.job_id in recovered
    wait_until(
        lambda: _status(coordinator, job.job_id) == "succeeded",
        timeout=30.0,
        describe="the recovered scratch job to finish",
    )


def test_a_cancelling_job_with_a_dead_process_live_lease_ends_cancelled(
    coordinator: LocalExecutionCoordinator,
) -> None:
    job = _scratch_job(coordinator, "scratch-cancelling-dead-lease")
    coordinator.repository.claim_lease(job.job_id, lease_owner="previous-process", ttl_seconds=60)
    coordinator.repository.request_cancel(job.job_id)

    coordinator.startup_recover()

    wait_until(
        lambda: _status(coordinator, job.job_id) == "cancelled",
        timeout=10.0,
        describe="the stranded cancellation to finish",
    )


def test_a_scratch_job_admitted_but_never_leased_is_relaunched(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _scratch_job(coordinator, "scratch-never-leased")
    launched: list[str] = []
    monkeypatch.setattr(coordinator, "_launch_scratch", lambda job_id, request: launched.append(job_id))

    coordinator.startup_recover()

    assert launched == [job.job_id]


def test_an_image_job_admitted_but_never_leased_is_handed_to_recipe_recovery(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = coordinator.repository
    job, _ = repository.create_job(
        job_id="image-never-leased",
        owner=repository.installation_principal_id,
        request_id="image-never-leased",
        profile="recipe.image.v1",
        payload={},
    )
    handed: list[list[str]] = []
    monkeypatch.setattr(coordinator._recipe, "recover_jobs", lambda ids: handed.append(list(ids)))

    coordinator.startup_recover()

    assert [job.job_id] in handed


def test_every_unfinished_code_job_gets_a_worker_not_only_the_first_200(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = coordinator.repository
    owner = repository.installation_principal_id
    created: list[str] = []
    for index in range(201):
        request = CodeExecutionRequest(
            owner=owner,
            request_id=f"pending-{index:03d}",
            source="_result = 1",
            intent_summary="Wait for a decision.",
        )
        job, _ = repository.create_job(
            job_id=f"pending-{index:03d}",
            owner=owner,
            request_id=request.request_id,
            profile="code.exec.v1",
            payload=request.payload(),
        )
        repository.request_approval(
            job.job_id, owner=owner, scope_digest=request.approval_scope_digest, reason="Compute."
        )
        created.append(job.job_id)
    launched: list[str] = []
    monkeypatch.setattr(coordinator, "_launch_code", launched.append)

    coordinator.startup_recover()

    assert sorted(launched) == sorted(created)
    assert len(repository.nonterminal_jobs(owner=owner)) == 201
