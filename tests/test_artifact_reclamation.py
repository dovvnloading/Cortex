"""Artifact job directories, temporary files and staging directories are reclaimed.

A hard kill, a full disk or a failed commit used to leave debris under the
artifact root that nothing ever removed: an empty directory per job, a
``.tmp-*`` file, a recipe staging directory. The sweep removes what a crash
leaves, and only what nothing can still need.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from threading import Event, Thread
import time

import pytest

from cortex_backend.execution import repository as repository_module
from cortex_backend.execution.cleanup import ExecutionCleanupSupervisor
from cortex_backend.execution.repository import ExecutionRepository, ExecutionRepositoryError

_LONG_AGO = time.time() - 7_200


def _repository(tmp_path: Path) -> ExecutionRepository:
    return ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")


def _job(repository: ExecutionRepository, job_id: str, *, finished: bool = False) -> str:
    repository.create_job(
        job_id=job_id,
        owner=repository.installation_principal_id,
        request_id=f"request-{job_id}",
        profile="scratch.auto.v1",
        payload={},
    )
    if finished:
        repository.transition(job_id, status="succeeded", event="completed")
    return job_id


def _publish(repository: ExecutionRepository, job_id: str, name: str = "out.txt"):
    return repository.publish_artifact(job_id, name=name, content=b"synthetic", mime_type="text/plain")


def _age(path: Path, when: float = _LONG_AGO) -> None:
    os.utime(path, (when, when))


def _job_directory(repository: ExecutionRepository, job_id: str) -> Path:
    return repository.artifact_root / job_id


# -- delete_artifact: row first, then file, then the directory --------------------------


class _FailingCommit(sqlite3.Connection):
    armed = False

    def commit(self):
        if _FailingCommit.armed:
            raise sqlite3.OperationalError("disk I/O error")
        super().commit()


def test_a_delete_that_fails_to_commit_leaves_the_row_and_its_file_together(tmp_path, monkeypatch):
    """The file used to be unlinked before the row's delete committed.

    A commit that then failed left a row pointing at nothing: the artifact
    could no longer be read and nothing would ever say why.
    """

    repository = _repository(tmp_path)
    artifact = _publish(repository, _job(repository, "job-1"))
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=_FailingCommit, **k)
    )

    _FailingCommit.armed = True
    try:
        with pytest.raises(ExecutionRepositoryError):
            repository.delete_artifact(artifact.artifact_id)
    finally:
        _FailingCommit.armed = False

    assert Path(artifact.path).is_file()
    assert repository.read_artifact(artifact.artifact_id) == b"synthetic"


def test_deleting_the_last_artifact_removes_the_job_directory_but_not_a_shared_one(tmp_path):
    repository = _repository(tmp_path)
    job_id = _job(repository, "job-1")
    first = _publish(repository, job_id, "one.txt")
    second = _publish(repository, job_id, "two.txt")

    repository.delete_artifact(first.artifact_id)
    assert _job_directory(repository, job_id).is_dir()
    repository.delete_artifact(second.artifact_id)
    assert not _job_directory(repository, job_id).exists()
    assert repository.get_artifact(first.artifact_id) is None


def test_a_file_that_cannot_be_removed_leaves_no_row_and_the_sweep_reclaims_it(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    job_id = _job(repository, "job-1")
    artifact = _publish(repository, job_id)
    stuck = Path(artifact.path)
    real_unlink = Path.unlink

    def locked(self, *args, **kwargs):
        if self == stuck:
            raise PermissionError("held open by a scanner")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(ExecutionRepositoryError, match="cleanup failed"):
        repository.delete_artifact(artifact.artifact_id)
    monkeypatch.setattr(Path, "unlink", real_unlink)

    assert repository.get_artifact(artifact.artifact_id) is None  # the row is gone
    assert stuck.is_file()  # the file is an orphan, not a dangling row
    _age(stuck)
    assert repository.sweep_artifact_root() == 2  # the file, then its now-empty directory
    assert not _job_directory(repository, job_id).exists()


# -- publish_artifact leaves nothing behind when it fails -----------------------------------


def test_a_publish_that_fails_leaves_no_job_directory_and_no_temporary_file(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    job_id = _job(repository, "job-1")

    def full_disk(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", full_disk)
    with pytest.raises(OSError):
        _publish(repository, job_id)

    assert not _job_directory(repository, job_id).exists()


def test_a_publish_whose_row_cannot_be_written_leaves_no_file_and_no_directory(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    job_id = _job(repository, "job-1")
    real_connect = sqlite3.connect

    class RefusesArtifactRows(sqlite3.Connection):
        def execute(self, sql, *args):
            if "INSERT INTO execution_artifacts" in sql:
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *args)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=RefusesArtifactRows, **k)
    )
    with pytest.raises(ExecutionRepositoryError):
        _publish(repository, job_id)

    assert not _job_directory(repository, job_id).exists()


def test_a_failed_publish_keeps_the_directory_other_artifacts_still_need(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    job_id = _job(repository, "job-1")
    kept = _publish(repository, job_id, "kept.txt")

    def full_disk(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", full_disk)
    with pytest.raises(OSError):
        _publish(repository, job_id, "lost.txt")
    monkeypatch.undo()

    assert repository.read_artifact(kept.artifact_id) == b"synthetic"


# -- the sweep --------------------------------------------------------------------------------


def test_the_sweep_removes_stranded_temporary_files_only_once_they_are_old(tmp_path):
    repository = _repository(tmp_path)
    job_id = _job(repository, "job-1")
    kept = _publish(repository, job_id)
    directory = _job_directory(repository, job_id)
    stranded = directory / f".tmp-{'a' * 32}"
    in_flight = directory / f".tmp-{'b' * 32}"
    stranded.write_bytes(b"half a write")
    in_flight.write_bytes(b"half a write")
    _age(stranded)

    assert repository.sweep_artifact_root() == 1

    assert not stranded.exists()
    assert in_flight.exists(), "a file this new may belong to a publish in flight"
    assert repository.read_artifact(kept.artifact_id) == b"synthetic"


def test_the_sweep_removes_an_unreferenced_artifact_file_but_never_a_referenced_one(tmp_path):
    repository = _repository(tmp_path)
    job_id = _job(repository, "job-1")
    live = _publish(repository, job_id)
    directory = _job_directory(repository, job_id)
    orphan = directory / f"{'c' * 32}-out.txt"
    fresh_orphan = directory / f"{'d' * 32}-out.txt"
    bystander = directory / "notes.txt"
    for path in (orphan, fresh_orphan, bystander):
        path.write_bytes(b"synthetic")
    for path in (orphan, Path(live.path), bystander):
        _age(path)

    assert repository.sweep_artifact_root() == 1

    assert not orphan.exists()
    assert fresh_orphan.exists()
    assert bystander.exists(), "a name the repository never gives a file is not the sweep's to remove"
    assert repository.read_artifact(live.artifact_id) == b"synthetic"


def test_the_sweep_matches_artifacts_by_id_so_a_moved_data_directory_loses_nothing(tmp_path):
    """A row keeps the absolute path it was written with; a moved data directory changes it."""

    repository = _repository(tmp_path)
    artifact = _publish(repository, _job(repository, "job-1"))
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_artifacts SET path = ? WHERE artifact_id = ?",
            (str(tmp_path / "old-home" / "artifacts" / "job-1" / Path(artifact.path).name), artifact.artifact_id),
        )
    _age(Path(artifact.path))

    assert repository.sweep_artifact_root() == 0

    assert Path(artifact.path).is_file()


def test_the_sweep_removes_an_empty_job_directory_but_not_one_with_a_row_or_a_reserved_name(tmp_path):
    repository = _repository(tmp_path)
    empty = repository.artifact_root / "job-empty"
    empty.mkdir()
    with_row = _job(repository, "job-with-row")
    kept = _publish(repository, with_row)
    Path(kept.path).unlink()  # a row whose file is gone: retention's business, not the sweep's
    reserved_workspaces = repository.artifact_root / ".code_workspaces"
    reserved_workspaces.mkdir()
    unknown = repository.artifact_root / ".something-else"
    unknown.mkdir()

    assert repository.sweep_artifact_root() == 1

    assert not empty.exists()
    assert _job_directory(repository, with_row).is_dir()
    assert repository.quarantine_root.is_dir()
    assert reserved_workspaces.is_dir()
    assert unknown.is_dir()


def test_the_sweep_removes_the_empty_directory_an_earlier_build_left_but_not_a_used_one(tmp_path):
    repository = _repository(tmp_path)
    stray = repository.artifact_root / ".artifact_quarantine"
    stray.mkdir()

    assert repository.sweep_artifact_root() == 1
    assert not stray.exists()

    stray.mkdir()
    (stray / "held").write_bytes(b"x")
    assert repository.sweep_artifact_root() == 0
    assert (stray / "held").exists()


def test_the_sweep_removes_recipe_staging_for_a_finished_or_unknown_job_but_not_a_live_one(tmp_path):
    repository = _repository(tmp_path)
    _job(repository, "job-live")
    _job(repository, "job-done", finished=True)
    names = {
        "live": ".recipe-job-live-abcd1234",
        "done": ".recipe-job-done-abcd1234",
        "unknown": ".recipe-job-gone-abcd1234",
        "odd": ".recipe-job-live-abc",  # not the name the coordinator gives its staging
    }
    for name in names.values():
        staging = repository.artifact_root / name
        staging.mkdir()
        (staging / "output").write_bytes(b"synthetic")
        (staging / ".quarantine").mkdir()

    assert repository.sweep_artifact_root() == 2

    assert (repository.artifact_root / names["live"]).is_dir()
    assert not (repository.artifact_root / names["done"]).exists()
    assert not (repository.artifact_root / names["unknown"]).exists()
    assert (repository.artifact_root / names["odd"]).is_dir()


def test_the_sweep_never_follows_or_removes_a_link(tmp_path):
    repository = _repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / f".tmp-{'e' * 32}").write_bytes(b"not ours")
    _age(outside / f".tmp-{'e' * 32}")
    link = repository.artifact_root / "job-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable here")

    assert repository.sweep_artifact_root() == 0

    assert (outside / f".tmp-{'e' * 32}").exists()
    assert link.is_symlink()


def _make_junction(link: Path, target: Path) -> None:
    """A real directory junction: a reparse point ``lstat`` reports as a plain directory."""

    if os.name != "nt":
        pytest.skip("directory junctions exist only on Windows")
    if not hasattr(Path, "is_junction"):
        pytest.skip("this interpreter cannot recognise a junction (Path.is_junction arrived in 3.12)")
    import _winapi  # type: ignore[import-not-found]

    try:
        _winapi.CreateJunction(str(target), str(link))
    except OSError:
        pytest.skip("directory junctions cannot be created here")


@pytest.mark.parametrize("name", ("job-junction", ".recipe-job-gone-abcd1234"))
def test_the_sweep_never_follows_or_removes_a_junction(tmp_path, name):
    """A symbolic link reads as a link to ``lstat``; a junction reads as a directory.

    Only the reparse-point check stands between the sweep and the files the
    junction leads to, so it needs a real junction to be tested at all.
    """

    repository = _repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / f".tmp-{'e' * 32}").write_bytes(b"not ours")
    _age(outside / f".tmp-{'e' * 32}")
    (outside / "output").write_bytes(b"not ours either")
    junction = repository.artifact_root / name
    _make_junction(junction, outside)
    try:
        assert repository.sweep_artifact_root() == 0

        assert (outside / f".tmp-{'e' * 32}").read_bytes() == b"not ours"
        assert (outside / "output").read_bytes() == b"not ours either"
        assert junction.exists(), "the junction itself was removed"
    finally:
        os.rmdir(junction)  # removes the junction itself and nothing behind it


def test_the_sweep_is_bounded_and_resumes_where_it_stopped(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    directories = []
    for index in range(6):
        directory = repository.artifact_root / f"job-{index}"
        directory.mkdir()
        directories.append(directory)

    # One removal per pass: every call makes progress and none does it all.
    passes = [repository.sweep_artifact_root(limit=1) for _ in range(6)]
    assert passes == [1, 1, 1, 1, 1, 1]
    assert not any(directory.exists() for directory in directories)

    # A tiny look-at budget stops a pass early, and the next one carries on.
    monkeypatch.setattr(repository_module, "_SWEEP_ENTRY_BUDGET", 2)
    for index in range(6):
        (repository.artifact_root / f"again-{index}").mkdir()
    removed = 0
    for _ in range(10):
        removed += repository.sweep_artifact_root(limit=100)
    assert removed == 6
    with pytest.raises(ValueError):
        repository.sweep_artifact_root(limit=0)


def _stale_temporary_files(directory: Path, count: int) -> list[Path]:
    """``count`` stranded ``.tmp-*`` files, old enough for the sweep to remove."""

    made = []
    for index in range(count):
        stale = directory / f".tmp-{index:032x}"
        stale.write_bytes(b"half a write")
        _age(stale)
        made.append(stale)
    return made


@pytest.mark.parametrize("limit", (1, 2, 7, 64, 65, 100))
def test_a_pass_never_removes_more_than_its_limit_even_inside_one_directory(tmp_path, limit):
    """The limit was checked only between entries.

    With ``limit=1`` one directory of 64 stale files lost all 64 of them and
    then itself -- 65 removals -- in a single pass.
    """

    repository = _repository(tmp_path)
    directory = repository.artifact_root / "job-big"
    directory.mkdir()
    _stale_temporary_files(directory, repository_module._SWEEP_CHILD_LIMIT)

    per_pass = []
    for _ in range(80):  # bounded: 65 things to remove
        per_pass.append(repository.sweep_artifact_root(limit=limit))
        if not directory.exists():
            break

    assert max(per_pass) <= limit
    assert sum(per_pass) == repository_module._SWEEP_CHILD_LIMIT + 1  # every file, then the directory
    assert not directory.exists()


def test_an_entry_cut_short_by_the_limit_is_resumed_not_skipped(tmp_path):
    repository = _repository(tmp_path)
    first = repository.artifact_root / "job-a"
    second = repository.artifact_root / "job-b"
    for directory, count in ((first, 3), (second, 1)):
        directory.mkdir()
        _stale_temporary_files(directory, count)

    # Six things to remove, two per pass. A pass that ran out of allowance inside
    # job-a must come back to job-a, not move on and leave the rest of it behind.
    assert repository.sweep_artifact_root(limit=2) == 2
    assert len(list(first.iterdir())) == 1, "the first pass should have stopped inside job-a"
    assert len(list(second.iterdir())) == 1

    assert repository.sweep_artifact_root(limit=2) == 2  # job-a's last file, then job-a itself
    assert not first.exists(), "job-a was not finished before the sweep went on to job-b"
    assert len(list(second.iterdir())) == 1, "job-b was touched before job-a was finished"

    assert repository.sweep_artifact_root(limit=2) == 2  # job-b's file, then job-b itself
    assert not second.exists()
    assert repository.sweep_artifact_root(limit=2) == 0


def test_the_limit_bounds_staging_and_legacy_directories_as_well_as_job_directories(tmp_path):
    repository = _repository(tmp_path)
    _job(repository, "job-done", finished=True)
    for index in range(3):
        staging = repository.artifact_root / f".recipe-job-done-abcd123{index}"
        staging.mkdir()
        (staging / "output").write_bytes(b"synthetic")
    (repository.artifact_root / ".artifact_quarantine").mkdir()

    per_pass = []
    for _ in range(8):  # bounded: four things to remove
        per_pass.append(repository.sweep_artifact_root(limit=1))
        if per_pass[-1] == 0:
            break

    assert per_pass == [1, 1, 1, 1, 0]
    assert [entry.name for entry in repository.artifact_root.iterdir()] == [".quarantine"]


def test_the_entry_budget_stops_a_pass_early_and_the_next_one_carries_on(tmp_path, monkeypatch):
    """The clause was never exercised: the earlier test would pass without it."""

    repository = _repository(tmp_path)
    monkeypatch.setattr(repository_module, "_SWEEP_ENTRY_BUDGET", 3)
    for index in range(6):
        (repository.artifact_root / f"job-{index}").mkdir()

    per_pass = []
    for _ in range(10):  # bounded: at most one pass per entry
        per_pass.append(repository.sweep_artifact_root(limit=100))
        if not any((repository.artifact_root / f"job-{index}").exists() for index in range(6)):
            break

    assert max(per_pass) <= 3, "one pass looked at more entries than its budget allows"
    assert len([removed for removed in per_pass if removed]) >= 2
    assert sum(per_pass) == 6


def test_the_sweep_never_removes_an_unexpired_artifact_or_a_directory_a_publish_just_made(tmp_path):
    repository = _repository(tmp_path)
    job_id = _job(repository, "job-1")
    artifact = _publish(repository, job_id)
    just_made = repository.artifact_root / "job-just-made"
    just_made.mkdir()
    (just_made / f".tmp-{'f' * 32}").write_bytes(b"being written")  # a publish's first file

    repository.sweep_artifact_root()
    repository.sweep_artifact_root()

    assert repository.read_artifact(artifact.artifact_id) == b"synthetic"
    assert (just_made / f".tmp-{'f' * 32}").exists()


def test_a_sweep_cannot_remove_a_job_directory_between_its_creation_and_its_first_file(tmp_path, monkeypatch):
    """A publish makes the directory, then the temporary file; an empty directory is fair game.

    The sweep's ``rmdir`` therefore waits on the same lock, so the publish
    never finds the directory gone under it.
    """

    repository = _repository(tmp_path)
    job_id = _job(repository, "job-1")
    inside = Event()
    release = Event()
    real_open = Path.open

    def slow_open(self, mode="r", *args, **kwargs):
        if self.name.startswith(".tmp-") and mode == "xb":
            inside.set()
            assert release.wait(5.0)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", slow_open)
    outcome: list[object] = []
    publisher = Thread(
        target=lambda: outcome.append(_publish(repository, job_id)),
        name="cortex-test-publisher",
    )
    sweeper_done = Event()
    sweeper = Thread(
        target=lambda: (repository.sweep_artifact_root(), sweeper_done.set()),
        name="cortex-test-sweeper",
    )
    try:
        publisher.start()
        assert inside.wait(5.0)
        sweeper.start()
        assert not sweeper_done.wait(0.3), "the sweep did not wait for the directory's owner"
    finally:
        release.set()
        publisher.join(10.0)
        if sweeper.is_alive():
            sweeper.join(10.0)

    assert len(outcome) == 1
    assert repository.read_artifact(outcome[0].artifact_id) == b"synthetic"


def test_the_cleanup_supervisor_runs_the_sweep_and_counts_it(tmp_path):
    repository = _repository(tmp_path)
    (repository.artifact_root / "job-empty").mkdir()
    supervisor = ExecutionCleanupSupervisor(repository)

    assert supervisor.run_once() is True

    assert supervisor.metrics.swept == 1
    assert not (repository.artifact_root / "job-empty").exists()
