"""Trusted artifact copy-in, validation, quarantine, and publication.

This module is the only boundary that may move a user-selected file into the
execution artifact store or publish a guest output.  It accepts explicit source
grants, reads files with identity/size/hash checks, MIME-sniffs bytes instead of
trusting extensions or model claims, and publishes only after every output has
passed validation.  It never executes, decodes, or overwrites a source file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from .models import ExecutionArtifact
from .repository import ExecutionRepository, ExecutionRepositoryError


MAX_ARTIFACT_PATH_CHARS = 4096
MAX_OUTPUT_COUNT = 16
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_MIME = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,31}/[a-z0-9][a-z0-9.+-]{0,63}$")
_SAFE_RELATIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_CONTROL_BYTES = frozenset(range(0, 9)) | frozenset(range(11, 13)) | frozenset(range(14, 32))
_ARCHIVE_MAGICS = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"Rar!\x1a\x07",
    b"7z\xbc\xaf\x27\x1c",
    b"\x1f\x8b",
)


class ArtifactBoundaryError(ValueError):
    """Stable artifact boundary failure without paths or raw OS details."""

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            raise ValueError("invalid artifact boundary code")
        self.code = code
        super().__init__("The artifact could not be transferred safely.")


class _NonFiniteJSON(ValueError):
    """Internal marker for JSON extensions that represent non-finite numbers."""


@dataclass(frozen=True, slots=True)
class ArtifactSourceGrant:
    """Authenticated, one-turn authorization for one explicit source path."""

    owner: str
    job_id: str
    source_turn_id: str
    source_path: Path

    def __post_init__(self) -> None:
        for value in (self.owner, self.job_id, self.source_turn_id):
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise ValueError("artifact source grant identifier is invalid")
        object.__setattr__(self, "source_path", Path(self.source_path))


@dataclass(frozen=True, slots=True)
class OutputClaim:
    """Provider declaration for one relative output in its private staging root."""

    relative_path: str
    mime_type: str | None = None

    def __post_init__(self) -> None:
        parts = self.relative_path.split("/") if isinstance(self.relative_path, str) else []
        if (
            not isinstance(self.relative_path, str)
            or len(self.relative_path) == 0
            or len(self.relative_path) > 256
            or _SAFE_RELATIVE.fullmatch(self.relative_path) is None
            or "\\" in self.relative_path
            or ":" in self.relative_path
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("artifact output path is invalid")
        if self.mime_type is not None and _SAFE_MIME.fullmatch(self.mime_type) is None:
            raise ValueError("artifact output MIME type is invalid")


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    artifact: ExecutionArtifact
    relative_path: str
    mime_type: str


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _has_reparse_parent(path: Path) -> bool:
    try:
        return any(parent.exists() and _is_reparse_point(parent) for parent in path.parents)
    except OSError:
        return True


def _validate_absolute_path(path: Path) -> None:
    text = str(path)
    if (
        not path.is_absolute()
        or len(text) > MAX_ARTIFACT_PATH_CHARS
        or "\x00" in text
        or any(":" in part for part in path.parts[1:])
    ):
        raise ArtifactBoundaryError("artifact_path_invalid")


def _validate_components(path: Path) -> None:
    """Reject links/reparse points in every existing parent component."""

    try:
        for component in reversed(path.parents):
            if component.exists() and _is_reparse_point(component):
                raise ArtifactBoundaryError("artifact_reparse_point")
        if _is_reparse_point(path):
            raise ArtifactBoundaryError("artifact_reparse_point")
    except ArtifactBoundaryError:
        raise
    except OSError:
        raise ArtifactBoundaryError("artifact_path_unavailable") from None


def _is_sparse(info: os.stat_result) -> bool:
    sparse_flag = getattr(stat, "FILE_ATTRIBUTE_SPARSE_FILE", 0x200)
    if int(getattr(info, "st_file_attributes", 0)) & sparse_flag:
        return True
    blocks = int(getattr(info, "st_blocks", 0))
    return blocks > 0 and int(info.st_size) > blocks * 512


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        info = path.lstat()
    except OSError:
        raise ArtifactBoundaryError("artifact_source_unavailable") from None
    if _is_reparse_point(path):
        raise ArtifactBoundaryError("artifact_reparse_point")
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactBoundaryError("artifact_not_regular_file")
    if int(getattr(info, "st_nlink", 1)) != 1:
        raise ArtifactBoundaryError("artifact_hardlink_rejected")
    if _is_sparse(info):
        raise ArtifactBoundaryError("artifact_sparse_file")
    return (
        int(getattr(info, "st_dev", 0)),
        int(getattr(info, "st_ino", 0)),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", 0)),
        int(getattr(info, "st_ctime_ns", 0)),
    )


def _read_stable(path: Path, maximum: int) -> bytes:
    before = _file_identity(path)
    if before[2] > maximum:
        raise ArtifactBoundaryError("artifact_too_large")
    try:
        with path.open("rb") as stream:
            content = stream.read(maximum + 1)
    except OSError:
        raise ArtifactBoundaryError("artifact_source_unavailable") from None
    after = _file_identity(path)
    if before != after or len(content) > maximum:
        raise ArtifactBoundaryError("artifact_source_changed")
    return content


def _is_printable_text(content: bytes) -> bool:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return not any(ord(character) in _CONTROL_BYTES for character in text)


def _reject_json_constant(_value: str) -> Any:
    """Keep non-standard NaN/Infinity JSON extensions outside the boundary."""

    raise _NonFiniteJSON("non-finite JSON number")


def sniff_artifact_mime(content: bytes) -> str:
    """Return a conservative MIME type or reject active/archive content."""

    if not isinstance(content, bytes):
        raise ArtifactBoundaryError("invalid_artifact")
    prefix = content[:4096]
    lower = prefix.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"MZ") or content.startswith(b"\x7fELF"):
        raise ArtifactBoundaryError("invalid_artifact")
    if content.startswith(b"\x4c\x00\x00\x00") or content.startswith(b"\xd0\xcf\x11\xe0"):
        raise ArtifactBoundaryError("invalid_artifact")
    if content.startswith(b"#!") or lower.startswith(b"[internetshortcut]"):
        raise ArtifactBoundaryError("invalid_artifact")
    if any(content.startswith(magic) for magic in _ARCHIVE_MAGICS) or content[257:262] == b"ustar":
        raise ArtifactBoundaryError("invalid_artifact")
    if (
        lower.startswith((b"<!doctype html", b"<html", b"<script", b"<svg", b"javascript:"))
        or b"<svg" in lower[:1024]
    ):
        raise ArtifactBoundaryError("invalid_artifact")
    if lower.startswith((b"@echo off", b"powershell ", b"cmd.exe ")):
        raise ArtifactBoundaryError("invalid_artifact")
    if _is_printable_text(content):
        try:
            parsed = json.loads(content.decode("utf-8"), parse_constant=_reject_json_constant)
        except _NonFiniteJSON:
            raise ArtifactBoundaryError("invalid_artifact") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "text/plain"
        if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
            return "application/json"
    return "application/octet-stream"


class ArtifactBoundary:
    """Owner-scoped copy-in and all-or-nothing output publication."""

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        max_input_bytes: int | None = None,
        max_output_count: int = MAX_OUTPUT_COUNT,
        max_total_output_bytes: int | None = None,
    ) -> None:
        if not isinstance(repository, ExecutionRepository):
            raise TypeError("repository must be an ExecutionRepository")
        maximum = repository.max_artifact_bytes if max_input_bytes is None else max_input_bytes
        if not 1 <= maximum <= repository.max_artifact_bytes:
            raise ValueError("max_input_bytes is invalid")
        if not 1 <= max_output_count <= MAX_OUTPUT_COUNT:
            raise ValueError("max_output_count is invalid")
        total = repository.max_artifact_bytes * max_output_count
        if max_total_output_bytes is not None:
            if not 1 <= max_total_output_bytes <= total:
                raise ValueError("max_total_output_bytes is invalid")
            total = max_total_output_bytes
        self.repository = repository
        self.max_input_bytes = maximum
        self.max_output_count = max_output_count
        self.max_total_output_bytes = total
        self.quarantine_root = repository.artifact_root / ".artifact_quarantine"
        try:
            if _is_reparse_point(repository.artifact_root) or _has_reparse_parent(repository.artifact_root):
                raise ArtifactBoundaryError("artifact_root_unavailable")
            self.quarantine_root.mkdir(parents=True, exist_ok=True)
            if _is_reparse_point(self.quarantine_root):
                raise ArtifactBoundaryError("artifact_root_unavailable")
        except ArtifactBoundaryError:
            raise
        except OSError:
            raise ArtifactBoundaryError("artifact_root_unavailable") from None

    def _authorize_job(self, job_id: str, owner: str) -> None:
        if self.repository.get_job(job_id, owner=owner) is None:
            raise ArtifactBoundaryError("artifact_owner_mismatch")

    @staticmethod
    def _generated_name(digest: str) -> str:
        return f"artifact-{digest[:32]}"

    def copy_in(
        self,
        grant: ArtifactSourceGrant,
        *,
        retention_seconds: int = 86_400,
    ) -> ExecutionArtifact:
        """Copy one explicitly granted source into the owner-scoped artifact store."""

        self._validate_retention(retention_seconds)
        self._authorize_job(grant.job_id, grant.owner)
        source = grant.source_path
        _validate_absolute_path(source)
        _validate_components(source)
        content = _read_stable(source, self.max_input_bytes)
        mime_type = sniff_artifact_mime(content)
        digest = sha256(content).hexdigest()
        try:
            return self.repository.publish_artifact(
                grant.job_id,
                name=self._generated_name(digest),
                content=content,
                mime_type=mime_type,
                retention_seconds=retention_seconds,
            )
        except ExecutionRepositoryError:
            raise ArtifactBoundaryError("artifact_publish_failed") from None

    @staticmethod
    def _validate_retention(retention_seconds: int) -> None:
        if (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or retention_seconds <= 0
        ):
            raise ArtifactBoundaryError("artifact_retention_invalid")

    def _output_root(self, root: str | os.PathLike[str]) -> Path:
        value = Path(root)
        _validate_absolute_path(value)
        _validate_components(value)
        try:
            resolved = value.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ArtifactBoundaryError("artifact_output_unavailable") from None
        if not resolved.is_dir() or _is_reparse_point(resolved):
            raise ArtifactBoundaryError("artifact_output_unavailable")
        return resolved

    @staticmethod
    def _claim_path(root: Path, claim: OutputClaim) -> Path:
        candidate = root / claim.relative_path
        cursor = root
        for component in Path(claim.relative_path).parts:
            cursor = cursor / component
            if _is_reparse_point(cursor):
                raise ArtifactBoundaryError("artifact_reparse_point")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ArtifactBoundaryError("artifact_output_unavailable") from None
        if not resolved.is_relative_to(root) or _is_reparse_point(candidate):
            raise ArtifactBoundaryError("artifact_path_invalid")
        return candidate

    def _quarantine(self, root: Path, path: Path) -> None:
        if _is_reparse_point(path):
            raise ArtifactBoundaryError("artifact_cleanup_pending")
        quarantine = root / ".quarantine"
        try:
            if _is_reparse_point(quarantine):
                raise ArtifactBoundaryError("artifact_cleanup_pending")
            quarantine.mkdir(parents=True, exist_ok=True)
            if _is_reparse_point(quarantine):
                raise ArtifactBoundaryError("artifact_cleanup_pending")
            target = quarantine / f"artifact-{secrets.token_hex(16)}"
            os.replace(path, target)
        except ArtifactBoundaryError:
            raise
        except OSError:
            raise ArtifactBoundaryError("artifact_cleanup_pending") from None

    def _output_files(self, root: Path) -> list[tuple[str, Path]]:
        found: list[tuple[str, Path]] = []
        try:
            for path in root.rglob("*"):
                relative = path.relative_to(root).as_posix()
                if _is_reparse_point(path):
                    raise ArtifactBoundaryError("artifact_reparse_point")
                if relative == ".quarantine" or relative.startswith(".quarantine/"):
                    continue
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise ArtifactBoundaryError("artifact_not_regular_file")
                found.append((relative, path))
        except ArtifactBoundaryError:
            raise
        except OSError:
            raise ArtifactBoundaryError("artifact_output_unavailable") from None
        return found

    def collect_outputs(
        self,
        job_id: str,
        owner: str,
        output_root: str | os.PathLike[str],
        claims: Sequence[OutputClaim],
        *,
        retention_seconds: int = 86_400,
    ) -> tuple[PublishedArtifact, ...]:
        """Validate every declared output before publishing any artifact."""

        self._validate_retention(retention_seconds)
        self._authorize_job(job_id, owner)
        if not isinstance(claims, Sequence) or not claims or len(claims) > self.max_output_count:
            raise ArtifactBoundaryError("artifact_output_count_invalid")
        if any(not isinstance(claim, OutputClaim) for claim in claims):
            raise ArtifactBoundaryError("artifact_output_claim_invalid")
        claim_map: dict[str, OutputClaim] = {}
        for claim in claims:
            if claim.relative_path in claim_map:
                raise ArtifactBoundaryError("artifact_output_claim_invalid")
            claim_map[claim.relative_path] = claim
        root = self._output_root(output_root)
        files = self._output_files(root)
        if {relative for relative, _ in files} != set(claim_map):
            for relative, path in files:
                if relative not in claim_map:
                    self._quarantine(root, path)
            raise ArtifactBoundaryError("artifact_unclaimed_output")
        for claim in claims:
            self._claim_path(root, claim)
        prepared: list[tuple[OutputClaim, Path, bytes, str, str]] = []
        total = 0
        for relative, path in files:
            claim = claim_map[relative]
            try:
                content = _read_stable(path, self.repository.max_artifact_bytes)
                mime_type = sniff_artifact_mime(content)
                if claim.mime_type is not None and claim.mime_type != mime_type:
                    raise ArtifactBoundaryError("artifact_mime_mismatch")
            except ArtifactBoundaryError:
                self._quarantine(root, path)
                raise
            total += len(content)
            if total > self.max_total_output_bytes:
                self._quarantine(root, path)
                raise ArtifactBoundaryError("artifact_output_limit")
            prepared.append((claim, path, content, mime_type, sha256(content).hexdigest()))
        published: list[PublishedArtifact] = []
        try:
            for claim, _path, content, mime_type, digest in prepared:
                artifact = self.repository.publish_artifact(
                    job_id,
                    name=self._generated_name(digest),
                    content=content,
                    mime_type=mime_type,
                    retention_seconds=retention_seconds,
                )
                published.append(
                    PublishedArtifact(
                        artifact=artifact,
                        relative_path=claim.relative_path,
                        mime_type=mime_type,
                    )
                )
        except ExecutionRepositoryError:
            try:
                for item in published:
                    self.repository.delete_artifact(item.artifact.artifact_id)
                for _claim, path, _content, _mime, _digest in prepared:
                    if path.exists() or path.is_symlink():
                        self._quarantine(root, path)
            except (ExecutionRepositoryError, OSError):
                raise ArtifactBoundaryError("artifact_cleanup_pending") from None
            raise ArtifactBoundaryError("artifact_publish_failed") from None
        try:
            for _claim, path, _content, _mime, _digest in prepared:
                path.unlink()
        except OSError:
            try:
                for item in published:
                    self.repository.delete_artifact(item.artifact.artifact_id)
            except ExecutionRepositoryError:
                raise ArtifactBoundaryError("artifact_cleanup_pending") from None
            raise ArtifactBoundaryError("artifact_cleanup_pending") from None
        return tuple(published)


__all__ = [
    "ArtifactBoundary",
    "ArtifactBoundaryError",
    "ArtifactSourceGrant",
    "MAX_ARTIFACT_PATH_CHARS",
    "MAX_OUTPUT_COUNT",
    "OutputClaim",
    "PublishedArtifact",
    "sniff_artifact_mime",
]
