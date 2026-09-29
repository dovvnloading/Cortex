"""Tests for BinaryFetcher's download -> verify -> atomic-move -> extract flow.

Uses httpx.MockTransport so no real network call happens; a small in-memory
zip fixture stands in for a real llama.cpp release archive.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

import cortex_backend.llamacpp.binary_fetcher as binary_fetcher_module
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


def _release_for(archive_bytes: bytes, *, filename: str = "llama-cpu.zip", tag: str = "b0001") -> PinnedRelease:
    return PinnedRelease(
        tag=tag,
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


def test_same_size_replacement_with_restored_mtime_is_re_downloaded(tmp_path: Path) -> None:
    """A forged stat identity must not bypass the launch-time hash check."""
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes)
    fetcher = BinaryFetcher(tmp_path, http_client=_client_returning(archive_bytes))
    exe_path = fetcher.ensure_binary(release, "cpu")
    original_stat = exe_path.stat()

    evil_content = b"E" * len(_EXE_CONTENT)
    assert len(evil_content) == original_stat.st_size
    exe_path.write_bytes(evil_content)
    os.utime(exe_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert exe_path.stat().st_size == original_stat.st_size
    assert exe_path.stat().st_mtime_ns == original_stat.st_mtime_ns
    # The cheap status-poll cache cannot distinguish this forged identity;
    # the launch path must still perform a fresh content verification.
    assert fetcher.is_cached(release, "cpu") is True

    restored_path = fetcher.ensure_binary(release, "cpu")

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
    exe_path = fetcher.ensure_binary(release, "cpu")

    # Touch a file so the cheap tree-identity cache misses and this call
    # actually reaches hash_directory -- otherwise the unchanged-directory
    # fast path would return the cached result without calling it at all.
    future = time.time() + 10
    os.utime(exe_path, (future, future))

    with patch("cortex_backend.llamacpp.binary_fetcher.hash_directory", side_effect=MemoryError):
        assert fetcher.is_cached(release, "cpu") is False


def test_is_cached_only_hashes_once_for_an_unchanged_directory(tmp_path: Path) -> None:
    """/api/v1/system polls is_cached() every ~2s while idle -- the full
    SHA-256 directory walk must be skipped when nothing on disk changed, and
    only re-run once a file actually changes."""
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes)
    fetcher = BinaryFetcher(tmp_path, http_client=_client_returning(archive_bytes))
    exe_path = fetcher.ensure_binary(release, "cpu")

    with patch(
        "cortex_backend.llamacpp.binary_fetcher.hash_directory", wraps=hash_directory
    ) as spy:
        # ensure_binary() already verified (and cached) this directory, so
        # a repeated is_cached() call for the same unchanged tree must not
        # re-hash it.
        assert fetcher.is_cached(release, "cpu") is True
        assert fetcher.is_cached(release, "cpu") is True
        assert spy.call_count == 0

        future = time.time() + 10
        os.utime(exe_path, (future, future))

        assert fetcher.is_cached(release, "cpu") is True
        assert spy.call_count == 1


def test_zip_slip_entries_are_rejected(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../evil.txt", b"escape attempt")
    archive_bytes = buffer.getvalue()
    release = _release_for(archive_bytes)
    fetcher = BinaryFetcher(tmp_path, http_client=_client_returning(archive_bytes))

    with pytest.raises(BinaryVerificationError):
        fetcher.ensure_binary(release, "cpu")


def test_download_rejects_advertised_content_length_over_ceiling(tmp_path: Path, monkeypatch) -> None:
    """A ``Content-Length`` beyond the byte ceiling must be rejected before a
    single byte of the body is read -- a misbehaving CDN, a corporate MITM
    proxy, or a redirect to something unrelated must not be able to fill the
    disk before the post-download SHA-256 check ever gets a chance to run.
    Mirrors download.py's equivalent "reject on the advertised size" test.
    """
    monkeypatch.setattr(binary_fetcher_module, "_MAX_BINARY_DOWNLOAD_BYTES", 1024)
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes)
    body_was_read = {"value": False}

    def body():
        body_was_read["value"] = True
        yield archive_bytes

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        # Advertise far more than the (patched) ceiling while keeping the
        # actual body small, so the assertion below proves the rejection
        # happened purely off the header, without ever reading the body.
        return httpx.Response(200, content=body(), headers={"Content-Length": str(10 * 1024 * 1024 * 1024)})

    fetcher = BinaryFetcher(tmp_path, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(BinaryVerificationError, match="byte ceiling"):
        fetcher.ensure_binary(release, "cpu")

    assert body_was_read["value"] is False
    assert not any(p.name.startswith(".download-") for p in tmp_path.iterdir())


def test_download_rejects_unbounded_body_over_ceiling_mid_stream(tmp_path: Path, monkeypatch) -> None:
    """No (or an untruthful) ``Content-Length`` must not bypass the ceiling:
    the running total accumulated during streaming is the backstop, and a
    download that blows past it mid-stream must be rejected with no partial
    archive left on disk."""
    monkeypatch.setattr(binary_fetcher_module, "_MAX_BINARY_DOWNLOAD_BYTES", 1024)
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes)
    oversized_body = b"x" * (2 * 1024)

    def body():
        # A generator body carries no Content-Length header (chunked
        # transfer), matching a server that never advertises a size.
        yield oversized_body

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=body())

    fetcher = BinaryFetcher(tmp_path, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(BinaryVerificationError, match="byte ceiling"):
        fetcher.ensure_binary(release, "cpu")

    assert not any(tmp_path.rglob("llama-server.exe"))
    assert not any(p.name.startswith(".download-") for p in tmp_path.iterdir())


def test_download_rejects_insufficient_free_disk_space(tmp_path: Path, monkeypatch) -> None:
    """A disk that cannot fit the download (plus the safety reserve) must be
    rejected before it is filled, leaving no partial archive behind --
    mirrors download.py's ``test_download_gguf_preserves_disk_reserve``."""
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes)
    # Free space just barely covers the archive itself, with nothing left
    # over for the (default, 128MiB) safety reserve.
    monkeypatch.setattr(
        binary_fetcher_module.shutil,
        "disk_usage",
        lambda directory: SimpleNamespace(free=len(archive_bytes)),
    )
    fetcher = BinaryFetcher(tmp_path, http_client=_client_returning(archive_bytes))

    with pytest.raises(BinaryVerificationError, match="free disk space"):
        fetcher.ensure_binary(release, "cpu")

    assert not any(tmp_path.rglob("llama-server.exe"))
    assert not any(p.name.startswith(".download-") for p in tmp_path.iterdir())


