"""Retention cleanup scheduling and restart/overlap safeguards."""

from __future__ import annotations

from contextlib import contextmanager
import logging
from datetime import datetime, timedelta, timezone
import threading
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from support import wait_until

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies
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


def test_cleanup_keeps_the_quarantine_root_it_needs_for_the_next_artifact(tmp_path):
    """The empty-directory sweep must not sweep the shared quarantine root.

    Every tombstone is a file directly inside a single ``.quarantine``
    directory, created once when the repository is opened. Unlinking the last
    tombstone leaves it empty by design, so including it in the sweep removed
    it on the very first expiry -- and the next artifact's
    ``path.replace(quarantine)`` then had no parent to move into, raising
    ``ExecutionRepositoryError`` and wedging retention for good. Nothing
    downstream retries: the supervisor records the failure and moves on, so
    expired artifacts simply accumulate on disk from then on.
    """
    repository = _repository(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()

    for index in range(2):
        job = _terminal_job(repository, f"expired-{index}")
        artifact = repository.publish_artifact(
            job.job_id,
            name=f"expired-{index}.txt",
            content=b"remove",
            mime_type="text/plain",
            retention_seconds=1,
        )

        result = repository.cleanup_expired(
            now=future, terminal_job_retention_seconds=0, limit=10
        )

        assert result.artifacts == 1
        assert repository.get_artifact(artifact.artifact_id) is None
        assert not Path(artifact.path).exists()
        # The per-job directory is still swept -- that part is the point.
        assert not Path(artifact.path).parent.exists()
        assert repository.quarantine_root.is_dir()


def test_cleanup_keeps_a_recent_terminal_job_without_an_artifact(tmp_path):
    repository = _repository(tmp_path)
    job = _terminal_job(repository, "recent-no-artifact")

    assert repository.cleanup_expired(
        now=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    ).artifacts == 0
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
    # Read the row directly: get_artifact reads the real clock and reports an artifact whose
    # one-second retention has lapsed as absent, which would make this depend on the test
    # finishing within a second of publishing.
    with original_connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM execution_artifacts WHERE artifact_id = ?", (artifact.artifact_id,)
        ).fetchone() is not None
    assert quarantine.exists()

    monkeypatch.setattr(repository, "connect", original_connect)
    result = repository.cleanup_expired(
        now=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    )
    assert result.artifacts == 1
    assert repository.get_artifact(artifact.artifact_id) is None
    assert not quarantine.exists()


