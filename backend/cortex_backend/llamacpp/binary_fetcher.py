"""Fetch, verify, and cache the pinned llama-server binary.

Mirrors ``packaging/prepare_webview2.ps1``'s download-to-temp -> verify ->
atomic-move shape, but verifies against a pinned SHA-256 (llama.cpp releases
aren't code-signed, unlike the Microsoft-signed WebView2 bootstrapper) rather
than an Authenticode signature.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx

from .binary_release import AssetSpec, GpuBackend, PinnedRelease
from .errors import BinaryVerificationError

logger = logging.getLogger(__name__)

_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hash_directory(root: Path) -> str:
    """Deterministic manifest hash over every regular file's relative path + content.

    llama.cpp's Windows release layout ships each ``.exe`` as a tiny stub
    that dynamically loads a same-directory ``-impl.dll`` plus shared
    ``ggml-*.dll`` backend libraries -- hashing only the entry-point exe
    would leave the files that actually contain the executable logic
    unverified, so the trust anchor covers the whole extracted directory.
    """
    digest = hashlib.sha256()
    for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


_TreeIdentity = tuple[tuple[str, int, int], ...]


def _tree_identity(root: Path) -> _TreeIdentity:
    """Cheap per-file ``(relative_path, size, mtime_ns)`` fingerprint of a tree.

    Stat-ing every file is orders of magnitude cheaper than hashing their
    content, so ``_verify_directory`` uses this to detect "nothing changed"
    and skip the expensive ``hash_directory`` walk -- mirroring the
    stat-based memoization ``GGUFModelDirectory`` uses for the same reason.
    """
    return tuple(
        sorted(
            (path.relative_to(root).as_posix(), (stat := path.stat()).st_size, stat.st_mtime_ns)
            for path in root.rglob("*")
            if path.is_file()
        )
    )


class BinaryFetcher:
    """Downloads and caches the pinned llama-server binary for one GPU backend."""

    def __init__(self, runtime_dir: Path, *, http_client: httpx.Client | None = None) -> None:
        self._runtime_dir = runtime_dir
        self._http = http_client
        self._verification_cache: dict[Path, tuple[_TreeIdentity, bool]] = {}

    def _verify_directory(
        self, target_dir: Path, asset: AssetSpec, *, force_hash: bool = False
    ) -> bool:
        """Re-verify the whole extracted directory against the pinned manifest hash.

        Run before every launch (not just once after download) so a corrupted or
        tampered-with cached install is caught rather than trusted forever. This
        isn't a single-file TOCTOU-resistant stat-hash-stat check (a multi-file
        tree walk can't be made atomic that cheaply); it is still a large
        improvement over trusting an unverified cache indefinitely. ``force_hash``
        bypasses the stat-based memoization for the launch boundary, because a
        local replacement can preserve both size and mtime.

        The full SHA-256 walk (``hash_directory``) only actually runs when the
        tree's cheap ``_tree_identity`` fingerprint has changed since the last
        call -- this check runs on every /api/v1/system poll (every 2s while a
        GGUF model is selected -- see App.tsx), and re-hashing a ~100MB,
        unchanged runtime directory on every idle poll was pure wasted CPU
        and disk I/O.
        """
        if not (target_dir / asset.executable_relpath).is_file():
            return False
        try:
            identity = _tree_identity(target_dir)
            cached = self._verification_cache.get(target_dir)
            if not force_hash and cached is not None and cached[0] == identity:
                return cached[1]
            result = hash_directory(target_dir) == asset.directory_sha256
            self._verification_cache[target_dir] = (identity, result)
            return result
        except (OSError, MemoryError):
            # This check runs on every /api/v1/system poll (every 2s while a
            # GGUF model is selected -- see App.tsx), including while a large
            # local model is loaded and system memory is under real pressure.
            # MemoryError is not an OSError subclass, so without this it was
            # escaping uncaught and 500ing the whole system-status endpoint in
            # a tight, permanent poll loop instead of just reporting "not
            # verified as cached" the way a disk-read OSError already does.
            return False

    def is_cached(self, release: PinnedRelease, backend: GpuBackend) -> bool:
        asset = release.assets[backend]
        return self._verify_directory(self._target_dir(release, backend), asset)

    def ensure_binary(self, release: PinnedRelease, backend: GpuBackend) -> Path:
        """Return a verified llama-server.exe path, downloading on first use."""
        asset = release.assets[backend]
        target_dir = self._target_dir(release, backend)
        exe_path = target_dir / asset.executable_relpath
        # This is the trust decision immediately before the path is returned
        # to the process launcher. A stat-only identity can be forged by an
        # ordinary local replacement (same size, restored mtime), so launch
        # must always perform the pinned content hash. Status polling still
        # uses the cheap memoized path through is_cached().
        if self._verify_directory(target_dir, asset, force_hash=True):
            return exe_path

        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        archive_tmp = self._runtime_dir / f".download-{uuid4().hex}"
        extract_tmp = self._runtime_dir / f".extract-{uuid4().hex}"
        try:
            self._download(release.download_url(backend), archive_tmp, asset.archive_sha256)
            self._extract(archive_tmp, extract_tmp)
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(extract_tmp, target_dir)
        finally:
            archive_tmp.unlink(missing_ok=True)
            if extract_tmp.exists():
                shutil.rmtree(extract_tmp, ignore_errors=True)

        if not self._verify_directory(target_dir, asset, force_hash=True):
            raise BinaryVerificationError(
                f"Downloaded llama.cpp binary for '{backend}' failed verification."
            )
        return exe_path

    def _target_dir(self, release: PinnedRelease, backend: GpuBackend) -> Path:
        return self._runtime_dir / f"{release.tag}-{backend}"

    def _download(self, url: str, destination: Path, expected_sha256: str) -> None:
        digest = hashlib.sha256()
        client = self._http or httpx
        try:
            with client.stream("GET", url, follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT) as response:
                response.raise_for_status()
                with destination.open("wb") as handle:
                    for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_BYTES):
                        handle.write(chunk)
                        digest.update(chunk)
        except httpx.HTTPError as exc:
            destination.unlink(missing_ok=True)
            raise BinaryVerificationError(
                "Could not download the local model runtime. Check your network connection and try again."
            ) from exc
        if digest.hexdigest() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise BinaryVerificationError(
                "Downloaded llama.cpp archive failed checksum verification."
            )

    @staticmethod
    def _extract(archive_path: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        resolved_destination = destination.resolve()
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                member_path = (destination / member.filename).resolve()
                if resolved_destination not in member_path.parents and member_path != resolved_destination:
                    raise BinaryVerificationError("Archive entry escaped the extraction directory.")
            archive.extractall(destination)
        # llama.cpp release zips extract into a single top-level folder;
        # flatten so llama-server.exe lands directly at
        # <destination>/llama-server.exe regardless of that folder's name.
        entries = list(destination.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            inner = entries[0]
            for item in inner.iterdir():
                item.rename(destination / item.name)
            inner.rmdir()
