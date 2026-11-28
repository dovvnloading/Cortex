"""Run workspaces of finished or vanished jobs are collected at startup (#621).

A run removes its own workspace when it ends, but a crash after the result
committed, or a removal that failed once, left the directory for good: no
recovery path visits a finished job and the artifact sweep skips the reserved
``.code_workspaces`` directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex_backend.execution.code_execution import CodeExecutionRequest
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator


def _workspace(coordinator: LocalExecutionCoordinator, name: str) -> Path:
    workspace = coordinator.repository.artifact_root / ".code_workspaces" / name
    workspace.mkdir(parents=True)
    (workspace / "leftover.txt").write_text("left by an earlier run", encoding="utf-8")
    return workspace


def _code_job(coordinator: LocalExecutionCoordinator, name: str):
    repository = coordinator.repository
    owner = repository.installation_principal_id
    request = CodeExecutionRequest(
        owner=owner, request_id=name, source="_result = 1", intent_summary="Compute."
    )
    job, _ = repository.create_job(
        job_id=name, owner=owner, request_id=name, profile="code.exec.v1", payload=request.payload()
    )
    return job


def test_startup_removes_workspaces_of_finished_and_unknown_jobs_only(
    coordinator: LocalExecutionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator, "_launch_code", lambda job_id: None)
    finished = _code_job(coordinator, "finished-job")
    coordinator.repository.transition(finished.job_id, status="succeeded", event="completed")
    unfinished = _code_job(coordinator, "unfinished-job")
    finished_workspace = _workspace(coordinator, finished.job_id)
    unknown_workspace = _workspace(coordinator, "job-whose-row-was-retired")
    unfinished_workspace = _workspace(coordinator, unfinished.job_id)
    not_a_workspace = coordinator.repository.artifact_root / ".code_workspaces" / "not a job id"
    not_a_workspace.mkdir()

    coordinator.startup_recover()

    assert not finished_workspace.exists()
    assert not unknown_workspace.exists()
    assert unfinished_workspace.exists()  # its relaunch resets it
    assert not_a_workspace.exists()


def test_a_workspace_root_that_is_a_link_is_never_followed(
    coordinator: LocalExecutionCoordinator, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "some-job").mkdir(parents=True)
    root = coordinator.repository.artifact_root / ".code_workspaces"
    try:
        root.symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("creating a symbolic link needs a privilege this machine lacks")

    coordinator.startup_recover()

    assert (elsewhere / "some-job").exists()
