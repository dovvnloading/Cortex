"""Artifact paths are stored relative to the artifact root, additively.

A row used to record the absolute path it was written with, so moving the data
directory made every existing row point at nothing: live attachments stopped
reading and expired ones were never reclaimed. Schema version 4 lets new rows
record their path relative to the artifact root. Nothing is rewritten on
upgrade -- rows written before that keep their absolute path -- and both forms
are read.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import shutil

import pytest

from cortex_backend.execution.repository import (
    SCHEMA_VERSION,
    ExecutionRepository,
    ExecutionRepositoryError,
)

_LONG_AGO = "2000-01-01T00:00:00+00:00"


def _open(data: Path) -> ExecutionRepository:
    return ExecutionRepository(data / "execution.sqlite", data / "artifacts")


def _job(repository: ExecutionRepository, job_id: str) -> str:
    repository.create_job(
        job_id=job_id,
        owner=repository.installation_principal_id,
        request_id=f"request-{job_id}",
        profile="scratch.auto.v1",
        payload={},
    )
    repository.transition(job_id, status="succeeded", event="completed")
    return job_id


def _publish(repository: ExecutionRepository, job_id: str, name: str = "out.txt", *, expired: bool = False):
    artifact = repository.publish_artifact(
        _job(repository, job_id), name=name, content=b"synthetic", mime_type="text/plain"
    )
    if expired:
        with repository.connect() as connection:
            connection.execute(
                "UPDATE execution_artifacts SET expires_at = ? WHERE artifact_id = ?",
                (_LONG_AGO, artifact.artifact_id),
            )
    return artifact


def _cleanup(repository: ExecutionRepository):
    """One retention pass at the real time: only rows made expired on purpose are due."""

    return repository.cleanup_expired()


def _rows(repository: ExecutionRepository, sql: str, *params: object):
    with repository.connect() as connection:
        return connection.execute(sql, params).fetchall()


def _artifact_row(repository: ExecutionRepository, artifact_id: str):
    with repository.connect() as connection:
        return connection.execute(
            "SELECT path FROM execution_artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()


def _tombstones(repository: ExecutionRepository) -> list[tuple[str, str, str]]:
    with repository.connect() as connection:
        return [
            (row["path"], row["quarantine_path"], row["state"])
            for row in connection.execute(
                "SELECT path, quarantine_path, state FROM execution_artifact_cleanup ORDER BY artifact_id"
            ).fetchall()
        ]


def _leave_a_pending_tombstone(repository: ExecutionRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run a cleanup whose move into quarantine is refused, so the tombstone stays pending."""

    def refused(*_args: object, **_kwargs: object) -> Path:
        raise PermissionError(13, "held open by a scanner")

    with monkeypatch.context() as blocked:
        blocked.setattr(Path, "replace", refused)
        result = _cleanup(repository)
    assert (result.artifacts, result.skipped) == (0, 1)


def _as_version_3_wrote_it(repository: ExecutionRepository) -> None:
    """Rewrite every path the way the previous build recorded it: absolute, at version 3."""

    root = repository.artifact_root
    with repository.connect() as connection:
        for row in connection.execute("SELECT artifact_id, path FROM execution_artifacts").fetchall():
            connection.execute(
                "UPDATE execution_artifacts SET path = ? WHERE artifact_id = ?",
                (str(root / row["path"]), row["artifact_id"]),
            )
        for row in connection.execute(
            "SELECT artifact_id, path, quarantine_path FROM execution_artifact_cleanup"
        ).fetchall():
            connection.execute(
                "UPDATE execution_artifact_cleanup SET path = ?, quarantine_path = ? WHERE artifact_id = ?",
                (str(root / row["path"]), str(root / row["quarantine_path"]), row["artifact_id"]),
            )
        connection.execute("UPDATE execution_schema SET version = 3 WHERE id = 1")


def _version(repository: ExecutionRepository) -> int:
    with repository.connect() as connection:
        return int(connection.execute("SELECT version FROM execution_schema WHERE id = 1").fetchone()[0])


