"""Adversarial trusted copy-in, MIME validation, quarantine, and publication tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import cortex_backend.execution.artifact_boundary as boundary_module
from cortex_backend.execution import (
    ArtifactBoundary,
    ArtifactBoundaryError,
    ArtifactSourceGrant,
    ExecutionRepository,
    ExecutionRepositoryError,
    OutputClaim,
)
from cortex_backend.execution.artifact_boundary import sniff_artifact_mime


def _repository(tmp_path: Path, *, maximum: int = 128) -> tuple[ExecutionRepository, str]:
    repository = ExecutionRepository(
        tmp_path / "execution.sqlite",
        tmp_path / "artifacts",
        max_artifact_bytes=maximum,
    )
    job, _created = repository.create_job(
        job_id="job-artifact-boundary",
        owner="session-a",
        request_id="request-artifact-boundary",
        profile="artifact.transform.v1",
        payload={},
    )
    return repository, job.job_id


def _grant(path: Path, job_id: str = "job-artifact-boundary") -> ArtifactSourceGrant:
    return ArtifactSourceGrant(
        owner="session-a",
        job_id=job_id,
        source_turn_id="turn-1",
        source_path=path,
    )


def test_copy_in_is_owner_bound_mime_sniffed_and_never_overwrites_source(tmp_path: Path):
    repository, job_id = _repository(tmp_path)
    source = tmp_path / "uploaded.bin"
    content = b"\x89PNG\r\n\x1a\ntrusted bytes"
    source.write_bytes(content)
    boundary = ArtifactBoundary(repository)

    artifact = boundary.copy_in(_grant(source, job_id))

    assert artifact.mime_type == "image/png"
    assert artifact.sha256
    assert source.read_bytes() == content
    assert repository.read_artifact(artifact.artifact_id) == content
    assert Path(artifact.path).is_relative_to(repository.artifact_root.resolve())


def test_copy_in_rejects_wrong_owner_and_alternate_data_stream_paths(tmp_path: Path):
    repository, job_id = _repository(tmp_path)
    source = tmp_path / "input.txt"
    source.write_text("hello", encoding="utf-8")
    boundary = ArtifactBoundary(repository)

    with pytest.raises(ArtifactBoundaryError) as owner_error:
        boundary.copy_in(
            ArtifactSourceGrant(
                owner="session-b",
                job_id=job_id,
                source_turn_id="turn-1",
                source_path=source,
            )
        )
    assert owner_error.value.code == "artifact_owner_mismatch"

    with pytest.raises(ArtifactBoundaryError) as ads_error:
        boundary.copy_in(_grant(Path(f"{source}:secret"), job_id))
    assert ads_error.value.code == "artifact_path_invalid"


def test_copy_in_rejects_source_mutation_during_snapshot(tmp_path: Path, monkeypatch):
    repository, job_id = _repository(tmp_path)
    source = tmp_path / "input.txt"
    source.write_text("stable", encoding="utf-8")
    boundary = ArtifactBoundary(repository)
    original = boundary_module._file_identity
    calls = 0

    def changed(path: Path):
        nonlocal calls
        calls += 1
        identity = original(path)
        return identity if calls == 1 else (*identity[:-1], identity[-1] + 1)

    monkeypatch.setattr(boundary_module, "_file_identity", changed)
    with pytest.raises(ArtifactBoundaryError) as error:
        boundary.copy_in(_grant(source, job_id))
    assert error.value.code == "artifact_source_changed"


def test_copy_in_rejects_hardlinks_and_symlinks_when_platform_allows_them(tmp_path: Path):
    repository, job_id = _repository(tmp_path)
    external = tmp_path / "external.bin"
    external.write_bytes(b"hardlink")
    hardlink = tmp_path / "hardlink.bin"
    try:
        hardlink.hardlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable on this platform")
    boundary = ArtifactBoundary(repository)
    with pytest.raises(ArtifactBoundaryError) as hardlink_error:
        boundary.copy_in(_grant(hardlink, job_id))
    assert hardlink_error.value.code == "artifact_hardlink_rejected"

    symlink = tmp_path / "symlink.bin"
    try:
        symlink.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")
    with pytest.raises(ArtifactBoundaryError) as symlink_error:
        boundary.copy_in(_grant(symlink, job_id))
    assert symlink_error.value.code == "artifact_reparse_point"


def test_output_collection_requires_exact_claims_and_publishes_only_after_validation(tmp_path: Path):
    repository, job_id = _repository(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    (output_root / "result.txt").write_text("safe output", encoding="utf-8")
    boundary = ArtifactBoundary(repository)

    published = boundary.collect_outputs(
        job_id,
        "session-a",
        output_root,
        [OutputClaim("result.txt", "text/plain")],
    )

    assert len(published) == 1
    assert published[0].mime_type == "text/plain"
    assert repository.read_artifact(published[0].artifact.artifact_id) == b"safe output"
    assert not (output_root / "result.txt").exists()


def test_output_extra_file_is_quarantined_without_partial_publication(tmp_path: Path):
    repository, job_id = _repository(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    (output_root / "declared.txt").write_text("declared", encoding="utf-8")
    (output_root / "secret.txt").write_text("unclaimed", encoding="utf-8")
    boundary = ArtifactBoundary(repository)

    with pytest.raises(ArtifactBoundaryError) as error:
        boundary.collect_outputs(
            job_id,
            "session-a",
            output_root,
            [OutputClaim("declared.txt", "text/plain")],
        )
    assert error.value.code == "artifact_unclaimed_output"
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM execution_artifacts").fetchone()[0] == 0
    assert list((output_root / ".quarantine").iterdir())


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"<!doctype html><script>alert(1)</script>", "invalid_artifact"),
        (b"MZ\x90\x00unsafe executable", "invalid_artifact"),
        (b"PK\x03\x04archive", "invalid_artifact"),
        (b"0" * 257 + b"ustar" + b"tar", "invalid_artifact"),
        (b'{"value": NaN}', "invalid_artifact"),
        (b"plain text", "artifact_mime_mismatch"),
    ],
)
def test_output_rejects_active_archive_or_mismatched_content(
    tmp_path: Path,
    content: bytes,
    expected: str,
):
    repository, job_id = _repository(tmp_path, maximum=max(128, len(content)))
    output_root = tmp_path / "output"
    output_root.mkdir()
    (output_root / "result.bin").write_bytes(content)
    boundary = ArtifactBoundary(repository)

    with pytest.raises(ArtifactBoundaryError) as error:
        boundary.collect_outputs(
            job_id,
            "session-a",
            output_root,
            [OutputClaim("result.bin", "image/png" if expected == "artifact_mime_mismatch" else None)],
        )
    assert error.value.code == expected


def test_output_publication_rolls_back_prior_artifacts_on_batch_failure(tmp_path: Path, monkeypatch):
    repository, job_id = _repository(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    (output_root / "one.txt").write_text("one", encoding="utf-8")
    (output_root / "two.txt").write_text("two", encoding="utf-8")
    boundary = ArtifactBoundary(repository)
    original = repository.publish_artifact
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ExecutionRepositoryError("injected publication failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "publish_artifact", fail_second)
    with pytest.raises(ArtifactBoundaryError) as error:
        boundary.collect_outputs(
            job_id,
            "session-a",
            output_root,
            [OutputClaim("one.txt", "text/plain"), OutputClaim("two.txt", "text/plain")],
        )
    assert error.value.code == "artifact_publish_failed"
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM execution_artifacts").fetchone()[0] == 0
    assert list((output_root / ".quarantine").iterdir())


def test_json_non_finite_numbers_are_not_misclassified_as_text():
    with pytest.raises(ArtifactBoundaryError) as error:
        sniff_artifact_mime(b'{"value": Infinity}')
    assert error.value.code == "invalid_artifact"


def test_repository_expiry_cleanup_fails_closed_for_external_database_paths(tmp_path: Path):
    repository, job_id = _repository(tmp_path)
    source = tmp_path / "input.txt"
    source.write_text("safe", encoding="utf-8")
    artifact = ArtifactBoundary(repository).copy_in(_grant(source, job_id))
    external = tmp_path / "outside.txt"
    external.write_text("must remain", encoding="utf-8")
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_artifacts SET path = ? WHERE artifact_id = ?",
            (str(external), artifact.artifact_id),
        )

    with pytest.raises(ExecutionRepositoryError):
        repository.purge_expired(now="9999-01-01T00:00:00+00:00")
    assert external.read_text(encoding="utf-8") == "must remain"


def test_output_limits_and_invalid_claims_fail_before_publication(tmp_path: Path):
    repository, job_id = _repository(tmp_path, maximum=8)
    output_root = tmp_path / "output"
    output_root.mkdir()
    (output_root / "result.txt").write_text("too large", encoding="utf-8")
    boundary = ArtifactBoundary(repository, max_total_output_bytes=8)

    with pytest.raises(ArtifactBoundaryError) as error:
        boundary.collect_outputs(
            job_id,
            "session-a",
            output_root,
            [OutputClaim("result.txt", "text/plain")],
        )
    assert error.value.code == "artifact_too_large"

    with pytest.raises(ValueError):
        OutputClaim("../escape.txt")
