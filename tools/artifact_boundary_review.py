"""Deterministic, disposable artifact-boundary security qualification.

The review corpus exercises the execution artifact and consent-gated agent
work-copy boundaries with fixed bytes and temporary files only.  It accepts no user/model input, never
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
from threading import Event
from time import monotonic
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
from cortex_backend.agent.protocol import ToolCall  # noqa: E402
from cortex_backend.agent.tool_registry import ToolContext, ToolRegistry, ToolResult  # noqa: E402
from cortex_backend.agent.tools.files import register_file_tools  # noqa: E402
from cortex_backend.agent.workcopy import WorkCopy, WorkCopyError, create as create_workcopy  # noqa: E402


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


# Fixed source-only bytes; every path below is created beneath run_review's temporary root.
_AGENT_ORIGINAL = b"first\r\nsecond\n"
_AGENT_UPDATED = b"changed\n"
_AGENT_DELETED = b"deleted\r\nwithout-final-newline"
_AGENT_ADDED = b"new\r\nfile\n"
_AGENT_OUTSIDE = b"outside\r\nuntouched"


def _agent_workcopy(root: Path) -> WorkCopy:
    project = root / "agent-project"
    project.mkdir()
    (project / "source.py").write_bytes(_AGENT_ORIGINAL)
    (project / "deleted.py").write_bytes(_AGENT_DELETED)
    return create_workcopy(project, "review", confirmed=True, data_dir=root / "agent-data")


def _file_result(root: Path, name: str, **arguments: object) -> ToolResult:
    registry = ToolRegistry()
    register_file_tools(registry, root)
    result = registry.execute(
        ToolCall("review-file-call", name, arguments),
        ToolContext(Event(), monotonic() + 30),
    )
    if not isinstance(result, ToolResult):
        raise _ReviewFailure("invalid_tool_result")
    return result


def _expect_file_error(expected: str, root: Path, name: str, **arguments: object) -> None:
    result = _file_result(root, name, **arguments)
    if result.success:
        raise _ReviewFailure(f"accepted:{expected}")
    try:
        data = json.loads(result.content)
    except (ValueError, TypeError):
        raise _ReviewFailure("invalid_tool_result") from None
    if data != {"error": expected}:
        raise _ReviewFailure(f"wrong_category:{expected}")


def _expect_workcopy_error(expected: str, action: Callable[[], object]) -> None:
    try:
        action()
    except WorkCopyError as error:
        if error.code != expected:
            raise _ReviewFailure(f"wrong_category:{expected}:{error.code}") from None
    else:
        raise _ReviewFailure(f"accepted:{expected}")


def _agent_bad_file_path(root: Path, bad_path: str) -> None:
    work = _agent_workcopy(root)
    # A single refusal suffices; avoid scanning or dispatching a grep worker.
    _expect_file_error("invalid_path", work.path, "write_file", path=bad_path, content="changed\n")
    if (work.path / "source.py").read_bytes() != _AGENT_ORIGINAL:
        raise _ReviewFailure("copy_bytes_changed")
    if (work.project_root / "source.py").read_bytes() != _AGENT_ORIGINAL:
        raise _ReviewFailure("project_bytes_changed")


def _case_agent_file_backslash_parent(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    _agent_bad_file_path(root, r"..\escape.py")


def _case_agent_file_absolute(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    _agent_bad_file_path(root, str(root / "absolute.py"))


def _case_agent_file_unc(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    # Only a rejected string: the review never attempts to resolve this network path.
    _agent_bad_file_path(root, r"\\review-host\share\escape.py")


def _case_agent_file_drive_relative(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    _agent_bad_file_path(root, "C:escape.py")


def _case_agent_file_device_con(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    _agent_bad_file_path(root, "CON.py")


def _case_agent_file_device_nul(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    _agent_bad_file_path(root, "NUL.py")


def _case_agent_file_device_com1(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    _agent_bad_file_path(root, "COM1.py")


def _case_agent_file_device_aux(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    _agent_bad_file_path(root, "AUX.py")


def _case_agent_file_ads(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    _agent_bad_file_path(root, "file.txt:stream")


def _case_agent_file_symlink(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    work = _agent_workcopy(root)
    outside = root / "outside.py"
    outside.write_bytes(_AGENT_OUTSIDE)
    try:
        (work.path / "linked.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        raise _BlockedCapability("symlink_unavailable") from None
    _expect_file_error("unsafe_path", work.path, "write_file", path="linked.py", content="changed\n")
    _expect_file_error("unsafe_path", work.path, "read_file", path="linked.py")
    if work.changeset() != {}:
        raise _ReviewFailure("linked_file_in_changeset")
    work.discard()
    if outside.read_bytes() != _AGENT_OUTSIDE:
        raise _ReviewFailure("outside_bytes_changed")


def _junction_primitive(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        # POSIX's directory symlink exercises the same no-follow boundary.
        link.symlink_to(target, target_is_directory=True)
        return

    # Create an actual Windows mount-point junction without starting cmd.exe.
    import ctypes
    from ctypes import wintypes
    import struct

    link.mkdir()
    substitute = ("\\??\\" + str(target)).encode("utf-16le")
    display = str(target).encode("utf-16le")
    names = substitute + b"\x00\x00" + display + b"\x00\x00"
    data = struct.pack(
        "<IHHHHHH", 0xA0000003, 8 + len(names), 0,
        0, len(substitute), len(substitute) + 2, len(display),
    ) + names
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(str(link), 0x40000000, 7, None, 3, 0x02200000, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise OSError("junction primitive unavailable")
    try:
        kernel32.DeviceIoControl.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        )
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        buffer = ctypes.create_string_buffer(data)
        written = wintypes.DWORD(0)
        if not kernel32.DeviceIoControl(
            handle, 0x000900A4, buffer, len(data), None, 0,
            ctypes.byref(written), None,
        ):
            raise OSError("junction primitive unavailable")
    finally:
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)


def _make_directory_junction(link: Path, target: Path) -> None:
    try:
        _junction_primitive(link, target)
    except (OSError, NotImplementedError, AttributeError):
        raise _BlockedCapability("junction_unavailable") from None


def _case_agent_file_junction(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    work = _agent_workcopy(root)
    outside = root / "outside"
    outside.mkdir()
    (outside / "secret.py").write_bytes(_AGENT_OUTSIDE)
    _make_directory_junction(work.path / "linked", outside)
    _expect_file_error("unsafe_path", work.path, "read_file", path="linked/secret.py")
    _expect_file_error("unsafe_path", work.path, "write_file", path="linked/new.py", content="changed\n")
    _expect_file_error("unsafe_path", work.path, "list_dir", path="linked")
    if work.changeset() != {}:
        raise _ReviewFailure("junction_in_changeset")
    work.discard()
    if (outside / "secret.py").read_bytes() != _AGENT_OUTSIDE or (outside / "new.py").exists():
        raise _ReviewFailure("outside_bytes_changed")


def _case_agent_accept_root_binding(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    work = _agent_workcopy(root)
    (work.path / "source.py").write_bytes(_AGENT_UPDATED)
    if set(work.changeset()) != {"source.py"}:
        raise _ReviewFailure("changeset_missing")
    original_root = work.project_root
    another_root = root / "other-project"
    another_root.mkdir()
    (another_root / "source.py").write_bytes(_AGENT_ORIGINAL)
    # A work copy may never be rebound to a second project after creation.
    work.project_root = another_root
    _expect_workcopy_error("stale_root", lambda: work.accept(["source.py"], confirmed=True))
    if (another_root / "source.py").read_bytes() != _AGENT_ORIGINAL:
        raise _ReviewFailure("other_root_changed")
    if (original_root / "source.py").read_bytes() != _AGENT_ORIGINAL:
        raise _ReviewFailure("original_root_changed")


def _case_agent_accept_link_swap(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    work = _agent_workcopy(root)
    (work.path / "source.py").write_bytes(_AGENT_UPDATED)
    if set(work.changeset()) != {"source.py"}:
        raise _ReviewFailure("changeset_missing")
    outside = root / "outside.py"
    # Matching original bytes force the acceptance guard to check file identity,
    # rather than accidentally passing merely because the contents differ.
    outside.write_bytes(_AGENT_ORIGINAL)
    target = work.project_root / "source.py"
    target.unlink()
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        raise _BlockedCapability("symlink_unavailable") from None
    _expect_workcopy_error("unsafe_path", lambda: work.accept(["source.py"], confirmed=True))
    if outside.read_bytes() != _AGENT_ORIGINAL or not target.is_symlink():
        raise _ReviewFailure("swapped_target_changed")


def _case_agent_undo_exact_bytes(root: Path, _repository: ExecutionRepository, _boundary: ArtifactBoundary, _job_id: str) -> None:
    work = _agent_workcopy(root)
    (work.path / "source.py").write_bytes(_AGENT_UPDATED)
    (work.path / "deleted.py").unlink()
    (work.path / "added.py").write_bytes(_AGENT_ADDED)
    selected = ("added.py", "deleted.py", "source.py")
    if tuple(sorted(work.changeset())) != selected:
        raise _ReviewFailure("changeset_missing")
    if work.accept(selected, confirmed=True) != selected:
        raise _ReviewFailure("accept_missing")
    if (
        (work.project_root / "source.py").read_bytes() != _AGENT_UPDATED
        or (work.project_root / "deleted.py").exists()
        or (work.project_root / "added.py").read_bytes() != _AGENT_ADDED
    ):
        raise _ReviewFailure("accept_bytes_changed")
    if work.undo(confirmed=True) != selected:
        raise _ReviewFailure("undo_missing")
    if (
        (work.project_root / "source.py").read_bytes() != _AGENT_ORIGINAL
        or (work.project_root / "deleted.py").read_bytes() != _AGENT_DELETED
        or (work.project_root / "added.py").exists()
    ):
        raise _ReviewFailure("undo_bytes_changed")
    work.discard()

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
    ("agent_file_backslash_parent", _case_agent_file_backslash_parent),
    ("agent_file_absolute", _case_agent_file_absolute),
    ("agent_file_unc", _case_agent_file_unc),
    ("agent_file_drive_relative", _case_agent_file_drive_relative),
    ("agent_file_device_con", _case_agent_file_device_con),
    ("agent_file_device_nul", _case_agent_file_device_nul),
    ("agent_file_device_com1", _case_agent_file_device_com1),
    ("agent_file_device_aux", _case_agent_file_device_aux),
    ("agent_file_ads", _case_agent_file_ads),
    ("agent_file_symlink", _case_agent_file_symlink),
    ("agent_file_junction", _case_agent_file_junction),
    ("agent_accept_root_binding", _case_agent_accept_root_binding),
    ("agent_accept_link_swap", _case_agent_accept_link_swap),
    ("agent_undo_exact_bytes", _case_agent_undo_exact_bytes),
)

# The shared setup every case runs through. A change here changes what each
# case exercises, so it is part of the digest too.
_HARNESS: tuple[Callable[..., object], ...] = (
    _fixture, _grant, _expect_boundary_error,
    _agent_workcopy, _file_result, _expect_file_error, _expect_workcopy_error,
    _agent_bad_file_path, _junction_primitive, _make_directory_junction,
)


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
