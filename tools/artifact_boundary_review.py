"""Deterministic, disposable artifact-boundary security qualification.

The review corpus exercises the trusted copy-in and output-publication boundary
with fixed bytes and temporary files only.  It accepts no user/model input, never
executes a file, and reports stable case names/categories rather than paths or
operating-system details.  A missing Windows link primitive is a blocked release
gate, not a silent pass.

The overall status is ``failed`` when any case observed the boundary doing the
wrong thing, ``blocked`` when nothing failed but a case could not run on this
host, and ``passed`` only when every case ran and passed.  Anything but
``passed`` exits non-zero: 1 for failed, 2 for blocked.  ``--strict`` is the
default and is still accepted for existing callers.  ``--json`` prints the
machine-readable result instead of the readable summary.
"""

from __future__ import annotations

import argparse
from functools import partial
from hashlib import sha256
import inspect
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import cast
from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import cortex_backend.execution.artifact_boundary as boundary_module  # noqa: E402
from cortex_backend.execution.artifact_boundary import (  # noqa: E402
    ArtifactBoundary,
    ArtifactBoundaryError,
    ArtifactSourceGrant,
    OutputClaim,
)
from cortex_backend.execution.repository import (  # noqa: E402
    ExecutionRepository,
    ExecutionRepositoryError,
)


_OWNER = "review-owner"
_TURN = "review-turn"
_PNG = b"\x89PNG\r\n\x1a\nreview-bytes"


class _ReviewFailure(Exception):
    """Internal bounded result marker; its text never leaves the probe."""

    def __init__(self, code: str) -> None:
        self.code = code


class _BlockedCapability(Exception):
    """A required host primitive was unavailable for this release probe."""

    def __init__(self, code: str) -> None:
        self.code = code


def _fixture(root: Path, case_name: str) -> tuple[Path, ExecutionRepository, ArtifactBoundary, str]:
    case_root = root / case_name
    case_root.mkdir()
    repository = ExecutionRepository(
        case_root / "execution.sqlite",
        case_root / "artifacts",
        max_artifact_bytes=4_096,
    )
    job_id = f"job-{case_name}"
    repository.create_job(
        job_id=job_id,
        owner=_OWNER,
        request_id=f"request-{case_name}",
        profile="artifact.transform.v1",
        payload={},
    )
    return case_root, repository, ArtifactBoundary(repository), job_id


def _grant(path: Path, job_id: str, *, owner: str = _OWNER) -> ArtifactSourceGrant:
    return ArtifactSourceGrant(
        owner=owner,
        job_id=job_id,
        source_turn_id=_TURN,
        source_path=path,
    )


def _expect_boundary_error(expected: str, action: Callable[[], object]) -> None:
    try:
        action()
    except ArtifactBoundaryError as error:
        if error.code != expected:
            raise _ReviewFailure(f"wrong_category:{expected}:{error.code}") from None
    else:
        raise _ReviewFailure(f"accepted:{expected}")


def _case_copy_in_preserves_source(root: Path, repository: ExecutionRepository, boundary: ArtifactBoundary, job_id: str) -> None:
    source = root / "source.bin"
    source.write_bytes(_PNG)
    artifact = boundary.copy_in(_grant(source, job_id))
    if artifact.mime_type != "image/png" or source.read_bytes() != _PNG:
        raise _ReviewFailure("source_or_mime_changed")
    if repository.read_artifact(artifact.artifact_id) != _PNG:
        raise _ReviewFailure("published_bytes_changed")


def _case_copy_in_owner_binding(root: Path, _repository: ExecutionRepository, boundary: ArtifactBoundary, job_id: str) -> None:
    source = root / "source.txt"
    source.write_text("owner-bound", encoding="utf-8")
    _expect_boundary_error(
        "artifact_owner_mismatch",
        lambda: boundary.copy_in(_grant(source, job_id, owner="other-owner")),
    )


def _case_copy_in_ads_path(root: Path, _repository: ExecutionRepository, boundary: ArtifactBoundary, job_id: str) -> None:
    source = root / "source.txt"
    source.write_text("ads", encoding="utf-8")
    _expect_boundary_error(
        "artifact_path_invalid",
        lambda: boundary.copy_in(_grant(Path(f"{source}:secret"), job_id)),
    )


def _case_copy_in_untyped_grant(_root: Path, _repository: ExecutionRepository, boundary: ArtifactBoundary, _job_id: str) -> None:
    # Deliberately not an ArtifactSourceGrant: the boundary must refuse it.
    _expect_boundary_error("artifact_grant_invalid", lambda: boundary.copy_in(object()))  # type: ignore[arg-type]