class _RecordingClient:
    """Delegates to a real mock-transport client while noting the timeout used."""

    def __init__(self, inner: httpx.Client) -> None:
        self._inner = inner
        self.timeouts: list[httpx.Timeout] = []

    def stream(self, *args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return self._inner.stream(*args, **kwargs)


def test_a_cancellable_download_tolerates_an_ordinary_network_stall(tmp_path: Path) -> None:
    """The cancellable read timeout must outlast a routine hiccup.

    It is an inactivity timeout, chosen so a stalled socket cannot delay
    cancellation for the full ordinary timeout -- the token is only checked
    between chunks. At one second it did that far too literally: shorter than
    a single TCP retransmission backoff, so the most routine network event
    there is aborted the entire hundred-plus-megabyte transfer, and nothing
    here retries or resumes.

    Measured against a local server that pauses once mid-body, a 1.5s stall
    failed the download in 1.38s with a token and succeeded without one.
    """
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=archive_bytes)

    client = _RecordingClient(httpx.Client(transport=httpx.MockTransport(handler)))
    fetcher = BinaryFetcher(tmp_path, http_client=client)

    fetcher.ensure_binary(release, "cpu", cancellation_event=SimpleNamespace(is_set=lambda: False))

    used = client.timeouts[0]
    assert used is not None
    # Comfortably past retransmission backoff...
    assert used.read >= 10.0
    # ...while still far tighter than the non-cancellable budget, which is the
    # whole reason a separate timeout exists.
    assert used.read < binary_fetcher_module._DOWNLOAD_TIMEOUT.read