def _tombstone_with_a_rejected_source(
    repository, tmp_path, *, state, quarantine_root_path=None, recorded_quarantine_root=None
):
    """A tombstone whose artifact is already in quarantine but whose source path no longer validates.

    The source path points outside the artifact root, as it does after the data
    directory is moved or a job directory is swapped for a link. Returns the
    artifact and the file sitting in quarantine. ``recorded_quarantine_root`` is
    the directory the row names for that file when the file is not there (the
    quarantine directory's old home, after a data directory has moved).
    """
    job = _terminal_job(repository, f"rejected-{state}")
    artifact = repository.publish_artifact(
        job.job_id, name="rejected.txt", content=b"expired bytes", mime_type="text/plain", retention_seconds=1
    )
    quarantine = (quarantine_root_path or repository.quarantine_root) / f"{artifact.artifact_id}-{'0' * 32}.artifact"
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    Path(artifact.path).replace(quarantine)
    elsewhere = tmp_path / "moved-data" / "rejected.txt"
    recorded = (recorded_quarantine_root or quarantine.parent) / quarantine.name
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_artifacts SET expires_at = ? WHERE artifact_id = ?",
            ("2000-01-01T00:00:00+00:00", artifact.artifact_id),
        )
        connection.execute(
            """
            INSERT INTO execution_artifact_cleanup
                (artifact_id, path, quarantine_path, state, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (artifact.artifact_id, str(elsewhere), str(recorded), state, repository._now()),
        )
    return artifact, quarantine


def _tombstone_rows(repository, artifact_id):
    with repository.connect() as connection:
        cleanup = connection.execute(
            "SELECT state FROM execution_artifact_cleanup WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        artifact = connection.execute(
            "SELECT 1 FROM execution_artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
    return cleanup, artifact


@pytest.mark.parametrize("state", ["pending", "quarantined", "finalized"])
def test_a_rejected_tombstone_does_not_orphan_the_file_already_in_quarantine(tmp_path, state):
    """Dropping a rejected row must not strand the file it had already moved.

    The quarantine file is inside our own quarantine root and was put there by
    the cleanup itself; only the original location stopped validating. Deleting
    the row and its tombstone left that file with nothing that would ever look at
    it again.
    """
    repository = _repository(tmp_path)
    artifact, quarantine = _tombstone_with_a_rejected_source(repository, tmp_path, state=state)

    result = repository.cleanup_expired(now="9999-01-01T00:00:00+00:00", terminal_job_retention_seconds=10**9)

    assert not quarantine.exists()
    assert result.skipped == 1
    assert _tombstone_rows(repository, artifact.artifact_id) == (None, None)


def test_a_rejected_tombstone_is_kept_while_its_quarantine_file_cannot_be_removed(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    artifact, quarantine = _tombstone_with_a_rejected_source(repository, tmp_path, state="quarantined")
    original_unlink = Path.unlink

    def locked_unlink(self, *args, **kwargs):
        if self.name == quarantine.name:
            raise PermissionError("held open by a scanner")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    first = repository.cleanup_expired(now="9999-01-01T00:00:00+00:00", terminal_job_retention_seconds=10**9)

    assert first.skipped == 1
    assert quarantine.exists()
    cleanup_row, artifact_row = _tombstone_rows(repository, artifact.artifact_id)
    assert cleanup_row is not None and artifact_row is not None  # kept, so a later pass retries

    monkeypatch.setattr(Path, "unlink", original_unlink)
    second = repository.cleanup_expired(now="9999-01-01T00:00:00+00:00", terminal_job_retention_seconds=10**9)

    assert second.skipped == 1
    assert not quarantine.exists()
    assert _tombstone_rows(repository, artifact.artifact_id) == (None, None)


def test_a_rejected_tombstone_never_touches_a_file_outside_the_quarantine_root(tmp_path):
    repository = _repository(tmp_path)
    outside_root = tmp_path / "somewhere-else"
    artifact, stray = _tombstone_with_a_rejected_source(
        repository, tmp_path, state="quarantined", quarantine_root_path=outside_root
    )

    result = repository.cleanup_expired(now="9999-01-01T00:00:00+00:00", terminal_job_retention_seconds=10**9)

    assert result.skipped == 1
    assert stray.read_bytes() == b"expired bytes"  # not ours to delete
    assert _tombstone_rows(repository, artifact.artifact_id) == (None, None)


@pytest.mark.parametrize("state", ["pending", "quarantined", "finalized"])
def test_a_rejected_tombstone_from_a_moved_data_directory_reclaims_the_file_by_name(tmp_path, state):
    """After a data directory moves, the row names a path that is no longer ours.

    The whole ``.quarantine`` directory moved with it, so the file is in the
    current quarantine root under the name the row recorded. Matching by that
    name, inside the current root, is what keeps the file from being stranded by
    the row that alone knew about it.
    """
    repository = _repository(tmp_path)
    old_home = tmp_path / "old-data" / "artifacts" / ".quarantine"
    artifact, quarantine = _tombstone_with_a_rejected_source(
        repository, tmp_path, state=state, recorded_quarantine_root=old_home
    )
    assert not (old_home / quarantine.name).exists() and quarantine.exists()

    result = repository.cleanup_expired(now="9999-01-01T00:00:00+00:00", terminal_job_retention_seconds=10**9)

    assert not quarantine.exists()
    assert result.skipped == 1
    assert _tombstone_rows(repository, artifact.artifact_id) == (None, None)


def test_a_rejected_tombstone_never_matches_by_name_a_file_made_for_another_artifact(tmp_path):
    """The by-name match is confined to this artifact's own tombstone in our root."""
    repository = _repository(tmp_path)
    artifact, _ = _tombstone_with_a_rejected_source(repository, tmp_path, state="quarantined")
    bystander = repository.quarantine_root / f"{'b' * 32}-{'c' * 32}.artifact"
    bystander.write_bytes(b"belongs to another tombstone")
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_artifact_cleanup SET quarantine_path = ? WHERE artifact_id = ?",
            # An old home naming a file that is not this artifact's.
            (str(tmp_path / "old-data" / ".quarantine" / bystander.name), artifact.artifact_id),
        )

    repository.cleanup_expired(now="9999-01-01T00:00:00+00:00", terminal_job_retention_seconds=10**9)

    assert bystander.read_bytes() == b"belongs to another tombstone"
    assert _tombstone_rows(repository, artifact.artifact_id) == (None, None)