# -- A store written by this build -------------------------------------------------------------


def test_new_rows_name_their_files_relative_to_the_artifact_root(tmp_path):
    repository = _open(tmp_path / "data")
    artifact = _publish(repository, "job-1", "out.txt")

    stored = _artifact_row(repository, artifact.artifact_id)["path"]

    assert stored == f"job-1/{Path(artifact.path).name}"
    assert not os.path.isabs(stored) and ":" not in stored and "\\" not in stored
    # What a caller is handed is still a real location, as it always was.
    assert Path(artifact.path) == repository.artifact_root / "job-1" / Path(artifact.path).name
    reread = repository.get_artifact(artifact.artifact_id)
    assert reread is not None and reread.path == artifact.path
    assert repository.read_artifact(artifact.artifact_id) == b"synthetic"


def test_a_cleanup_tombstone_records_relative_paths(tmp_path, monkeypatch):
    repository = _open(tmp_path / "data")
    artifact = _publish(repository, "job-1", expired=True)

    _leave_a_pending_tombstone(repository, monkeypatch)

    ((path, quarantine, state),) = _tombstones(repository)
    assert state == "pending"
    assert path == f"job-1/{Path(artifact.path).name}"
    assert PurePosixPath(quarantine).parent.as_posix() == ".quarantine"
    assert quarantine.endswith(".artifact") and not os.path.isabs(quarantine)
    assert Path(artifact.path).is_file(), "the refused move left the file where it was"

    result = _cleanup(repository)  # the move is allowed again
    assert (result.artifacts, result.skipped) == (1, 0)
    assert not Path(artifact.path).exists()
    assert _tombstones(repository) == []


def test_a_moved_data_directory_keeps_live_and_expiring_artifacts_working(tmp_path, monkeypatch):
    first = tmp_path / "data"
    repository = _open(first)
    live = _publish(repository, "job-live", "live.txt")
    stuck = _publish(repository, "job-stuck", "stuck.txt", expired=True)
    _leave_a_pending_tombstone(repository, monkeypatch)
    waiting = _publish(repository, "job-waiting", "waiting.txt", expired=True)
    old_location = Path(stuck.path)

    moved = tmp_path / "moved"
    shutil.move(str(first), str(moved))
    reopened = _open(moved)

    # The live artifact still reads, and it reports where it is now.
    assert reopened.read_artifact(live.artifact_id) == b"synthetic"
    found = reopened.get_artifact(live.artifact_id)
    assert found is not None and Path(found.path) == moved / "artifacts" / "job-live" / Path(live.path).name
    # The pending tombstone and the expired row are both reclaimed, with nothing skipped.
    result = _cleanup(reopened)
    assert (result.artifacts, result.skipped) == (2, 0)
    assert reopened.get_artifact(stuck.artifact_id) is None
    assert reopened.get_artifact(waiting.artifact_id) is None
    assert not (moved / "artifacts" / "job-stuck").exists()
    assert not (moved / "artifacts" / "job-waiting").exists()
    assert not old_location.exists()
    assert reopened.read_artifact(live.artifact_id) == b"synthetic"
    assert _tombstones(reopened) == []


# -- A store written by the previous build ------------------------------------------------------


