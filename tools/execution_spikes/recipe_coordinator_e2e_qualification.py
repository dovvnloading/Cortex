"""Durable recipe-coordinator qualification against the signed native worker.

This probe is intentionally separate from normal Cortex startup.  It creates a
disposable repository, signs and installs the already-built fixed worker with
an ephemeral key, composes the explicit native coordinator factory, and then
drives the same owner-scoped attachment, transform, cancellation, retention,
publication, and cleanup boundaries that a qualified caller would use.

The probe is Windows-only by design.  A non-Windows invocation is reported as
``native_windows_required``; it never substitutes a host process, an in-memory
worker, or an alternate transport.  No source path or executable authority is
placed in a job payload, and all temporary state is removed before the probe
returns.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import time
from tempfile import mkdtemp
from typing import Any
from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[2]
SPIKE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(SPIKE_ROOT))

from cortex_backend.execution.artifact_boundary import ArtifactBoundary  # noqa: E402
from cortex_backend.execution.attachment_staging import (  # noqa: E402
    AttachmentStagingService,
)
from cortex_backend.execution.bundle_installer import SignedBundleInstaller  # noqa: E402
from cortex_backend.execution.native_win32 import NativeWin32ProcessFactory  # noqa: E402
from cortex_backend.execution.qualification import (  # noqa: E402
    build_native_recipe_coordinator_factory,
)
from cortex_backend.execution.recipe_coordinator import (  # noqa: E402
    RecipeExecutionCoordinator,
    RecipeExecutionError,
    RecipeImageRequest,
)
from cortex_backend.execution.repository import (  # noqa: E402
    ExecutionRepository,
    ExecutionRepositoryError,
)
from cortex_backend.execution.worker_provenance import verify_active_worker  # noqa: E402
from recipe_worker_e2e_qualification import (  # noqa: E402
    _bounded_cleanup,
    _current_user_sid,
    _fixed_png,
    _install_ephemeral,
    _plan,
    _process_executable,
    _slow_png,
)


DEFAULT_TIMEOUT_SECONDS = 300.0
ATTACHMENT_RETENTION_SECONDS = 300
OUTPUT_RETENTION_SECONDS = 300
SHORT_RETENTION_SECONDS = 1
FOREIGN_OWNER = "f" * 64


class QualificationFailure(RuntimeError):
    """A deterministic, redacted coordinator qualification failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _RecordingProcessFactory:
    """Record native workers so the qualification can prove they were closed."""

    def __init__(self, workers: list[Any]) -> None:
        self._workers = workers
        self._delegate = NativeWin32ProcessFactory()

    def create_suspended(self, plan: Any) -> Any:
        worker = self._delegate.create_suspended(plan)
        self._workers.append(worker)
        return worker


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise QualificationFailure(code)


def _wait_until(predicate: Callable[[], bool], timeout_seconds: float, code: str) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise QualificationFailure(code)
        time.sleep(0.01)


def _artifact_ids_for_job(repository: ExecutionRepository, job_id: str) -> list[str]:
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT artifact_id FROM execution_artifacts WHERE job_id = ? ORDER BY artifact_id",
            (job_id,),
        ).fetchall()
    return [str(row["artifact_id"]) for row in rows]


def _temporary_recipe_directories(repository: ExecutionRepository) -> list[Path]:
    return [
        path
        for path in repository.artifact_root.iterdir()
        if path.name.startswith(".recipe-")
    ]


def _wait_for_worker(
    repository: ExecutionRepository,
    job_id: str,
    workers: list[Any],
    minimum_worker_count: int,
    timeout_seconds: float,
) -> None:
    """Wait until the coordinator has both entered the worker phase and launched it."""

    def ready() -> bool:
        job = repository.get_job(job_id)
        if job is None:
            return False
        phases = {event.phase for event in repository.events(job_id)}
        return "worker" in phases and len(workers) > minimum_worker_count

    _wait_until(ready, timeout_seconds, "worker_launch_timeout")