def test_a_stall_after_cancellation_is_reported_as_cancellation(tmp_path: Path) -> None:
    """A socket that goes silent once the user has stopped is not a network fault.

    The in-loop token check only runs between chunks, so when no further chunk
    arrives the request leaves through the read timeout instead. Reporting
    "check your network connection" for a stop the caller asked for would be
    actively misleading.
    """
    release = _release_for(_build_archive())
    # Unset until the request is in flight -- ensure_binary refuses an
    # already-cancelled call up front, which would not exercise this path.
    stopped = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        stopped["value"] = True
        raise httpx.ReadTimeout("the socket went quiet")

    fetcher = BinaryFetcher(
        tmp_path, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(BinaryVerificationError, match="cancelled"):
        fetcher.ensure_binary(
            release,
            "cpu",
            cancellation_event=SimpleNamespace(is_set=lambda: stopped["value"]),
        )


# ---------------------------------------------------------------------------
# Superseded runtime builds are removed (RT-20)
# ---------------------------------------------------------------------------


def _older_build(root: Path, name: str) -> Path:
    """A build directory laid out like an extracted release, under ``root``."""
    directory = root / name
    (directory / "nested").mkdir(parents=True)
    (directory / "llama-server.exe").write_bytes(b"older stub")
    (directory / "llama-server-impl.dll").write_bytes(b"older impl")
    (directory / "nested" / "ggml-extra.dll").write_bytes(b"older backend")
    return directory


def _fetch_current(root: Path, *, tag: str = "b0100") -> tuple[BinaryFetcher, PinnedRelease]:
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes, tag=tag)
    fetcher = BinaryFetcher(root, http_client=_client_returning(archive_bytes))
    fetcher.ensure_binary(release, "cpu")
    return fetcher, release


def test_old_release_directories_are_pruned(tmp_path: Path) -> None:
    older_cpu = _older_build(tmp_path, "b0050-cpu")
    older_vulkan = _older_build(tmp_path, "b0050-vulkan")
    oldest = _older_build(tmp_path, "b0007-cpu")
    same_tag_other_backend = _older_build(tmp_path, "b0100-vulkan")
    newer = _older_build(tmp_path, "b0200-cpu")
    # Things that are not superseded builds and must never be touched.
    in_progress_download = tmp_path / ".download-0123456789abcdef"
    in_progress_download.write_bytes(b"partial archive")
    in_progress_extract = tmp_path / ".extract-0123456789abcdef"
    (in_progress_extract / "half").mkdir(parents=True)
    marker = tmp_path / "preferred_gpu_backend.json"
    marker.write_text("{}", encoding="utf-8")
    unrelated = tmp_path / "notes"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep", encoding="utf-8")
    a_file_named_like_a_build = tmp_path / "b0040-cpu"
    a_file_named_like_a_build.write_bytes(b"not a directory")

    fetcher, release = _fetch_current(tmp_path)

    assert not older_cpu.exists()
    assert not older_vulkan.exists()
    assert not oldest.exists()
    # The pinned release, on both backends, and anything newer than it.
    assert (tmp_path / "b0100-cpu" / "llama-server.exe").is_file()
    assert same_tag_other_backend.is_dir()
    assert newer.is_dir()
    assert in_progress_download.is_file()
    assert in_progress_extract.is_dir()
    assert marker.is_file()
    assert (unrelated / "keep.txt").is_file()
    assert a_file_named_like_a_build.is_file()
    # No half-deleted remains are left under a launchable name, and the runtime
    # that was just fetched still verifies.
    assert not any(entry.name.startswith(".prune-") for entry in tmp_path.iterdir())
    assert fetcher.is_cached(release, "cpu")


def test_pruning_also_runs_when_the_current_build_was_already_cached(tmp_path: Path) -> None:
    fetcher, release = _fetch_current(tmp_path)
    older = _older_build(tmp_path, "b0050-cpu")
    later = BinaryFetcher(tmp_path, http_client=_client_returning(b"no download expected"))

    later.ensure_binary(release, "cpu")

    assert not older.exists()
    assert fetcher.is_cached(release, "cpu")


def test_pruning_happens_once_per_release_and_process(tmp_path: Path) -> None:
    fetcher, release = _fetch_current(tmp_path)
    appeared_later = _older_build(tmp_path, "b0050-cpu")

    fetcher.ensure_binary(release, "cpu")

    assert appeared_later.is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="relies on Windows refusing to rename a directory that holds an open file")