def test_a_store_written_before_version_4_is_read_as_it_was_and_is_not_rewritten(tmp_path, monkeypatch):
    data = tmp_path / "data"
    repository = _open(data)
    live = _publish(repository, "job-live", "live.txt")
    stuck = _publish(repository, "job-stuck", "stuck.txt", expired=True)
    _leave_a_pending_tombstone(repository, monkeypatch)
    _as_version_3_wrote_it(repository)
    assert _version(repository) == 3
    before = {
        "artifacts": sorted(
            (row["artifact_id"], row["path"])
            for row in _rows(repository, "SELECT artifact_id, path FROM execution_artifacts")
        ),
        "tombstones": _tombstones(repository),
    }
    assert all(os.path.isabs(path) for _id, path in before["artifacts"])

    upgraded = _open(data)

    assert _version(upgraded) == SCHEMA_VERSION == 4
    after = {
        "artifacts": sorted(
            (row["artifact_id"], row["path"])
            for row in _rows(upgraded, "SELECT artifact_id, path FROM execution_artifacts")
        ),
        "tombstones": _tombstones(upgraded),
    }
    assert after == before, "the upgrade rewrote rows it had no need to touch"
    # Both forms read, and an absolute row reports the path it holds.
    assert upgraded.read_artifact(live.artifact_id) == b"synthetic"
    found = upgraded.get_artifact(live.artifact_id)
    assert found is not None and found.path == dict(before["artifacts"])[live.artifact_id]
    result = _cleanup(upgraded)
    assert (result.artifacts, result.skipped) == (1, 0)
    assert upgraded.get_artifact(stuck.artifact_id) is None
    assert not Path(stuck.path).exists()
    assert _tombstones(upgraded) == []
    # A row written after the upgrade is relative, next to the absolute one.
    fresh = _publish(upgraded, "job-fresh", "fresh.txt")
    assert not os.path.isabs(_artifact_row(upgraded, fresh.artifact_id)["path"])
    assert os.path.isabs(_artifact_row(upgraded, live.artifact_id)["path"])


def test_reopening_a_version_4_store_changes_nothing(tmp_path):
    data = tmp_path / "data"
    repository = _open(data)
    artifact = _publish(repository, "job-1")
    before = _artifact_row(repository, artifact.artifact_id)["path"]

    again = _open(data)

    assert _version(again) == SCHEMA_VERSION
    assert _artifact_row(again, artifact.artifact_id)["path"] == before
    assert again.read_artifact(artifact.artifact_id) == b"synthetic"


def test_an_absolute_row_in_a_moved_data_directory_is_contained_as_it_always_was(tmp_path):
    """The limitation is only for rows written before version 4: they are refused, not followed."""

    first = tmp_path / "data"
    repository = _open(first)
    artifact = _publish(repository, "job-1", expired=True)
    _as_version_3_wrote_it(repository)
    moved = tmp_path / "moved"
    shutil.move(str(first), str(moved))
    reopened = _open(moved)
    survivor = moved / "artifacts" / "job-1" / Path(artifact.path).name

    with pytest.raises(ExecutionRepositoryError):
        reopened.read_artifact(artifact.artifact_id)
    result = _cleanup(reopened)

    assert (result.artifacts, result.skipped) == (0, 1)
    assert reopened.get_artifact(artifact.artifact_id) is None  # the unusable row is dropped
    assert survivor.is_file()  # and nothing outside a validated location was touched


# -- Failure paths: a relative row is confined exactly as an absolute one is ----------------------


_ESCAPES = (
    "../outside.txt",
    pytest.param(
        "..\\outside.txt",
        marks=pytest.mark.skipif(os.name != "nt", reason="a backslash is a separator only on Windows"),
    ),
    "job-1/../../outside.txt",
    "./../outside.txt",
)


@pytest.mark.parametrize("stored", _ESCAPES)
def test_a_relative_artifact_row_that_climbs_out_of_the_root_is_refused_and_touches_nothing(tmp_path, stored):
    repository = _open(tmp_path / "data")
    outside = tmp_path / "data" / "outside.txt"
    outside.write_bytes(b"not ours")
    artifact = _publish(repository, "job-1", expired=True)
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_artifacts SET path = ? WHERE artifact_id = ?", (stored, artifact.artifact_id)
        )

    with pytest.raises(ExecutionRepositoryError):
        repository.read_artifact(artifact.artifact_id)
    with pytest.raises(ExecutionRepositoryError):
        repository.delete_artifact(artifact.artifact_id)
    assert outside.read_bytes() == b"not ours"
    result = _cleanup(repository)

    assert (result.artifacts, result.skipped) == (0, 1)
    assert outside.read_bytes() == b"not ours"
    assert repository.get_artifact(artifact.artifact_id) is None


