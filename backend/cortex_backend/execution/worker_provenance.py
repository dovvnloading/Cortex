"""Storage-only binding of an installed signed bundle to the image worker role.

This module is intentionally not a launcher.  It accepts only an
``InstalledBundle`` returned by the signed installer, revalidates the immutable
generation, and proves that exactly one declared ``image_transform`` entry is the
fixed ``recipe_worker.exe`` file.  It never imports, loads, decodes, or executes
the worker.  Process isolation, native broker identity, resource limits, and
lifecycle enablement remain separate release gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Final

from .bundle_installer import (
    BundleInstallError,
    InstalledBundle,
    SignedBundleInstaller,
)
from .manifest import (
    MAX_BUNDLE_ENTRY_BYTES,
    MAX_MANIFEST_BYTES,
    ManifestVerificationError,
    parse_signed_manifest,
    verify_bundle_files,
)


EXPECTED_WORKER_PATH: Final[str] = "recipe_worker.exe"
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class WorkerProvenanceError(ValueError):
    """Stable worker provenance failure without paths or filesystem details."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid worker provenance code")
        self.code = code
        super().__init__("The installed recipe worker failed provenance verification safely.")


@dataclass(frozen=True, slots=True)
class VerifiedRecipeWorker:
    """Verified metadata for a worker file; no executable handle is retained."""

    bundle_root: Path
    bundle_digest: str
    key_id: str
    worker_path: str
    worker_sha256: str
    worker_size: int
    recipe_version: str


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _read_manifest(root: Path):
    manifest_path = root / "manifest.json"
    if _is_reparse_point(manifest_path):
        raise WorkerProvenanceError("worker_manifest_reparse")
    try:
        with manifest_path.open("rb") as stream:
            payload = stream.read(MAX_MANIFEST_BYTES + 1)
    except OSError:
        raise WorkerProvenanceError("worker_manifest_unavailable") from None
    if len(payload) > MAX_MANIFEST_BYTES:
        raise WorkerProvenanceError("worker_manifest_too_large")
    try:
        raw = json.loads(payload.decode("ascii"))
        return parse_signed_manifest(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ManifestVerificationError, ValueError):
        raise WorkerProvenanceError("worker_manifest_invalid") from None


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        stat = path.stat()
    except OSError:
        raise WorkerProvenanceError("worker_entrypoint_unavailable") from None
    if _is_reparse_point(path):
        raise WorkerProvenanceError("worker_entrypoint_reparse")
    if not path.is_file() or getattr(stat, "st_nlink", 1) != 1:
        raise WorkerProvenanceError("worker_entrypoint_invalid")
    return (
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
        int(getattr(stat, "st_ino", 0)),
    )


def _read_worker(path: Path, expected_size: int, expected_digest: str) -> None:
    before = _file_identity(path)
    if before[0] != expected_size or expected_size > MAX_BUNDLE_ENTRY_BYTES:
        raise WorkerProvenanceError("worker_entrypoint_size_mismatch")
    digest = sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(min(1024 * 1024, MAX_BUNDLE_ENTRY_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    raise WorkerProvenanceError("worker_entrypoint_size_mismatch")
                digest.update(chunk)
    except WorkerProvenanceError:
        raise
    except OSError:
        raise WorkerProvenanceError("worker_entrypoint_unavailable") from None
    after = _file_identity(path)
    if before != after or total != expected_size:
        raise WorkerProvenanceError("worker_entrypoint_changed")
    if digest.hexdigest() != expected_digest:
        raise WorkerProvenanceError("worker_entrypoint_hash_mismatch")


def verify_installed_worker(installed: InstalledBundle) -> VerifiedRecipeWorker:
    """Verify the exact image-worker role in an installer-returned generation."""

    if not isinstance(installed, InstalledBundle):
        raise TypeError("installed must be an InstalledBundle")
    root = installed.bundle_root
    if _is_reparse_point(root):
        raise WorkerProvenanceError("worker_bundle_reparse")
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorkerProvenanceError("worker_bundle_unavailable") from None
    if not root.is_dir():
        raise WorkerProvenanceError("worker_bundle_unavailable")

    manifest = _read_manifest(root)
    if manifest.manifest_digest() != installed.state.digest:
        raise WorkerProvenanceError("worker_manifest_mismatch")
    try:
        verify_bundle_files(manifest, root)
    except ManifestVerificationError:
        raise WorkerProvenanceError("worker_bundle_integrity_failed") from None

    worker_entries = [
        entry for entry in manifest.entries if entry.entrypoint == "image_transform"
    ]
    if len(worker_entries) == 0:
        raise WorkerProvenanceError("worker_role_missing")
    if len(worker_entries) != 1:
        raise WorkerProvenanceError("worker_role_ambiguous")
    entry = worker_entries[0]
    if entry.bundle_path != EXPECTED_WORKER_PATH:
        raise WorkerProvenanceError("worker_entrypoint_mismatch")

    worker_path = root / EXPECTED_WORKER_PATH
    try:
        if not worker_path.resolve(strict=True).is_relative_to(root):
            raise WorkerProvenanceError("worker_entrypoint_invalid")
    except (OSError, RuntimeError):
        raise WorkerProvenanceError("worker_entrypoint_unavailable") from None
    _read_worker(worker_path, entry.size, entry.sha256)
    return VerifiedRecipeWorker(
        bundle_root=root,
        bundle_digest=installed.state.digest,
        key_id=manifest.key_id,
        worker_path=EXPECTED_WORKER_PATH,
        worker_sha256=entry.sha256,
        worker_size=entry.size,
        recipe_version=entry.version,
    )


def verify_active_worker(installer: SignedBundleInstaller) -> VerifiedRecipeWorker:
    """Verify the active generation after the installer rechecks its signature."""

    if not isinstance(installer, SignedBundleInstaller):
        raise TypeError("installer must be a SignedBundleInstaller")
    try:
        installed = installer.status()
    except BundleInstallError:
        raise WorkerProvenanceError("worker_bundle_integrity_failed") from None
    if installed is None:
        raise WorkerProvenanceError("worker_bundle_unavailable")
    return verify_installed_worker(installed)


__all__ = [
    "EXPECTED_WORKER_PATH",
    "VerifiedRecipeWorker",
    "WorkerProvenanceError",
    "verify_active_worker",
    "verify_installed_worker",
]
