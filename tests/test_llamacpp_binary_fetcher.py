"""Tests for BinaryFetcher's download -> verify -> atomic-move -> extract flow.

Uses httpx.MockTransport so no real network call happens; a small in-memory
zip fixture stands in for a real llama.cpp release archive.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from cortex_backend.llamacpp.binary_fetcher import BinaryFetcher, hash_directory
from cortex_backend.llamacpp.binary_release import AssetSpec, PinnedRelease
from cortex_backend.llamacpp.errors import BinaryVerificationError

# A real llama.cpp Windows release ships each .exe as a tiny stub that
# dynamically loads a same-directory "-impl.dll" -- these fixtures mirror
# that shape so verification is actually exercised against more than one
# file per asset (the bug this test file used to miss: hashing only the
# stub would leave a tampered impl DLL undetected).
_EXE_CONTENT = b"fake llama-server.exe stub"
_IMPL_DLL_CONTENT = b"fake llama-server-impl.dll payload, much larger in reality"


def _build_archive(*, wrapped: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        prefix = "llama-cpp-bin/" if wrapped else ""
        archive.writestr(f"{prefix}llama-server.exe", _EXE_CONTENT)
        archive.writestr(f"{prefix}llama-server-impl.dll", _IMPL_DLL_CONTENT)
    return buffer.getvalue()


def _expected_directory_hash() -> str:
    """Hash a directory laid out exactly like BinaryFetcher's flattened extraction.

    Uses its own isolated temp directory (never the test's tmp_path) so this
    fixture setup can't leak a stray "llama-server.exe" into assertions that
    scan tmp_path for leftover files.
    """
    with tempfile.TemporaryDirectory() as scratch:
        extract_dir = Path(scratch) / "expected"
        extract_dir.mkdir()
        (extract_dir / "llama-server.exe").write_bytes(_EXE_CONTENT)
        (extract_dir / "llama-server-impl.dll").write_bytes(_IMPL_DLL_CONTENT)
        return hash_directory(extract_dir)


def _release_for(archive_bytes: bytes, *, filename: str = "llama-cpu.zip") -> PinnedRelease:
    return PinnedRelease(
        tag="b0001",
        assets={
            "cpu": AssetSpec(
                filename=filename,
                archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
                directory_sha256=_expected_directory_hash(),
            )
        },
    )


def _client_returning(content: bytes, *, status_code: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("wrapped", [True, False])
def test_ensure_binary_downloads_verifies_extracts_and_caches(tmp_path: Path, wrapped: bool) -> None:
    archive_bytes = _build_archive(wrapped=wrapped)
    release = _release_for(archive_bytes)
    fetcher = BinaryFetcher(tmp_path, http_client=_client_returning(archive_bytes))

    exe_path = fetcher.ensure_binary(release, "cpu")

    assert exe_path.is_file()
    assert exe_path.name == "llama-server.exe"
    assert exe_path.read_bytes() == _EXE_CONTENT
    assert (exe_path.parent / "llama-server-impl.dll").read_bytes() == _IMPL_DLL_CONTENT
    assert fetcher.is_cached(release, "cpu")
    # No leftover temp files.
    assert not any(p.name.startswith(".download-") or p.name.startswith(".extract-") for p in tmp_path.iterdir())


def test_ensure_binary_reuses_the_cache_without_a_second_download(tmp_path: Path) -> None:
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes)
    fetcher = BinaryFetcher(tmp_path, http_client=_client_returning(archive_bytes))
    fetcher.ensure_binary(release, "cpu")

    calls = {"count": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, content=archive_bytes)

    fetcher = BinaryFetcher(tmp_path, http_client=httpx.Client(transport=httpx.MockTransport(counting_handler)))
    fetcher.ensure_binary(release, "cpu")
    assert calls["count"] == 0


def test_corrupted_archive_is_rejected_and_leaves_no_partial_file(tmp_path: Path) -> None:
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes)
    # Server returns different bytes than the pinned archive hash expects.
    fetcher = BinaryFetcher(tmp_path, http_client=_client_returning(b"tampered archive bytes"))

    with pytest.raises(BinaryVerificationError):
        fetcher.ensure_binary(release, "cpu")

    assert not any(tmp_path.rglob("llama-server.exe"))
    assert not any(p.name.startswith(".download-") for p in tmp_path.iterdir())


def test_tampered_cached_exe_is_re_downloaded(tmp_path: Path) -> None:
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes)
    fetcher = BinaryFetcher(tmp_path, http_client=_client_returning(archive_bytes))
    exe_path = fetcher.ensure_binary(release, "cpu")

    exe_path.write_bytes(b"tampered after the fact")
    assert fetcher.is_cached(release, "cpu") is False

    # A fresh ensure_binary() call must detect the mismatch and re-fetch.
    fetcher2 = BinaryFetcher(tmp_path, http_client=_client_returning(archive_bytes))
    restored_path = fetcher2.ensure_binary(release, "cpu")
    assert restored_path.read_bytes() == _EXE_CONTENT


def test_tampered_companion_dll_is_detected_even_though_the_exe_stub_is_untouched(tmp_path: Path) -> None:
    """The scenario the old exe-only hash would have missed: the launched
    entry point is a tiny stub, and the actual code lives in a
    same-directory impl DLL -- tampering with that DLL alone must still
    fail verification."""
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes)
    fetcher = BinaryFetcher(tmp_path, http_client=_client_returning(archive_bytes))
    exe_path = fetcher.ensure_binary(release, "cpu")

    (exe_path.parent / "llama-server-impl.dll").write_bytes(b"tampered impl payload")
    assert fetcher.is_cached(release, "cpu") is False


def test_is_cached_reports_false_instead_of_raising_when_hashing_hits_memory_pressure(tmp_path: Path) -> None:
    # /api/v1/system polls is_cached() every 2s while a GGUF model is
    # selected, including while a large local model has system memory under
    # real pressure. MemoryError is not an OSError subclass, so it must be
    # handled explicitly -- otherwise it escapes this best-effort check
    # uncaught and 500s the whole system-status endpoint on every poll.
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes)
    fetcher = BinaryFetcher(tmp_path, http_client=_client_returning(archive_bytes))
    fetcher.ensure_binary(release, "cpu")

    with patch("cortex_backend.llamacpp.binary_fetcher.hash_directory", side_effect=MemoryError):
        assert fetcher.is_cached(release, "cpu") is False


def test_zip_slip_entries_are_rejected(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../evil.txt", b"escape attempt")
    archive_bytes = buffer.getvalue()
    release = _release_for(archive_bytes)
    fetcher = BinaryFetcher(tmp_path, http_client=_client_returning(archive_bytes))

    with pytest.raises(BinaryVerificationError):
        fetcher.ensure_binary(release, "cpu")
