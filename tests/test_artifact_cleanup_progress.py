"""Retention passes make progress and never orphan a quarantined file (#619, #620)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cortex_backend.execution.repository import ArtifactCleanupBlocked, ExecutionRepository

FUTURE = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()


def _repository(tmp_path: Path) -> ExecutionRepository:
    return ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")


def _expiring_artifact(repository: ExecutionRepository, name: str, retention_seconds: int):
    repository.create_job(
        job_id=f"job-{name}",
        owner=repository.installation_principal_id,
        request_id=f"request-{name}",
        profile="scratch.auto.v1",
        payload={},
    )
    return repository.publish_artifact(
        f"job-{name}",
        name=f"{name}.txt",
        content=b"synthetic",
        mime_type="text/plain",
        retention_seconds=retention_seconds,
    )


def _artifact_ids(repository: ExecutionRepository) -> set[str]:
    with repository.connect() as connection:
        rows = connection.execute("SELECT artifact_id FROM execution_artifacts").fetchall()
    return {str(row["artifact_id"]) for row in rows}


def test_a_blocked_oldest_artifact_does_not_starve_the_ones_behind_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    blocked = _expiring_artifact(repository, "blocked", retention_seconds=60)
    reclaimable = _expiring_artifact(repository, "reclaimable", retention_seconds=120)
    real_validate = repository._validated_cleanup_path

    def validate(path: Path) -> Path:
        if path.name == Path(blocked.path).name:
            raise ArtifactCleanupBlocked("Artifact path is unavailable.")
        return real_validate(path)

    monkeypatch.setattr(repository, "_validated_cleanup_path", validate)

    for _ in range(3):
        repository.cleanup_expired(now=FUTURE, limit=1)

    remaining = _artifact_ids(repository)
    assert reclaimable.artifact_id not in remaining
    assert blocked.artifact_id in remaining  # kept for a later retry, not discarded


def test_a_pass_that_loses_the_tombstone_race_finishes_the_winners_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    artifact = _expiring_artifact(repository, "raced", retention_seconds=60)
    real_record = repository._record_artifact_cleanup
    winner_quarantine = repository.quarantine_root / f"{artifact.artifact_id}-other-pass.artifact"
    raced: list[str] = []

    def another_pass_records_first(artifact_id, path, quarantine):
        if not raced:
            raced.append(artifact_id)
            # The other pass recorded its own quarantine name, moved the file
            # there, and was interrupted before advancing its state.
            winner = winner_quarantine if isinstance(quarantine, Path) else repository._stored_text(winner_quarantine)
            real_record(artifact_id, path, winner)
            source = path if isinstance(path, Path) else repository._stored_path(path)
            source.replace(winner_quarantine)
        return real_record(artifact_id, path, quarantine)

    monkeypatch.setattr(repository, "_record_artifact_cleanup", another_pass_records_first)

    repository.cleanup_expired(now=FUTURE, limit=10)

    assert raced == [artifact.artifact_id]
    assert artifact.artifact_id not in _artifact_ids(repository)
    assert not winner_quarantine.exists(), "the other pass's quarantined file was orphaned"
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM execution_artifact_cleanup").fetchone()[0] == 0