def test_a_build_that_is_locked_is_kept_and_the_launch_still_succeeds(tmp_path: Path) -> None:
    locked = _older_build(tmp_path, "b0050-cpu")
    free = _older_build(tmp_path, "b0060-cpu")
    # A file held open the way a running program holds its own files.
    with (locked / "llama-server-impl.dll").open("rb"):
        fetcher, release = _fetch_current(tmp_path)

        assert (locked / "llama-server.exe").is_file()
        assert (locked / "nested" / "ggml-extra.dll").is_file()
    assert not free.exists()
    assert fetcher.is_cached(release, "cpu")


def test_a_build_a_program_is_running_from_is_left_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A running image refuses a write-open; that alone is enough to keep the build."""
    in_use = _older_build(tmp_path, "b0050-cpu")
    real_open = Path.open

    def open_refusing_the_running_image(self: Path, mode: str = "r", *args, **kwargs):
        if self.name == "llama-server.exe" and "+" in mode:
            raise PermissionError(13, "The process cannot access the file")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_refusing_the_running_image)

    _fetch_current(tmp_path)

    assert (in_use / "llama-server.exe").is_file()
    assert (in_use / "llama-server-impl.dll").is_file()
    assert (in_use / "nested" / "ggml-extra.dll").is_file()


def test_a_failure_while_deleting_never_fails_the_launch_and_is_finished_next_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = _older_build(tmp_path, "b0050-cpu")
    archive_bytes = _build_archive()
    release = _release_for(archive_bytes, tag="b0100")  # built first: it uses rmtree itself
    real_rmtree = shutil.rmtree

    def refuse(*args, **kwargs):
        raise PermissionError(13, "access denied")

    monkeypatch.setattr(binary_fetcher_module.shutil, "rmtree", refuse)
    BinaryFetcher(tmp_path, http_client=_client_returning(archive_bytes)).ensure_binary(release, "cpu")  # must not raise

    # The old build was moved out of the launchable name before the delete
    # failed, so nothing can start from a half-deleted directory.
    assert not older.exists()
    leftovers = [entry for entry in tmp_path.iterdir() if entry.name.startswith(".prune-")]
    assert len(leftovers) == 1

    monkeypatch.setattr(binary_fetcher_module.shutil, "rmtree", real_rmtree)
    BinaryFetcher(tmp_path, http_client=_client_returning(b"no download expected")).ensure_binary(release, "cpu")

    assert not any(entry.name.startswith(".prune-") for entry in tmp_path.iterdir())


def test_at_most_a_handful_of_builds_are_removed_per_pass(tmp_path: Path) -> None:
    for build in range(1, 13):
        _older_build(tmp_path, f"b{build:04d}-cpu")

    _fetch_current(tmp_path)

    remaining = [entry.name for entry in tmp_path.iterdir() if entry.name.startswith("b00") and entry.name != "b0100-cpu"]
    assert len(remaining) == 12 - binary_fetcher_module._MAX_PRUNED_PER_PASS


def test_a_link_that_looks_like_an_old_build_is_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "precious").mkdir(parents=True)
    (outside / "precious" / "data.txt").write_text("keep", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    try:
        (runtime / "b0050-cpu").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create directory symlinks")

    _fetch_current(runtime)

    assert (outside / "precious" / "data.txt").read_text(encoding="utf-8") == "keep"
    # And the link itself was not moved or renamed out of sight.
    assert (runtime / "b0050-cpu").is_symlink()
    assert not any(entry.name.startswith(".prune-") for entry in runtime.iterdir())


def test_nothing_is_pruned_when_the_pinned_tag_is_not_a_build_number(tmp_path: Path) -> None:
    older = _older_build(tmp_path, "b0050-cpu")

    _fetch_current(tmp_path, tag="custom-tag")

    assert older.is_dir()


def test_a_cancelled_pass_removes_nothing(tmp_path: Path) -> None:
    fetcher, release = _fetch_current(tmp_path)
    older = _older_build(tmp_path, "b0050-cpu")
    fetcher._pruned_for = None

    fetcher._prune_superseded_builds(release, SimpleNamespace(is_set=lambda: True))

    assert older.is_dir()


def test_pruning_logs_build_names_and_nothing_from_inside_them(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    _older_build(tmp_path, "b0050-cpu")

    _fetch_current(tmp_path)

    assert "b0050-cpu" in caplog.text
    for inside in ("llama-server-impl.dll", "ggml-extra.dll", "older stub"):
        assert inside not in caplog.text
