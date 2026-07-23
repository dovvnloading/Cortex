"""Fail-closed signing metadata for the packaged recipe worker.

This module is a release-tool boundary, not a runtime capability. It turns an
already-built one-folder worker into a signed ``recipe.manifest.v1`` payload by
hashing every ordinary package file. Only ``recipe_worker.exe`` receives the
``image_transform`` role; all other files are inert ``resource`` entries so the
installer's exact-tree check can preserve the PyInstaller dependency closure.
Private key bytes are accepted only from an external caller and are never
persisted or returned.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Final, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from pydantic import ValidationError

from .manifest import (
    MAX_BUNDLE_ENTRY_BYTES,
    MAX_MANIFEST_ENTRIES,
    ManifestEntry,
    TrustedRecipeKeys,
    parse_signed_manifest,
    verify_manifest_signature,
)


EXPECTED_WORKER_PATH: Final[str] = "recipe_worker.exe"
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_KEY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SAFE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class WorkerReleaseError(ValueError):
    """Stable release failure category without paths or key material."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid worker release code")
        self.code = code
        super().__init__("The recipe worker release artifact failed closed.")


@dataclass(frozen=True, slots=True)
class SignedWorkerRelease:
    """Safe release metadata; private key material is intentionally absent."""

    manifest: dict[str, Any]
    manifest_digest: str
    entry_count: int
    worker_size: int
    worker_sha256: str


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _path_has_reparse_component(path: Path) -> bool:
    """Reject symlink/junction parents, not only a reparse leaf."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if _is_reparse_point(current):
            return True
    return False


def _ordinary_root(source_root: str | os.PathLike[str]) -> Path:
    try:
        candidate = Path(source_root)
    except (TypeError, ValueError):
        raise WorkerReleaseError("release_root_invalid") from None
    if _path_has_reparse_component(candidate):
        raise WorkerReleaseError("release_root_invalid")
    try:
        root = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorkerReleaseError("release_root_unavailable") from None
    if _is_reparse_point(root) or not root.is_dir():
        raise WorkerReleaseError("release_root_invalid")
    return root


def _ordinary_file(path: Path) -> tuple[int, int, int, int, int]:
    if _path_has_reparse_component(path):
        raise WorkerReleaseError("release_entry_reparse")
    try:
        stat_result = path.lstat()
    except OSError:
        raise WorkerReleaseError("release_entry_unavailable") from None
    if stat.S_ISLNK(stat_result.st_mode) or not stat.S_ISREG(stat_result.st_mode):
        raise WorkerReleaseError("release_entry_invalid")
    if int(getattr(stat_result, "st_nlink", 1)) != 1:
        raise WorkerReleaseError("release_entry_invalid")
    if stat_result.st_size < 1 or stat_result.st_size > MAX_BUNDLE_ENTRY_BYTES:
        raise WorkerReleaseError("release_entry_size_invalid")
    return (
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
        int(stat_result.st_ctime_ns),
        int(getattr(stat_result, "st_ino", 0)),
        int(getattr(stat_result, "st_dev", 0)),
    )


def _digest_file(path: Path, expected_identity: tuple[int, int, int, int, int]) -> str:
    expected_size = expected_identity[0]
    digest = sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(min(1024 * 1024, expected_size + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    raise WorkerReleaseError("release_entry_changed")
                digest.update(chunk)
    except WorkerReleaseError:
        raise
    except OSError:
        raise WorkerReleaseError("release_entry_unavailable") from None
    after = _ordinary_file(path)
    if after != expected_identity or total != expected_size:
        raise WorkerReleaseError("release_entry_changed")
    return digest.hexdigest()


def _enumerate_files(root: Path) -> list[tuple[str, Path, int, str]]:
    files: list[tuple[str, Path, int, str]] = []
    try:
        candidates = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    except (OSError, RuntimeError):
        raise WorkerReleaseError("release_tree_unavailable") from None
    for candidate in candidates:
        if _is_reparse_point(candidate):
            raise WorkerReleaseError("release_entry_reparse")
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root).as_posix()
        if relative == "manifest.json":
            raise WorkerReleaseError("release_manifest_reserved")
        stat_identity = _ordinary_file(candidate)
        files.append((relative, candidate, stat_identity[0], _digest_file(candidate, stat_identity)))
    if not files or not any(relative == EXPECTED_WORKER_PATH for relative, *_ in files):
        raise WorkerReleaseError("release_worker_missing")
    if len(files) > MAX_MANIFEST_ENTRIES:
        raise WorkerReleaseError("release_entry_count_exceeded")
    return files


def _canonical_signed_payload(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError, OverflowError):
        raise WorkerReleaseError("release_manifest_invalid") from None


def build_signed_worker_manifest(
    source_root: str | os.PathLike[str],
    *,
    private_key_bytes: bytes,
    key_id: str,
    bundle_version: str,
    sequence: int,
) -> SignedWorkerRelease:
    """Hash every package file and return a self-verified signed manifest."""

    if not isinstance(private_key_bytes, bytes) or len(private_key_bytes) != 32:
        raise WorkerReleaseError("release_signing_key_invalid")
    if not isinstance(key_id, str) or _SAFE_KEY_ID.fullmatch(key_id) is None:
        raise WorkerReleaseError("release_key_id_invalid")
    if not isinstance(bundle_version, str) or _SAFE_VERSION.fullmatch(bundle_version) is None:
        raise WorkerReleaseError("release_version_invalid")
    if type(sequence) is not int or sequence < 1:
        raise WorkerReleaseError("release_sequence_invalid")
    try:
        signer = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    except (TypeError, ValueError):
        raise WorkerReleaseError("release_signing_key_invalid") from None

    root = _ordinary_root(source_root)
    files = _enumerate_files(root)
    entries: list[ManifestEntry] = []
    worker_size = 0
    worker_sha256 = ""
    try:
        for index, (relative, _path, size, digest) in enumerate(files, start=1):
            is_worker = relative == EXPECTED_WORKER_PATH
            if is_worker:
                worker_size = size
                worker_sha256 = digest
            entries.append(
                ManifestEntry(
                    recipe_id="image_transform" if is_worker else f"resource-{index:06d}",
                    bundle_path=relative,
                    entrypoint="image_transform" if is_worker else "resource",
                    version=bundle_version,
                    size=size,
                    sha256=digest,
                )
            )
    except (TypeError, ValueError, ValidationError):
        raise WorkerReleaseError("release_manifest_invalid") from None
    unsigned = {
        "schema_version": "recipe.manifest.v1",
        "key_id": key_id,
        "sequence": sequence,
        "bundle_version": bundle_version,
        "rollback_of": None,
        "entries": [entry.model_dump(mode="json") for entry in entries],
    }
    signature = base64.urlsafe_b64encode(signer.sign(_canonical_signed_payload(unsigned))).decode(
        "ascii"
    ).rstrip("=")
    payload = {**unsigned, "signature": signature}
    try:
        # Use the same guarded parser as the runtime verifier.  The wire format
        # carries ``entries`` as a JSON array while the immutable model stores
        # it as a tuple; validating through the parser keeps release and runtime
        # canonicalization exactly aligned.
        manifest = parse_signed_manifest(payload)
        public_key = signer.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        verified = verify_manifest_signature(payload, TrustedRecipeKeys({key_id: public_key}))
    except Exception:
        raise WorkerReleaseError("release_manifest_self_check_failed") from None
    if verified.manifest != manifest:
        raise WorkerReleaseError("release_manifest_self_check_failed")
    return SignedWorkerRelease(
        manifest=manifest.model_dump(mode="json"),
        manifest_digest=verified.digest,
        entry_count=len(entries),
        worker_size=worker_size,
        worker_sha256=worker_sha256,
    )


__all__ = [
    "EXPECTED_WORKER_PATH",
    "SignedWorkerRelease",
    "WorkerReleaseError",
    "build_signed_worker_manifest",
]