@pytest.mark.skipif(os.name != "nt", reason="drive-relative and rooted paths are a Windows form")
@pytest.mark.parametrize("form", ("rooted", "rooted-forward", "other-drive"))
def test_a_drive_relative_or_rooted_row_is_not_followed_out_of_the_root(tmp_path, form):
    """Joining one of these to the root must not land anywhere but under it.

    A rooted path keeps only the root's drive, and a path on another drive
    replaces the root altogether; either way it ends up outside and is refused.
    """

    other_drive = "Y:" if tmp_path.drive.upper() == "Z:" else "Z:"
    stored = {
        "rooted": "\\outside.txt",
        "rooted-forward": "/outside.txt",
        "other-drive": f"{other_drive}outside.txt",
    }[form]
    repository = _open(tmp_path / "data")
    artifact = _publish(repository, "job-1", expired=True)
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_artifacts SET path = ? WHERE artifact_id = ?", (stored, artifact.artifact_id)
        )

    with pytest.raises(ExecutionRepositoryError):
        repository.read_artifact(artifact.artifact_id)
    result = _cleanup(repository)

    assert (result.artifacts, result.skipped) == (0, 1)


@pytest.mark.parametrize("stored", ("../outside.txt", ".quarantine/foreign.artifact", "job-1"))
def test_a_relative_tombstone_that_names_something_it_may_not_touch_is_discarded_untouched(tmp_path, stored):
    """Outside the root, inside quarantine (which is not an artifact) or a directory."""

    repository = _open(tmp_path / "data")
    outside = tmp_path / "data" / "outside.txt"
    outside.write_bytes(b"not ours")
    foreign = repository.quarantine_root / "foreign.artifact"
    foreign.write_bytes(b"belongs to another tombstone")
    artifact = _publish(repository, "job-1", expired=True)
    with repository.connect() as connection:
        connection.execute(
            """
            INSERT INTO execution_artifact_cleanup (artifact_id, path, quarantine_path, state, created_at)
            VALUES (?, ?, '.quarantine/own.artifact', 'pending', ?)
            """,
            (artifact.artifact_id, stored, _LONG_AGO),
        )

    result = _cleanup(repository)

    assert result.skipped == 1 and result.artifacts == 0
    assert _tombstones(repository) == []
    assert outside.read_bytes() == b"not ours"
    assert foreign.read_bytes() == b"belongs to another tombstone"
    assert Path(artifact.path).parent.is_dir()


def test_a_relative_quarantine_path_outside_quarantine_is_never_unlinked(tmp_path):
    repository = _open(tmp_path / "data")
    victim = repository.artifact_root / "job-other" / "keep.txt"
    victim.parent.mkdir()
    victim.write_bytes(b"live artifact bytes")
    artifact = _publish(repository, "job-1", expired=True)
    with repository.connect() as connection:
        connection.execute(
            """
            INSERT INTO execution_artifact_cleanup (artifact_id, path, quarantine_path, state, created_at)
            VALUES (?, ?, ?, 'finalized', ?)
            """,
            (artifact.artifact_id, f"job-1/{Path(artifact.path).name}", "job-other/keep.txt", _LONG_AGO),
        )

    _cleanup(repository)

    assert victim.read_bytes() == b"live artifact bytes"


# -- The two conversions ----------------------------------------------------------------------


def test_the_path_conversions_round_trip_and_leave_foreign_absolute_paths_alone(tmp_path):
    repository = _open(tmp_path / "data")
    inside = repository.artifact_root / "job-1" / "file.bin"
    foreign = tmp_path / "elsewhere" / "file.bin"

    assert repository._stored_text(inside) == "job-1/file.bin"
    assert repository._stored_path("job-1/file.bin") == inside
    assert repository._stored_text(foreign) == str(foreign)
    assert repository._stored_path(str(foreign)) == foreign
    assert repository._stored_path(repository._stored_text(inside)) == inside
