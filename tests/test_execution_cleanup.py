"""Retention cleanup scheduling and restart/overlap safeguards."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.execution.cleanup import ExecutionCleanupSupervisor
from cortex_backend.execution.repository import (
    ExecutionRepository,
    ExecutionRepositoryError,
)


def _repository(tmp_path):
    return ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")


def _terminal_job(repository, job_id: str, *, profile: str = "fake.v1"):
    job, _ = repository.create_job(
        job_id=job_id,
        owner=repository.installation_principal_id,
        request_id=f"request-{job_id}",
        profile=profile,
        payload={},
    )
    return repository.transition(
        job.job_id,
        status="succeeded",
        event="completed",
        phase="completed",
        data={"ok": True},
    )


def test_safe_cleanup_keeps_fresh_retained_artifact_and_removes_expired_rows(tmp_path):
    repository = _repository(tmp_path)
    fresh = _terminal_job(repository, "fresh")
    retained = repository.publish_artifact(
        fresh.job_id,
        name="fresh.txt",
        content=b"retain",
        mime_type="text/plain",
        retention_seconds=3600,
    )
    assert repository.cleanup_expired(
        now=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(),
        terminal_job_retention_seconds=0,
    ).rows == 0
    assert repository.get_artifact(retained.artifact_id) is not None

    expired = _terminal_job(repository, "expired")
    artifact = repository.publish_artifact(
        expired.job_id,
        name="expired.txt",
        content=b"remove",
        mime_type="text/plain",
        retention_seconds=1,
    )
    result = repository.cleanup_expired(
        now=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(),
        terminal_job_retention_seconds=0,
        limit=10,
    )
    assert result.artifacts == 1
    assert result.jobs == 1
    assert result.events == 2
    assert repository.get_artifact(artifact.artifact_id) is None
    assert repository.get_job(expired.job_id) is None
    assert not Path(artifact.path).exists()


def test_purge_expired_keeps_recent_terminal_job_without_artifact(tmp_path):
    repository = _repository(tmp_path)
    job = _terminal_job(repository, "recent-no-artifact")

    assert repository.purge_expired(
        now=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    ) == 0
    assert repository.get_job(job.job_id) is not None


def test_cleanup_resumes_quarantine_tombstone_after_restart(tmp_path):
    repository = _repository(tmp_path)
    job = _terminal_job(repository, "restart-cleanup")
    artifact = repository.publish_artifact(
        job.job_id,
        name="restart.txt",
        content=b"remove after restart",
        mime_type="text/plain",
        retention_seconds=1,
    )
    quarantine = repository.quarantine_root / f"{artifact.artifact_id}-restart.artifact"
    with repository.connect() as connection:
        connection.execute(
            """
            INSERT INTO execution_artifact_cleanup
                (artifact_id, path, quarantine_path, state, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (artifact.artifact_id, artifact.path, str(quarantine), repository._now()),
        )
    Path(artifact.path).replace(quarantine)

    restarted = ExecutionRepository(
        tmp_path / "execution.sqlite", tmp_path / "artifacts"
    )
    result = restarted.cleanup_expired(
        now=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    )

    assert result.artifacts == 1
    assert restarted.get_artifact(artifact.artifact_id) is None
    assert not quarantine.exists()
    with restarted.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM execution_artifact_cleanup WHERE artifact_id = ?",
            (artifact.artifact_id,),
        ).fetchone() is None