def _successful_transform(
    repository: ExecutionRepository,
    coordinator: RecipeExecutionCoordinator,
    workers: list[Any],
    owner: str,
    source_artifact_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    request = RecipeImageRequest(
        owner=owner,
        request_id="recipe-transform",
        source_artifact_id=source_artifact_id,
        plan=_plan(artifact_id=source_artifact_id),
        retention_seconds=OUTPUT_RETENTION_SECONDS,
    )
    accepted = coordinator.start_image_transform(request)
    completed = coordinator.wait(accepted.job_id, timeout=timeout_seconds)
    _require(completed.status == "succeeded", "coordinator_transform_not_succeeded")
    _require(isinstance(completed.result, dict), "coordinator_result_missing")
    result = completed.result
    expected_keys = {
        "schema_version",
        "artifact_id",
        "mime_type",
        "format",
        "size",
        "sha256",
        "width",
        "height",
        "plan_digest",
    }
    _require(set(result) == expected_keys, "coordinator_result_shape_invalid")
    _require("path" not in result, "coordinator_result_path_leak")
    output_id = result.get("artifact_id")
    _require(isinstance(output_id, str), "coordinator_result_artifact_invalid")
    artifact = repository.get_artifact(output_id, owner=owner)
    _require(artifact is not None, "coordinator_output_unavailable")
    _require(artifact.job_id == accepted.job_id, "coordinator_output_owner_mismatch")
    content = repository.read_artifact(output_id)
    _require(sha256(content).hexdigest() == result.get("sha256"), "coordinator_output_hash_invalid")
    _require(artifact.sha256 == result.get("sha256"), "coordinator_output_digest_mismatch")
    _require(artifact.mime_type == result.get("mime_type") == "image/png", "coordinator_output_mime_invalid")
    _require(result.get("size") == len(content) == artifact.size, "coordinator_output_size_invalid")
    _require(result.get("width") == 4 and result.get("height") == 3, "coordinator_output_dimensions_invalid")
    _require(not _temporary_recipe_directories(repository), "atomic_publication_staging_leaked")
    _require(bool(workers), "native_worker_not_launched")
    return {
        "status": "passed",
        "job_id": accepted.job_id,
        "source_artifact_id": source_artifact_id,
        "output_artifact_id": output_id,
        "output_sha256": artifact.sha256,
    }


def _owner_isolation(
    repository: ExecutionRepository,
    coordinator: RecipeExecutionCoordinator,
    source_artifact_id: str,
) -> dict[str, Any]:
    _require(
        repository.get_artifact(source_artifact_id, owner=FOREIGN_OWNER) is None,
        "owner_artifact_metadata_leak",
    )
    try:
        coordinator.start_image_transform(
            RecipeImageRequest(
                owner=FOREIGN_OWNER,
                request_id="foreign-transform",
                source_artifact_id=source_artifact_id,
                plan=_plan(artifact_id=source_artifact_id),
                retention_seconds=OUTPUT_RETENTION_SECONDS,
            )
        )
    except RecipeExecutionError as error:
        _require(error.code == "input_artifact_unavailable", "owner_isolation_error_invalid")
    else:
        raise QualificationFailure("owner_isolation_bypass")
    return {"status": "passed", "source_artifact_id": source_artifact_id}


def _retention(
    repository: ExecutionRepository,
    staging: AttachmentStagingService,
    owner: str,
) -> dict[str, Any]:
    attachment = staging.stage(
        owner=owner,
        request_id="attachment-short-retention",
        content=_fixed_png(),
        retention_seconds=SHORT_RETENTION_SECONDS,
    )
    time.sleep(SHORT_RETENTION_SECONDS + 0.25)
    try:
        repository.read_artifact(attachment.artifact.artifact_id)
    except ExecutionRepositoryError:
        pass
    else:
        raise QualificationFailure("retention_expiry_not_enforced")
    removed = repository.cleanup_expired().artifacts
    _require(repository.get_artifact(attachment.artifact.artifact_id, owner=owner) is None, "retention_purge_failed")
    return {
        "status": "passed",
        "artifact_id": attachment.artifact.artifact_id,
        "purged_artifacts": removed,
    }


def _cancellation(
    repository: ExecutionRepository,
    coordinator: RecipeExecutionCoordinator,
    staging: AttachmentStagingService,
    workers: list[Any],
    owner: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    attachment = staging.stage(
        owner=owner,
        request_id="attachment-cancellation",
        content=_slow_png(),
        retention_seconds=ATTACHMENT_RETENTION_SECONDS,
    )
    source_id = attachment.artifact.artifact_id
    accepted = coordinator.start_image_transform(
        RecipeImageRequest(
            owner=owner,
            request_id="recipe-cancellation",
            source_artifact_id=source_id,
            plan=_plan(artifact_id=source_id, slow=True),
            retention_seconds=OUTPUT_RETENTION_SECONDS,
        )
    )
    worker_count_before = len(workers)
    _wait_for_worker(
        repository,
        accepted.job_id,
        workers,
        worker_count_before,
        timeout_seconds,
    )
    coordinator.cancel(accepted.job_id, owner=owner)
    completed = coordinator.wait(accepted.job_id, timeout=timeout_seconds)
    _require(completed.status == "cancelled", "coordinator_cancellation_not_terminal")
    _require(completed.error == "cancelled", "coordinator_cancellation_error_invalid")
    _require(completed.result is None, "coordinator_cancellation_published_result")
    _require(not _artifact_ids_for_job(repository, accepted.job_id), "coordinator_cancellation_artifact_leak")
    _require(not _temporary_recipe_directories(repository), "coordinator_cancellation_staging_leaked")
    return {"status": "passed", "job_id": accepted.job_id, "source_artifact_id": source_id}


def _native_cleanup(
    workers: list[Any],
    workspace: Path,
) -> None:
    """Treat a still-running workspace worker as a qualification failure."""

    leaked: list[int] = []
    root = workspace.resolve()
    for worker in workers:
        process_id = getattr(worker, "process_id", None)
        if type(process_id) is not int:
            continue
        executable = _process_executable(process_id)
        if executable is not None:
            try:
                if executable.is_relative_to(root):
                    leaked.append(process_id)
            except (OSError, RuntimeError):
                leaked.append(process_id)
    _require(not leaked, "native_worker_process_leaked")


def _remove_workspace(workspace: Path, *, timeout_seconds: float = 10.0) -> None:
    """Retry temporary-tree removal while native Windows handles settle."""

    deadline = time.monotonic() + timeout_seconds
    last_error: OSError | None = None
    while workspace.exists():
        try:
            shutil.rmtree(workspace)
        except FileNotFoundError:
            return
        except OSError as error:
            last_error = error
        if not workspace.exists():
            return
        if time.monotonic() >= deadline:
            if last_error is not None:
                raise last_error
            raise OSError("qualification workspace cleanup timed out")
        time.sleep(0.05)


def qualify(
    source_root: Path,
    *,
    timeout_seconds: float,
    case_name: str | None = None,
) -> dict[str, Any]:
    """Run the strict durable coordinator qualification corpus."""

    if os.name != "nt":
        return {"status": "blocked", "code": "native_windows_required", "stages": [], "cases": {}}
    if not source_root.is_dir():
        return {"status": "blocked", "code": "package_missing", "stages": [], "cases": {}}

    workspace = Path(mkdtemp(prefix="cortex-recipe-coordinator-e2e-"))
    stages: list[str] = []
    cases: dict[str, dict[str, Any]] = {}
    workers: list[Any] = []
    repository: ExecutionRepository | None = None
    coordinator: RecipeExecutionCoordinator | None = None
    cleanup_error: str | None = None
    result: dict[str, Any] | None = None
    try:
        installer: SignedBundleInstaller = _install_ephemeral(source_root, workspace / "store")
        stages.extend(["signed_ephemeral_manifest", "installed_immutable_generation"])
        verify_active_worker(installer)
        stages.extend(["provenance_verified", "no_host_fallback_configured"])

        repository = ExecutionRepository(
            workspace / "execution.sqlite",
            workspace / "artifacts",
            max_artifact_bytes=10 * 1024 * 1024,
        )
        boundary = ArtifactBoundary(repository)
        staging = AttachmentStagingService(repository, boundary)
        owner = repository.installation_principal_id
        _require(len(owner) == 64 and all(character in "0123456789abcdef" for character in owner), "installation_owner_invalid")
        _require(owner != FOREIGN_OWNER, "owner_identity_collision")

        def process_factory_factory() -> _RecordingProcessFactory:
            return _RecordingProcessFactory(workers)

        factory = build_native_recipe_coordinator_factory(
            installer,
            allowed_user_sids=frozenset({_current_user_sid()}),
            process_factory_factory=process_factory_factory,
            accept_timeout_seconds=min(timeout_seconds, 30.0),
            worker_timeout_seconds=min(timeout_seconds, 120.0),
            cancel_grace_seconds=5.0,
            artifact_boundary_factory=lambda current: boundary if current is repository else ArtifactBoundary(current),
        )
        coordinator = factory(repository)
        stages.extend(["repository_ready", "attachment_boundary_ready", "native_coordinator_composed"])
        base_attachment = staging.stage(
            owner=owner,
            request_id="attachment-base",
            content=_fixed_png(),
            retention_seconds=ATTACHMENT_RETENTION_SECONDS,
        )
        _require(base_attachment.job.status == "succeeded", "attachment_stage_not_succeeded")
        source_artifact_id = base_attachment.artifact.artifact_id
        stages.append("attachment_staged")

        selected = {
            "transform": lambda: _successful_transform(
                repository,
                coordinator,
                workers,
                owner,
                source_artifact_id,
                timeout_seconds,
            ),
            "owner_isolation": lambda: _owner_isolation(
                repository,
                coordinator,
                source_artifact_id,
            ),
            "retention": lambda: _retention(repository, staging, owner),
            "cancellation": lambda: _cancellation(
                repository,
                coordinator,
                staging,
                workers,
                owner,
                timeout_seconds,
            ),
        }
        for name, action in selected.items():
            if case_name is not None and case_name != name:
                continue
            try:
                cases[name] = action()
                stages.append(f"{name}_verified")
            except QualificationFailure as error:
                cases[name] = {"status": "blocked", "code": error.code}
                raise
            except Exception as error:
                cases[name] = {"status": "blocked", "code": "qualification_case_failed", "error_type": type(error).__name__}
                raise
        _require(case_name is None or case_name in selected, "qualification_case_invalid")
        result = {"status": "passed", "stages": stages, "cases": cases}
    except QualificationFailure as error:
        result = {"status": "blocked", "code": error.code, "stages": stages, "cases": cases}
    except Exception:
        result = {"status": "blocked", "code": "qualification_failed_closed", "stages": stages, "cases": cases}
    finally:
        if coordinator is not None:
            try:
                coordinator.shutdown(timeout=min(timeout_seconds, 30.0))
            except Exception:
                cleanup_error = "coordinator_shutdown_failed"
        if repository is not None:
            try:
                _native_cleanup(workers, workspace)
                stages.append("native_worker_processes_closed")
            except QualificationFailure as error:
                cleanup_error = error.code
        _bounded_cleanup(
            lambda: _remove_workspace(workspace),
            timeout_seconds=15.0,
        )
        if workspace.exists():
            cleanup_error = cleanup_error or "qualification_workspace_cleanup_failed"

    assert result is not None
    if cleanup_error is not None:
        result = {
            "status": "blocked",
            "code": cleanup_error,
            "stages": stages,
            "cases": cases,
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT / "dist" / "recipe-runtime")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--case",
        choices=("transform", "owner_isolation", "retention", "cancellation"),
        help="qualify one coordinator case instead of the full corpus",
    )
    args = parser.parse_args(argv)
    if not 10 <= args.timeout_seconds <= 600:
        parser.error("--timeout-seconds must be between 10 and 600")
    result = qualify(
        args.source_root.resolve(),
        timeout_seconds=args.timeout_seconds,
        case_name=args.case,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":") if args.json else None))
    return 2 if args.strict and result["status"] != "passed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
