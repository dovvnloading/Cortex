"""A job id names a directory, so it must be a name every platform can create.

``create_job("nul")`` used to succeed and the first ``publish_artifact`` then
raised a raw ``OSError`` carrying the absolute path, because ``nul`` is the
Windows NUL device and not a directory name. ``a.`` shared a directory with
``a`` for the same reason: Windows drops trailing dots.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex_backend.execution import repository as repository_module
from cortex_backend.execution.repository import ExecutionRepository, ExecutionRepositoryError


def _repository(tmp_path: Path) -> ExecutionRepository:
    return ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")


def _create(repository: ExecutionRepository, job_id: str):
    return repository.create_job(
        job_id=job_id,
        owner=repository.installation_principal_id,
        request_id=f"request-{job_id}",
        profile="scratch.auto.v1",
        payload={},
    )


_REFUSED_JOB_IDS = (
    "nul",
    "NUL",
    "Nul",
    "con",
    "prn",
    "aux",
    "com1",
    "COM9",
    "lpt1",
    "LPT9",
    "nul.txt",
    "con.tar.gz",
    "aux.",  # a device name and a trailing dot
    "a.",
    "a..",
    "job-1.",
    "x" * 199 + ".",
    "x" * 201,
    "",
    ".hidden",
    "-lead",
    "a b",
    "a/b",
    "a\\b",
    "..",
    "a\n",
)

_ACCEPTED_JOB_IDS = (
    "a",
    "job-1",
    "a.b",
    "a..b",
    "0123456789abcdef0123456789abcdef",
    "null",
    "console",
    "com0",
    "com10",
    "lpt0",
    "nul-1",
    "nul_",
    "auxiliary",
    "x" * 200,
)


def _label(job_id: str) -> str:
    return job_id if 0 < len(job_id) <= 24 else f"{len(job_id)}-chars-ending-{job_id[-1:]!r}"


@pytest.mark.parametrize("job_id", _REFUSED_JOB_IDS, ids=_label)
def test_a_job_id_that_is_not_a_portable_directory_name_is_refused_when_the_job_is_created(
    tmp_path: Path, job_id: str
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(ValueError, match="job_id"):
        _create(repository, job_id)

    assert repository.list_jobs(owner=repository.installation_principal_id, include_terminal=True) == []


@pytest.mark.parametrize("job_id", _ACCEPTED_JOB_IDS, ids=_label)
def test_ordinary_job_ids_are_still_accepted(tmp_path: Path, job_id: str) -> None:
    repository = _repository(tmp_path)

    job, created = _create(repository, job_id)
    artifact = repository.publish_artifact(job.job_id, name="out.txt", content=b"synthetic")

    assert created is True
    assert repository.read_artifact(artifact.artifact_id) == b"synthetic"


@pytest.mark.parametrize("job_id", ("nul", "aux", "a.", "COM1"))
def test_publishing_for_a_job_that_was_stored_under_an_older_shape_touches_nothing(
    tmp_path: Path, job_id: str
) -> None:
    """A row an earlier build accepted is refused at publish, before any path is built."""

    repository = _repository(tmp_path)
    with repository.connect() as connection:
        connection.execute(
            """
            INSERT INTO execution_jobs
            (job_id, owner, request_id, profile, status, sequence, payload_json, created_at, updated_at)
            VALUES (?, ?, 'request-old', 'scratch.auto.v1', 'queued', 0, '{}', 'x', 'x')
            """,
            (job_id, repository.installation_principal_id),
        )
    before = sorted(entry.name for entry in repository.artifact_root.iterdir())

    with pytest.raises(ExecutionRepositoryError) as refused:
        repository.publish_artifact(job_id, name="out.txt", content=b"synthetic")

    assert str(tmp_path) not in str(refused.value)
    assert sorted(entry.name for entry in repository.artifact_root.iterdir()) == before


def test_a_directory_that_cannot_be_created_is_reported_without_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    job, _ = _create(repository, "job-1")
    real_mkdir = Path.mkdir

    def refuses(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "job-1":
            raise PermissionError(13, "Access is denied", str(self))
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", refuses)

    with pytest.raises(ExecutionRepositoryError) as failure:
        repository.publish_artifact(job.job_id, name="out.txt", content=b"synthetic")

    assert str(tmp_path) not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__ is True
    assert not (repository.artifact_root / "job-1").exists()


def test_a_file_that_cannot_be_opened_is_reported_without_its_path_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    job, _ = _create(repository, "job-1")
    real_open = Path.open

    def refuses(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if self.name.startswith(".tmp-"):
            raise PermissionError(13, "Access is denied", str(self))
        return real_open(self, mode, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", refuses)

    with pytest.raises(ExecutionRepositoryError) as failure:
        repository.publish_artifact(job.job_id, name="out.txt", content=b"synthetic")

    assert str(tmp_path) not in str(failure.value)
    assert failure.value.__cause__ is None
    assert not (repository.artifact_root / "job-1").exists(), "the directory this call made was left behind"


def test_a_failing_cleanup_after_a_refused_write_does_not_replace_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler unlinks partial files; an OSError there must not become the error raised."""

    repository = _repository(tmp_path)
    job, _ = _create(repository, "job-1")
    real_open = Path.open
    real_unlink = Path.unlink

    def refuses(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if self.name.startswith(".tmp-"):
            raise PermissionError(13, "Access is denied", str(self))
        return real_open(self, mode, *args, **kwargs)  # type: ignore[arg-type]

    def cannot_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.parent.name == "job-1":
            raise PermissionError(13, "Access is denied", str(self))
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", refuses)
    monkeypatch.setattr(Path, "unlink", cannot_unlink)

    with pytest.raises(ExecutionRepositoryError) as failure:
        repository.publish_artifact(job.job_id, name="out.txt", content=b"synthetic")

    assert str(tmp_path) not in str(failure.value)


def _directory_link(link: Path, target: Path) -> None:
    """Make ``link`` a symbolic link, or on Windows a junction, to ``target``; skip if neither can be made."""

    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    try:
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, AttributeError, OSError):
        pytest.skip("neither symbolic links nor junctions can be created here")


def test_a_job_directory_that_is_a_link_is_refused_before_anything_is_created(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    job, _ = _create(repository, "job-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    _directory_link(repository.artifact_root / "job-1", outside)

    with pytest.raises(ExecutionRepositoryError, match="escaped"):
        repository.publish_artifact(job.job_id, name="out.txt", content=b"synthetic")

    assert list(outside.iterdir()) == []


def test_the_containment_check_alone_refuses_a_link_before_the_directory_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``planned.parent != root`` is the check that does not depend on link detection.

    Older interpreters cannot see a junction (``Path.is_junction`` arrived in
    3.12), so ``_is_reparse_point`` says no; the resolved location is then the
    only thing that says the job directory is somewhere else. It must run
    before ``mkdir``, which would otherwise be pointed at the link's target.
    """

    repository = _repository(tmp_path)
    job, _ = _create(repository, "job-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    _directory_link(repository.artifact_root / "job-1", outside)
    monkeypatch.setattr(repository_module, "_is_reparse_point", lambda _path: False)
    made: list[Path] = []
    real_mkdir = Path.mkdir

    def recording_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        made.append(self)
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", recording_mkdir)

    with pytest.raises(ExecutionRepositoryError, match="escaped"):
        repository.publish_artifact(job.job_id, name="out.txt", content=b"synthetic")

    assert made == [], "a refused job directory was still handed to mkdir"
    assert list(outside.iterdir()) == []