def test_cleanup_retains_tombstone_when_database_finalize_fails(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    job = _terminal_job(repository, "db-retry-cleanup")
    artifact = repository.publish_artifact(
        job.job_id,
        name="db-retry.txt",
        content=b"retain until durable finalize",
        mime_type="text/plain",
        retention_seconds=1,
    )
    quarantine = repository.quarantine_root / f"{artifact.artifact_id}-db-retry.artifact"
    with repository.connect() as connection:
        connection.execute(
            """
            INSERT INTO execution_artifact_cleanup
                (artifact_id, path, quarantine_path, state, created_at)
            VALUES (?, ?, ?, 'quarantined', ?)
            """,
            (artifact.artifact_id, artifact.path, str(quarantine), repository._now()),
        )
    Path(artifact.path).replace(quarantine)

    original_connect = repository.connect
    calls = 0

    @contextmanager
    def fail_finalize_connection():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ExecutionRepositoryError("synthetic database failure")
        with original_connect() as connection:
            yield connection

    monkeypatch.setattr(repository, "connect", fail_finalize_connection)
    with pytest.raises(ExecutionRepositoryError):
        repository.cleanup_expired(
            now=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
        )
    assert repository.get_artifact(artifact.artifact_id) is not None
    assert quarantine.exists()

    monkeypatch.setattr(repository, "connect", original_connect)
    result = repository.cleanup_expired(
        now=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    )
    assert result.artifacts == 1
    assert repository.get_artifact(artifact.artifact_id) is None
    assert not quarantine.exists()


def test_cleanup_supervisor_handles_failure_and_releases_lease(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    supervisor = ExecutionCleanupSupervisor(repository)
    monkeypatch.setattr(
        repository,
        "cleanup_expired",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic")),
    )
    assert supervisor.run_once() is False
    assert supervisor.metrics.failures == 1
    # A failed pass must not strand the lease or prevent a later retry.
    repository.claim_cleanup_lease(lease_owner="retry", ttl_seconds=1)
    repository.release_cleanup_lease(lease_owner="retry")


def test_cleanup_supervisor_skips_live_peer_and_local_overlap(tmp_path):
    repository = _repository(tmp_path)
    first = ExecutionCleanupSupervisor(repository)
    second = ExecutionCleanupSupervisor(repository)
    repository.claim_cleanup_lease(lease_owner="live-peer", ttl_seconds=30)
    assert first.run_once() is False
    assert first.metrics.lease_conflicts == 1
    repository.release_cleanup_lease(lease_owner="live-peer")

    assert first._run_lock.acquire()
    try:
        assert first.run_once() is False
    finally:
        first._run_lock.release()
    assert first.metrics.skipped_overlap == 1
    assert second.run_once() is True


def test_cleanup_supervisor_can_restart_after_clean_stop(tmp_path):
    supervisor = ExecutionCleanupSupervisor(_repository(tmp_path), interval_seconds=0.01)
    supervisor.start()
    time.sleep(0.04)
    supervisor.stop(timeout=1)
    first_runs = supervisor.metrics.runs
    supervisor.start()
    time.sleep(0.04)
    supervisor.stop(timeout=1)
    assert first_runs > 0
    assert supervisor.metrics.runs > first_runs
    assert not supervisor.running


def test_app_lifespan_starts_and_stops_cleanup_supervisor(tmp_path):
    supervisor = ExecutionCleanupSupervisor(_repository(tmp_path), interval_seconds=60)
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=("testserver", "127.0.0.1", "localhost", "::1"),
        cleanup_supervisor=supervisor,
    )
    with TestClient(app):
        assert supervisor.running
    assert not supervisor.running
    assert supervisor.metrics.runs >= 1


def test_app_does_not_auto_wire_cleanup_for_protocol_compatible_fake_repository():
    fake_repository = SimpleNamespace(installation_principal_id="a" * 64)
    fake_coordinator = SimpleNamespace(repository=fake_repository)
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=("testserver",),
        execution_coordinator=fake_coordinator,
    )
    assert app.state.cleanup_supervisor is None


def test_cleanup_renews_lease_during_slow_pass(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    supervisor = ExecutionCleanupSupervisor(
        repository, lease_seconds=1.5, interval_seconds=60
    )
    renewals = []
    cleanup_started = Event()
    renewal_observed_during_pass = Event()
    original_renew = repository.renew_cleanup_lease

    def observed_renewal(**kwargs):
        renewed = original_renew(**kwargs)
        if renewed:
            renewals.append(True)
            if cleanup_started.is_set():
                renewal_observed_during_pass.set()
        return renewed

    monkeypatch.setattr(repository, "renew_cleanup_lease", observed_renewal)
    release = Event()
    original_cleanup = repository.cleanup_expired

    def slow_cleanup(**kwargs):
        cleanup_started.set()
        assert release.wait(timeout=2.5)
        return original_cleanup(**kwargs)

    monkeypatch.setattr(repository, "cleanup_expired", slow_cleanup)
    outcome = []
    runner = Thread(target=lambda: outcome.append(supervisor.run_once()))
    runner.start()
    assert cleanup_started.wait(timeout=1)
    assert renewal_observed_during_pass.wait(timeout=2.5)
    release.set()
    runner.join(timeout=3)
    assert not runner.is_alive()
    assert outcome == [True]
    assert renewals
