"""Runtime and model staging respect the disk reserve and do not pile up (#659-#661)."""

from __future__ import annotations

import os
from pathlib import Path
import time
from types import SimpleNamespace
import zipfile

import pytest

from cortex_backend.llamacpp import binary_fetcher as binary_fetcher_module
from cortex_backend.llamacpp.binary_fetcher import BinaryFetcher
from cortex_backend.llamacpp.errors import BinaryVerificationError

LONG_AGO = time.time() - 3600


def _archive(tmp_path: Path, size: int) -> Path:
    path = tmp_path / "runtime.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("llama/llama-server.exe", b"\0" * size)
    return path


def _free_space(monkeypatch: pytest.MonkeyPatch, *values: int) -> None:
    sequence = iter(values)
    last = {"value": values[-1]}

    def usage(_path):
        last["value"] = next(sequence, last["value"])
        return SimpleNamespace(total=10**12, used=0, free=last["value"])

    monkeypatch.setattr(binary_fetcher_module.shutil, "disk_usage", usage)


def test_unpacking_is_refused_before_it_would_breach_the_reserve(tmp_path: Path, monkeypatch) -> None:
    """Only the download was checked; extraction could fill the disk (#659)."""
    archive = _archive(tmp_path, 4 * 1024 * 1024)
    reserve = binary_fetcher_module._MIN_FREE_SPACE_BYTES
    _free_space(monkeypatch, reserve + 1024 * 1024)  # room for the archive, not its contents

    with pytest.raises(BinaryVerificationError, match="unpack"):
        BinaryFetcher._extract(archive, tmp_path / "extracted")

    assert not any((tmp_path / "extracted").rglob("*.exe"))


def test_unpacking_stops_when_space_runs_out_meanwhile(tmp_path: Path, monkeypatch) -> None:
    archive = _archive(tmp_path, 4 * 1024 * 1024)
    reserve = binary_fetcher_module._MIN_FREE_SPACE_BYTES
    # Enough at the start; then something else takes the space.
    _free_space(monkeypatch, reserve + 64 * 1024 * 1024, reserve)

    with pytest.raises(BinaryVerificationError, match="unpack"):
        BinaryFetcher._extract(archive, tmp_path / "extracted")


def test_unpacking_with_room_to_spare_succeeds(tmp_path: Path) -> None:
    archive = _archive(tmp_path, 1024)
    BinaryFetcher._extract(archive, tmp_path / "extracted")
    assert (tmp_path / "extracted" / "llama-server.exe").stat().st_size == 1024


def _age(path: Path) -> None:
    for item in (path, *path.rglob("*")) if path.is_dir() else (path,):
        os.utime(item, (LONG_AGO, LONG_AGO))


def test_staging_left_by_a_dead_runtime_install_is_reclaimed(tmp_path: Path) -> None:
    """A hard exit left .download-/.extract- staging that nothing ever removed (#661)."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    old_archive = runtime / ".download-0123"
    old_archive.write_bytes(b"partial archive")
    old_extract = runtime / ".extract-4567"
    (old_extract / "llama").mkdir(parents=True)
    (old_extract / "llama" / "llama-server.exe").write_bytes(b"partial")
    for path in (old_archive, old_extract):
        _age(path)
    live_archive = runtime / ".download-89ab"
    live_archive.write_bytes(b"still being written")
    cached_build = runtime / "b1234-cpu"
    cached_build.mkdir()
    _age(cached_build)
    unrelated = runtime / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")
    _age(unrelated)

    BinaryFetcher(runtime)._reclaim_abandoned_staging()

    assert not old_archive.exists()
    assert not old_extract.exists()
    assert live_archive.exists()
    assert cached_build.exists()
    assert unrelated.exists()


def test_staging_left_by_a_dead_model_download_is_reclaimed(tmp_path: Path, monkeypatch) -> None:
    """Every retry staged a new .download-<uuid>.part; dead ones were never removed (#660)."""
    import httpx

    from cortex_backend.llamacpp import download as download_module
    from cortex_backend.llamacpp.download import GGUFDownloadError, download_gguf

    monkeypatch.setattr(
        download_module.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [(0, 0, 0, "", ("93.184.216.34", port))],
    )
    models = tmp_path / "models"
    models.mkdir()
    abandoned = models / f".download-{'a' * 32}.part"
    abandoned.write_bytes(b"gigabytes, once")
    _age(abandoned)
    live = models / f".download-{'b' * 32}.part"
    live.write_bytes(b"another transfer")
    lookalike = models / ".download-not-ours.part"
    lookalike.write_bytes(b"unrelated")
    _age(lookalike)
    journal = models / f".split-publish-{'c' * 32}.json"
    journal.write_text("{}", encoding="utf-8")
    _age(journal)

    # Not a whole GGUF, so this download fails validation; the reclaim it does
    # first is what is under test.
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"GGUF" + bytes(64)))
    )
    with pytest.raises(GGUFDownloadError):
        download_gguf("https://example.com/model.gguf", "model.gguf", models, http_client=client)

    assert not abandoned.exists()
    assert live.exists()
    assert lookalike.exists()
    assert journal.exists()
