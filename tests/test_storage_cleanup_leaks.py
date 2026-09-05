"""Nothing the app does on a normal day should leave files behind forever.

Both of these accumulate silently in a user's data directory: one file pair
per launch, and one directory per artifact ever created.
"""

from __future__ import annotations

from pathlib import Path

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