def test_a_rejected_tombstone_leaves_a_directory_in_quarantine_alone(tmp_path):
    repository = _repository(tmp_path)
    artifact, quarantine = _tombstone_with_a_rejected_source(repository, tmp_path, state="quarantined")
    quarantine.unlink()
    quarantine.mkdir()
    (quarantine / "inside.txt").write_bytes(b"not a file we quarantined")

    repository.cleanup_expired(now="9999-01-01T00:00:00+00:00", terminal_job_retention_seconds=10**9)

    assert (quarantine / "inside.txt").read_bytes() == b"not a file we quarantined"
    assert _tombstone_rows(repository, artifact.artifact_id) == (None, None)


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


def test_cleanup_supervisor_reports_rows_it_could_not_reclaim_and_still_succeeds(
    tmp_path, caplog
):
    """A poisoned row is a visible metric, not a failed pass that hides the store growing."""
    repository = _repository(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not ours")
    poisoned = _terminal_job(repository, "poisoned")
    artifact = repository.publish_artifact(
        poisoned.job_id, name="poisoned.txt", content=b"x", mime_type="text/plain"
    )
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_artifacts SET path = ?, expires_at = ? WHERE artifact_id = ?",
            (str(outside), "2000-01-01T00:00:00+00:00", artifact.artifact_id),
        )
    supervisor = ExecutionCleanupSupervisor(repository, terminal_job_retention_seconds=0)

    with caplog.at_level(logging.WARNING, logger="cortex.execution.cleanup"):
        assert supervisor.run_once() is True

    assert supervisor.metrics.failures == 0
    assert supervisor.metrics.successes == 1
    assert supervisor.metrics.artifacts_skipped == 1
    assert outside.read_bytes() == b"not ours"
    assert "could not safely reclaim" in caplog.text
    assert str(tmp_path) not in caplog.text


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
    # Wait for the pass itself, never for a stretch of wall-clock time: a fixed
    # sleep after start() assumes the new thread got scheduled inside it, and
    # on a loaded machine it can have run no pass at all before stop() is
    # called (the loop then never enters), which read as "did not restart".
    supervisor = ExecutionCleanupSupervisor(_repository(tmp_path), interval_seconds=0.01)
    supervisor.start()
    try:
        wait_until(lambda: supervisor.metrics.runs >= 1, describe="a first cleanup pass")
    finally:
        supervisor.stop()
    first_runs = supervisor.metrics.runs
    assert first_runs > 0
    assert not supervisor.running

    supervisor.start()
    try:
        wait_until(
            lambda: supervisor.metrics.runs > first_runs,
            describe=lambda: f"a cleanup pass after the restart (runs={supervisor.metrics.runs})",
        )
    finally:
        supervisor.stop()
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


def _live_threads(name: str) -> list[Thread]:
    return [thread for thread in threading.enumerate() if thread.name == name and thread.is_alive()]


