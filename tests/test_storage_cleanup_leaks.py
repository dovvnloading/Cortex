"""Nothing the app does on a normal day should leave files behind forever.

Both of these accumulate silently in a user's data directory: one file pair
per launch, and one directory per artifact ever created.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cortex_backend.execution.models import ExecutionArtifact
from cortex_backend.execution.repository import ExecutionRepository
from cortex_backend.repositories.sqlite_settings import SQLiteSettingsRepository
from cortex_backend.repositories.storage import DatabaseManager


def test_backup_rotation_leaves_no_temporary_sidecars(tmp_path: Path) -> None:
    """Validating a copy opens it, which makes SQLite create its sidecars.

    os.replace then moves only the file itself, so "<temp>-wal" and
    "<temp>-shm" were stranded under a name nothing would reference again.
    The backup rotates on every startup, so this grew by two dead files per
    launch for the life of the install.
    """
    db_path = str(tmp_path / "chat.sqlite")
    legacy = str(tmp_path / "legacy")
    manager = DatabaseManager(db_path=db_path, legacy_history_dir=legacy)
    manager.create_chat_from_messages(
        "thread-1", "Title", [{"role": "user", "content": "hello"}]
    )
    for _ in range(9):
        DatabaseManager(db_path=db_path, legacy_history_dir=legacy)

    stranded = [entry.name for entry in tmp_path.iterdir() if ".tmp-" in entry.name]

    assert stranded == [], f"10 startups stranded {len(stranded)} sidecar files"


def test_settings_backup_rotation_leaves_no_temporary_sidecars(tmp_path: Path) -> None:
    """The settings store copies the same way and leaked the same way."""
    repository = SQLiteSettingsRepository(tmp_path / "settings.sqlite")
    original = repository.load().settings
    for revision in range(1, 6):
        repository.save(
            original.model_copy(update={"revision": revision}),
            expected_revision=revision - 1,
        )
        SQLiteSettingsRepository(tmp_path / "settings.sqlite")

    stranded = [entry.name for entry in tmp_path.iterdir() if ".tmp-" in entry.name]

    assert stranded == [], f"stranded {len(stranded)} sidecar files"


def test_retention_removes_the_directory_it_emptied(tmp_path: Path) -> None:
    """Artifacts live one directory per job.

    Deleting the last file left the directory itself behind forever, so every
    attachment and every execution added one that nothing would reclaim.
    """
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    owner = repository.installation_principal_id
    job, _ = repository.create_job(
        job_id="job-1",
        owner=owner,
        request_id="request-1",
        profile="scratch.auto.v1",
        payload={},
    )
    repository.publish_artifact(
        job.job_id, name="out.txt", content=b"bytes", mime_type="text/plain"
    )
    job_directory = repository.artifact_root / job.job_id
    assert job_directory.is_dir()

    repository.transition(job.job_id, status="succeeded", event="completed")
    # Retention is driven by the artifact's own expires_at, so age it rather
    # than waiting out a real clock.
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_artifacts SET expires_at = ?",
            ("2000-01-01T00:00:00+00:00",),
        )

    # Quarantine, then finalize: the pass is deliberately restart-safe and
    # moves one state per call.
    for _ in range(4):
        repository.cleanup_expired(terminal_job_retention_seconds=0, limit=100)

    assert not job_directory.exists(), (
        "the job directory survived after its last artifact was removed"
    )


# ---------------------------------------------------------------------------
# One row that cannot be cleaned up must not stop everything behind it.
# ---------------------------------------------------------------------------

_FAR_FUTURE = "9999-01-01T00:00:00+00:00"
_LONG_AGO = "2000-01-01T00:00:00+00:00"


def _repository(tmp_path: Path) -> ExecutionRepository:
    return ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")


def _expired_artifact(
    repository: ExecutionRepository, job_id: str, *, name: str = "out.txt"
) -> ExecutionArtifact:
    """A terminal job holding one artifact that is already past its retention."""
    job, _ = repository.create_job(
        job_id=job_id,
        owner=repository.installation_principal_id,
        request_id=f"request-{job_id}",
        profile="scratch.auto.v1",
        payload={},
    )
    artifact = repository.publish_artifact(
        job.job_id, name=name, content=b"synthetic", mime_type="text/plain", retention_seconds=1
    )
    repository.transition(job.job_id, status="succeeded", event="completed")
    return artifact


def _point_artifact_at(repository: ExecutionRepository, artifact: ExecutionArtifact, path: Path) -> None:
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_artifacts SET path = ? WHERE artifact_id = ?",
            (str(path), artifact.artifact_id),
        )


def _tombstone(
    repository: ExecutionRepository,
    artifact: ExecutionArtifact,
    *,
    path: Path,
    quarantine: Path,
    created_at: str = _LONG_AGO,
) -> None:
    with repository.connect() as connection:
        connection.execute(
            """
            INSERT INTO execution_artifact_cleanup
                (artifact_id, path, quarantine_path, state, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (artifact.artifact_id, str(path), str(quarantine), created_at),
        )