def _case_copy_in_hardlink(root: Path, _repository: ExecutionRepository, boundary: ArtifactBoundary, job_id: str) -> None:
    source = root / "external.bin"
    source.write_bytes(b"hardlink")
    link = root / "hardlink.bin"
    try:
        link.hardlink_to(source)
    except (OSError, NotImplementedError):
        raise _BlockedCapability("hardlink_unavailable") from None
    _expect_boundary_error("artifact_hardlink_rejected", lambda: boundary.copy_in(_grant(link, job_id)))


def _case_copy_in_symlink(root: Path, _repository: ExecutionRepository, boundary: ArtifactBoundary, job_id: str) -> None:
    source = root / "external.bin"
    source.write_bytes(b"symlink")
    link = root / "symlink.bin"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        raise _BlockedCapability("symlink_unavailable") from None
    _expect_boundary_error("artifact_reparse_point", lambda: boundary.copy_in(_grant(link, job_id)))


def _case_mime_active_and_nonfinite_corpus(_root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    rejected = (
        b"MZ\x90\x00executable",
        b"PK\x03\x04archive",
        b"<!doctype html><script>x</script>",
        b"\xff\xfe<\x00s\x00v\x00g\x00>\x00",
        b"[InternetShortcut]\nURL=https://example.invalid",
        b'{"value": NaN}',
        b'{"value": 1e999999}',
    )
    for content in rejected:
        _expect_boundary_error("invalid_artifact", partial(boundary_module.sniff_artifact_mime, content))
    if boundary_module.sniff_artifact_mime(b'{"value": ' + b"9" * 5_000 + b"}") != "text/plain":
        raise _ReviewFailure("oversized_json_not_bounded")


def _case_output_exact_claims_and_quarantine(root: Path, repository: ExecutionRepository, boundary: ArtifactBoundary, job_id: str) -> None:
    output = root / "output"
    output.mkdir()
    (output / "declared.txt").write_text("declared", encoding="utf-8")
    (output / "unclaimed.txt").write_text("unclaimed", encoding="utf-8")
    _expect_boundary_error(
        "artifact_unclaimed_output",
        lambda: boundary.collect_outputs(job_id, _OWNER, output, [OutputClaim("declared.txt", "text/plain")]),
    )
    with repository.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM execution_artifacts").fetchone()[0]
    if count != 0 or not list((output / ".quarantine").iterdir()):
        raise _ReviewFailure("quarantine_or_publication_invariant_failed")


def _case_output_symlink_rejection(root: Path, _repository: ExecutionRepository, boundary: ArtifactBoundary, job_id: str) -> None:
    output = root / "output"
    output.mkdir()
    external = root / "external.txt"
    external.write_text("outside", encoding="utf-8")
    link = output / "link.txt"
    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError):
        raise _BlockedCapability("symlink_unavailable") from None
    _expect_boundary_error(
        "artifact_reparse_point",
        lambda: boundary.collect_outputs(job_id, _OWNER, output, [OutputClaim("link.txt")]),
    )


def _case_output_hardlink_rejection(root: Path, _repository: ExecutionRepository, boundary: ArtifactBoundary, job_id: str) -> None:
    output = root / "output"
    output.mkdir()
    external = root / "external.txt"
    external.write_text("outside", encoding="utf-8")
    link = output / "link.txt"
    try:
        link.hardlink_to(external)
    except (OSError, NotImplementedError):
        raise _BlockedCapability("hardlink_unavailable") from None
    _expect_boundary_error(
        "artifact_hardlink_rejected",
        lambda: boundary.collect_outputs(job_id, _OWNER, output, [OutputClaim("link.txt")]),
    )


def _case_output_publication_rollback(root: Path, repository: ExecutionRepository, boundary: ArtifactBoundary, job_id: str) -> None:
    output = root / "output"
    output.mkdir()
    (output / "one.txt").write_text("one", encoding="utf-8")
    (output / "two.txt").write_text("two", encoding="utf-8")
    original = repository.publish_artifact
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ExecutionRepositoryError("injected publication failure")
        return original(*args, **kwargs)

    repository.publish_artifact = fail_second  # type: ignore[method-assign]
    try:
        _expect_boundary_error(
            "artifact_publish_failed",
            lambda: boundary.collect_outputs(
                job_id,
                _OWNER,
                output,
                [OutputClaim("one.txt", "text/plain"), OutputClaim("two.txt", "text/plain")],
            ),
        )
    finally:
        repository.publish_artifact = original  # type: ignore[method-assign]
    with repository.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM execution_artifacts").fetchone()[0]
    if count != 0 or not list((output / ".quarantine").iterdir()):
        raise _ReviewFailure("rollback_or_quarantine_invariant_failed")


def _case_repository_size_integrity(root: Path, repository: ExecutionRepository, boundary: ArtifactBoundary, job_id: str) -> None:
    source = root / "source.txt"
    source.write_text("integrity", encoding="utf-8")
    artifact = boundary.copy_in(_grant(source, job_id))
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_artifacts SET size = size + 1 WHERE artifact_id = ?",
            (artifact.artifact_id,),
        )
    try:
        repository.read_artifact(artifact.artifact_id)
    except ExecutionRepositoryError:
        return
    raise _ReviewFailure("database_size_tamper_accepted")