def test_stop_that_times_out_says_so_and_still_releases_the_lease(tmp_path, monkeypatch, caplog):
    """A worker still inside a pass when the timeout runs out must not vanish silently.

    stop() used to do everything after the join -- stop the lease renewal, release
    the lease, log -- only when the worker had already exited. With a worker still
    running, nothing was logged and the renewal thread went on extending the
    installation-wide cleanup lease for the life of the process, so the next
    supervisor saw a conflict and retention stopped.
    """
    repository = _repository(tmp_path)
    supervisor = ExecutionCleanupSupervisor(repository, interval_seconds=60, lease_seconds=30)
    pass_started = Event()
    finish_pass = Event()
    original_cleanup = repository.cleanup_expired

    def slow_cleanup(**kwargs):
        pass_started.set()
        assert finish_pass.wait(timeout=10)
        return original_cleanup(**kwargs)

    monkeypatch.setattr(repository, "cleanup_expired", slow_cleanup)
    supervisor.start()
    try:
        assert pass_started.wait(timeout=5)
        assert _live_threads("cortex-execution-cleanup-lease")

        with caplog.at_level(logging.WARNING, logger="cortex.execution.cleanup"):
            supervisor.stop(timeout=0.05)

        # The worker is still alive, and the handle says so, so start() cannot build a second.
        assert supervisor.running
        assert any("still running" in record.message for record in caplog.records), caplog.text
        assert str(tmp_path) not in caplog.text
        # ...but nothing keeps extending the lease on its behalf.
        assert not _live_threads("cortex-execution-cleanup-lease")
        repository.claim_cleanup_lease(lease_owner="next-supervisor", ttl_seconds=5)
        repository.release_cleanup_lease(lease_owner="next-supervisor")
    finally:
        finish_pass.set()
        supervisor.stop(timeout=5)
    wait_until(lambda: not supervisor.running, describe="the cleanup worker to exit")
    assert not _live_threads("cortex-execution-cleanup")


def test_stop_that_finds_the_worker_gone_is_quiet_and_releases_the_lease(tmp_path, caplog):
    repository = _repository(tmp_path)
    supervisor = ExecutionCleanupSupervisor(repository, interval_seconds=60)
    supervisor.start()
    wait_until(lambda: supervisor.metrics.runs >= 1, describe="the first cleanup pass")

    with caplog.at_level(logging.WARNING, logger="cortex.execution.cleanup"):
        supervisor.stop(timeout=5)

    assert not supervisor.running
    assert caplog.records == []
    repository.claim_cleanup_lease(lease_owner="next-supervisor", ttl_seconds=5)
    repository.release_cleanup_lease(lease_owner="next-supervisor")


def test_stop_reports_a_lease_renewal_that_will_not_stop(tmp_path, monkeypatch, caplog):
    repository = _repository(tmp_path)
    supervisor = ExecutionCleanupSupervisor(repository, interval_seconds=60)
    monkeypatch.setattr(supervisor, "_stop_lease_renewal", lambda: False)

    with caplog.at_level(logging.WARNING, logger="cortex.execution.cleanup"):
        supervisor.stop(timeout=0)

    assert any("renewal did not stop" in record.message for record in caplog.records), caplog.text


def test_a_worker_that_survives_the_kill_ladder_is_reported(caplog):
    """A leaked sandboxed child must not look like a worker that said nothing.

    Every teardown step is best-effort, so failures used to be swallowed
    entirely. A path that always fails leaks a child holding its sandbox
    resources on every run, and with nothing recorded that is
    indistinguishable from a worker which simply never produced a result.
    """
    from cortex_backend.execution import local_process

    class _Unkillable:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            del timeout

        def terminate(self):
            raise OSError("terminate refused")

        def kill(self):
            raise OSError("kill refused")

    with caplog.at_level(logging.WARNING, logger="cortex.execution.local_process"):
        local_process._stop_process(_Unkillable(), grace_seconds=0.0)

    assert any("could not be killed" in record.message for record in caplog.records)


def test_ordinary_teardown_stays_quiet(caplog):
    """A worker that stops on request must not log anything at warning level."""
    from cortex_backend.execution import local_process

    class _Cooperative:
        def __init__(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            del timeout
            self.alive = False

        def terminate(self):  # pragma: no cover - never reached
            raise AssertionError("terminate should not be needed")

    with caplog.at_level(logging.WARNING, logger="cortex.execution.local_process"):
        local_process._stop_process(_Cooperative(), grace_seconds=0.0)

    assert caplog.records == []
