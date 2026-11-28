"""A coordinator that loses a job's lease leaves that job alone (issue #627).

The code profile already returned on LeaseConflict. Scratch and image jobs
instead wrote ``failed``/``lease_unavailable`` over the job, although the
losing attempt never owned it and the winner was still running it.
"""

from __future__ import annotations

from pathlib import Path
from threading import Event

from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.recipe_coordinator import RecipeExecutionCoordinator
from cortex_backend.execution.scratch_compute import SCRATCH_PAYLOAD_SCHEMA, ScratchComputeRequest
from test_recipe_coordinator import OWNER, _FakeAttempt, _output, _repository, _request


def test_a_scratch_attempt_that_loses_the_lease_writes_nothing(
    coordinator: LocalExecutionCoordinator,
) -> None:
    repository = coordinator.repository
    owner = repository.installation_principal_id
    job, created = repository.create_job(
        job_id="scratch-owned-elsewhere",
        owner=owner,
        request_id="scratch-owned-elsewhere",
        profile="scratch.compute.v1",
        payload={"schema_version": SCRATCH_PAYLOAD_SCHEMA, "expression": "1 + 1"},
    )
    assert created
    # Coordinator A holds the job and is running it.
    repository.claim_lease(job.job_id, lease_owner="coordinator-a", ttl_seconds=60)
    before = repository.get_job(job.job_id)

    # Coordinator B starts the same job and loses the claim.
    coordinator._run_scratch(
        job.job_id,
        ScratchComputeRequest(owner=owner, request_id=job.request_id, expression="1 + 1"),
        Event(),
    )

    after = repository.get_job(job.job_id)
    assert before is not None and after is not None
    assert after.status == before.status
    assert after.error is None
    assert repository.lease_holder(job.job_id) == "coordinator-a"


def test_an_image_attempt_that_loses_the_lease_writes_nothing(tmp_path: Path) -> None:
    repository, _source_job, source_artifact_id = _repository(tmp_path)
    request = _request(source_artifact_id)
    attempts: list[_FakeAttempt] = []

    def factory(_job: object) -> _FakeAttempt:
        attempt = _FakeAttempt(_output())
        attempts.append(attempt)
        return attempt

    coordinator = RecipeExecutionCoordinator(repository, factory)
    try:
        job, created = repository.create_job(
            job_id="image-owned-elsewhere",
            owner=OWNER,
            request_id=request.request_id,
            profile="recipe.image.v1",
            payload=coordinator._payload_for_request(request),
        )
        assert created
        repository.claim_lease(job.job_id, lease_owner="coordinator-a", ttl_seconds=60)
        before = repository.get_job(job.job_id)

        coordinator._run(job.job_id, request, Event())

        after = repository.get_job(job.job_id)
        assert before is not None and after is not None
        assert after.status == before.status
        assert after.error is None
        assert repository.lease_holder(job.job_id) == "coordinator-a"
        assert attempts == []
    finally:
        coordinator.shutdown()