_CaseFunction = Callable[[Path, ExecutionRepository, ArtifactBoundary, str], None]

# The corpus that runs. The digest is computed from this tuple, so it cannot
# drift from what actually executes.
_CASES: tuple[tuple[str, _CaseFunction], ...] = (
    ("copy_in_preserves_source", _case_copy_in_preserves_source),
    ("copy_in_owner_binding", _case_copy_in_owner_binding),
    ("copy_in_ads_path", _case_copy_in_ads_path),
    ("copy_in_untyped_grant", _case_copy_in_untyped_grant),
    ("copy_in_hardlink", _case_copy_in_hardlink),
    ("copy_in_symlink", _case_copy_in_symlink),
    ("mime_active_and_nonfinite_corpus", _case_mime_active_and_nonfinite_corpus),
    ("output_exact_claims_and_quarantine", _case_output_exact_claims_and_quarantine),
    ("output_symlink_rejection", _case_output_symlink_rejection),
    ("output_hardlink_rejection", _case_output_hardlink_rejection),
    ("output_publication_rollback", _case_output_publication_rollback),
    ("repository_size_integrity", _case_repository_size_integrity),
)

# The shared setup every case runs through. A change here changes what each
# case exercises, so it is part of the digest too.
_HARNESS: tuple[Callable[..., object], ...] = (_fixture, _grant, _expect_boundary_error)


def _corpus_digest(cases: tuple[tuple[str, _CaseFunction], ...]) -> str:
    """Fingerprint the code that runs, not a list of names kept beside it.

    The digest covers each case's name and source, in run order, plus the
    shared harness, so deleting, reordering, renaming or editing a case --
    or the setup it runs through -- changes it.
    """

    material = [
        ["harness", [inspect.getsource(helper) for helper in _HARNESS]],
        *([name, inspect.getsource(case)] for name, case in cases),
    ]
    encoded = json.dumps(material, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return sha256(encoded).hexdigest()[:16]


def run_review() -> dict[str, object]:
    """Run the fixed corpus in a disposable directory and return safe evidence."""

    cases = _CASES
    outcomes: list[dict[str, str]] = []
    with TemporaryDirectory(prefix="cortex-artifact-review-") as temporary:
        root = Path(temporary)
        for name, case in cases:
            try:
                case_root, repository, boundary, job_id = _fixture(root, name)
                case(case_root, repository, boundary, job_id)
            except _BlockedCapability as error:
                outcomes.append({"case": name, "status": "blocked", "reason": error.code})
            except _ReviewFailure as error:
                outcomes.append({"case": name, "status": "failed", "reason": error.code})
            except Exception as error:  # pragma: no cover - qualification failure path.
                outcomes.append({"case": name, "status": "failed", "reason": f"unexpected_exception:{type(error).__name__}"})
            else:
                outcomes.append({"case": name, "status": "passed"})
    passed = sum(outcome["status"] == "passed" for outcome in outcomes)
    blocked = sum(outcome["status"] == "blocked" for outcome in outcomes)
    failed = sum(outcome["status"] == "failed" for outcome in outcomes)
    # A wrong answer from the boundary is a failure, never a host limitation:
    # "blocked" is reserved for a case that could not run at all. A corpus
    # that ran nothing proved nothing, so it cannot pass either.
    if failed or not outcomes:
        status = "failed"
    elif blocked:
        status = "blocked"
    else:
        status = "passed"
    return {
        "status": status,
        "corpus": "artifact-boundary-review.v1",
        "corpus_digest": _corpus_digest(cases),
        "cases": len(outcomes),
        "passed": passed,
        "blocked": blocked,
        "failed": failed,
        "outcomes": outcomes,
    }


_EXIT_CODES = {"passed": 0, "failed": 1, "blocked": 2}


def _summary(result: dict[str, object]) -> str:
    lines = [
        f"Artifact boundary review: {result['status']}",
        f"  corpus {result['corpus']} (digest {result['corpus_digest']})",
        f"  {result['cases']} cases: {result['passed']} passed, "
        f"{result['blocked']} blocked, {result['failed']} failed",
    ]
    for outcome in cast("list[dict[str, str]]", result["outcomes"]):
        if outcome["status"] != "passed":
            lines.append(f"  {outcome['status']:<7} {outcome['case']} ({outcome['reason']})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print the result as compact JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="accepted for existing callers; strict is the default and cannot be turned off",
    )
    args = parser.parse_args(argv)
    result = run_review()
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(_summary(result))
    # Strict always: the module promises a blocked release gate, not a silent
    # pass, and a gate that exits zero on a total failure is not one.
    return _EXIT_CODES.get(str(result["status"]), 1)


if __name__ == "__main__":
    raise SystemExit(main())