def _tombstone_count(repository: ExecutionRepository) -> int:
    with repository.connect() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM execution_artifact_cleanup").fetchone()[0])


def _cleanup(repository: ExecutionRepository, *, limit: int = 100):
    return repository.cleanup_expired(
        now=_FAR_FUTURE, terminal_job_retention_seconds=0, limit=limit
    )


def test_one_unvalidatable_tombstone_does_not_disable_retention(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A tombstone pointing outside the artifact root sat at the head of the queue.

    Both path validators raise, there was no per-row handling, and cleanup runs
    the tombstone pass first -- so the exception escaped before artifact, job
    and event retention ever ran, on every pass, forever. The supervisor
    swallowed it into a failure counter and the store grew without bound.
    """
    repository = _repository(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not ours")
    poisoned = _expired_artifact(repository, "poisoned-job", name="poisoned.txt")
    _point_artifact_at(repository, poisoned, outside)
    _tombstone(
        repository,
        poisoned,
        path=outside,
        quarantine=repository.quarantine_root / "poisoned.artifact",
    )
    healthy = _expired_artifact(repository, "healthy-job", name="healthy.txt")
    idle, _ = repository.create_job(
        job_id="idle-job",
        owner=repository.installation_principal_id,
        request_id="request-idle",
        profile="scratch.auto.v1",
        payload={},
    )
    repository.transition(idle.job_id, status="succeeded", event="completed")

    with caplog.at_level(logging.DEBUG, logger="cortex.execution.repository"):
        result = _cleanup(repository)

    # The later artifact and both terminal jobs were reclaimed ...
    assert result.artifacts == 1
    assert repository.get_artifact(healthy.artifact_id) is None
    assert not Path(healthy.path).exists()
    assert repository.get_job("idle-job") is None
    assert repository.get_job("healthy-job") is None
    # ... the poisoned row is reported, dropped, and no longer pins its job ...
    assert result.skipped == 1
    assert _tombstone_count(repository) == 0
    assert repository.get_artifact(poisoned.artifact_id) is None
    assert repository.get_job("poisoned-job") is None
    # ... and the file it pointed at was never touched.
    assert outside.read_bytes() == b"not ours"
    assert str(tmp_path) not in caplog.text


def test_an_expired_artifact_row_outside_the_root_does_not_block_the_rest(tmp_path: Path) -> None:
    """The same defect one step earlier: the batch validated every row up front."""
    repository = _repository(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not ours")
    poisoned = _expired_artifact(repository, "poisoned-job", name="poisoned.txt")
    _point_artifact_at(repository, poisoned, outside)
    healthy = _expired_artifact(repository, "healthy-job", name="healthy.txt")

    result = _cleanup(repository)

    assert (result.artifacts, result.skipped) == (1, 1)
    assert repository.get_artifact(healthy.artifact_id) is None
    assert repository.get_artifact(poisoned.artifact_id) is None
    assert outside.read_bytes() == b"not ours"
    assert _tombstone_count(repository) == 0


def test_a_directory_where_an_artifact_file_should_be_is_left_alone(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    artifact = _expired_artifact(repository, "odd-job")
    target = Path(artifact.path)
    target.unlink()
    target.mkdir()
    (target / "keep.txt").write_bytes(b"keep")
    healthy = _expired_artifact(repository, "healthy-job", name="healthy.txt")

    result = _cleanup(repository)

    assert (result.artifacts, result.skipped) == (1, 1)
    assert (target / "keep.txt").read_bytes() == b"keep"
    assert repository.get_artifact(healthy.artifact_id) is None


def test_a_row_that_cannot_be_finished_yet_is_deferred_and_retried(tmp_path: Path) -> None:
    """Both the source and its quarantine exist: ambiguous, so nothing moves."""
    repository = _repository(tmp_path)
    stuck = _expired_artifact(repository, "stuck-job", name="stuck.txt")
    quarantine = repository.quarantine_root / "stuck.artifact"
    quarantine.write_bytes(b"leftover")
    _tombstone(repository, stuck, path=Path(stuck.path), quarantine=quarantine)
    healthy = _expired_artifact(repository, "healthy-job", name="healthy.txt")

    first = _cleanup(repository)

    assert (first.artifacts, first.skipped) == (1, 1)
    assert repository.get_artifact(healthy.artifact_id) is None
    # Neither file was touched, and the row is still there to be retried.
    assert Path(stuck.path).exists()
    assert quarantine.read_bytes() == b"leftover"
    assert repository.get_artifact(stuck.artifact_id) is not None
    with repository.connect() as connection:
        queued_at = connection.execute(
            "SELECT created_at FROM execution_artifact_cleanup WHERE artifact_id = ?",
            (stuck.artifact_id,),
        ).fetchone()[0]
    assert queued_at > _LONG_AGO

    assert _cleanup(repository).skipped == 1  # still blocked; still no exception

    quarantine.unlink()
    third = _cleanup(repository)
    assert (third.artifacts, third.skipped) == (1, 0)
    assert not Path(stuck.path).exists()
    assert repository.get_artifact(stuck.artifact_id) is None
    assert _tombstone_count(repository) == 0


def test_skipped_rows_do_not_use_up_the_batch_for_fresh_expiries(tmp_path: Path) -> None:
    """A batch of one, spent on a stuck tombstone, must still reach a new expiry."""
    repository = _repository(tmp_path)
    stuck = _expired_artifact(repository, "stuck-job", name="stuck.txt")
    quarantine = repository.quarantine_root / "stuck.artifact"
    quarantine.write_bytes(b"leftover")
    _tombstone(repository, stuck, path=Path(stuck.path), quarantine=quarantine)
    fresh = _expired_artifact(repository, "fresh-job", name="fresh.txt")

    result = _cleanup(repository, limit=1)

    assert (result.artifacts, result.skipped) == (1, 1)
    assert repository.get_artifact(fresh.artifact_id) is None
    assert repository.get_artifact(stuck.artifact_id) is not None


def test_a_permanently_blocked_row_does_not_starve_the_rows_behind_it(tmp_path: Path) -> None:
    """With a batch of one, a stuck head row would be retried forever."""
    repository = _repository(tmp_path)
    stuck = _expired_artifact(repository, "stuck-job", name="stuck.txt")
    quarantine = repository.quarantine_root / "stuck.artifact"
    quarantine.write_bytes(b"leftover")
    _tombstone(repository, stuck, path=Path(stuck.path), quarantine=quarantine)
    waiting = _expired_artifact(repository, "waiting-job", name="waiting.txt")
    _tombstone(
        repository,
        waiting,
        path=Path(waiting.path),
        quarantine=repository.quarantine_root / "waiting.artifact",
        created_at="2001-01-01T00:00:00+00:00",
    )

    first = _cleanup(repository, limit=1)
    assert (first.artifacts, first.skipped) == (0, 1)
    second = _cleanup(repository, limit=1)

    assert (second.artifacts, second.skipped) == (1, 0)
    assert repository.get_artifact(waiting.artifact_id) is None
    assert repository.get_artifact(stuck.artifact_id) is not None
