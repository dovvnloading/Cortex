"""Regressions for concurrent GGUF publication and split-model rollback.

These tests force the exact filesystem interleavings where a model saved by a
different writer used to be overwritten or deleted. No real network is used.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cortex_backend.llamacpp import download as gguf_download


def test_hard_link_fallback_cannot_clobber_a_model_created_just_before_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = tmp_path / ".staged.part"
    staged.write_bytes(b"GGUF-staged-content")
    destination = tmp_path / "model.gguf"
    other_bytes = b"concurrent writer's existing model"
    real_move = gguf_download._move_no_replace

    def links_unavailable(_source: Path, _destination: Path) -> None:
        raise OSError(errno.ENOTSUP, "hard links unsupported")

    def competing_move(source: Path, target: Path) -> None:
        # The competitor wins AFTER the hard link fails but immediately BEFORE
        # the no-replace rename. A previous exists()+os.replace() lost this race.
        target.write_bytes(other_bytes)
        real_move(source, target)

    monkeypatch.setattr(gguf_download.os, "link", links_unavailable)
    monkeypatch.setattr(gguf_download, "_move_no_replace", competing_move)

    with pytest.raises(gguf_download.GGUFDownloadError, match="refusing to overwrite"):
        gguf_download._publish(staged, destination, allow_overwrite=False)

    assert destination.read_bytes() == other_bytes
    assert staged.read_bytes() == b"GGUF-staged-content"


def test_hard_link_fallback_fails_closed_if_atomic_rename_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = tmp_path / ".staged.part"
    staged.write_bytes(b"GGUF-staged-content")
    destination = tmp_path / "model.gguf"

    def unsupported(*_args: object) -> None:
        raise OSError(errno.ENOTSUP, "atomic operation unsupported")

    monkeypatch.setattr(gguf_download.os, "link", unsupported)
    monkeypatch.setattr(gguf_download, "_move_no_replace", unsupported)

    with pytest.raises(gguf_download.GGUFDownloadError, match="without risking an overwrite"):
        gguf_download._publish(staged, destination, allow_overwrite=False)

    assert staged.exists()
    assert not destination.exists()


def _stub_network(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubGGUFTransfer:
        def __init__(self, *, staging_path: Path, **_kwargs: object) -> None:
            self.path = staging_path

        def run(self) -> None:
            self.path.write_bytes(b"GGUF" + b"\x00" * 28)

    monkeypatch.setattr(gguf_download, "_GGUFTransfer", StubGGUFTransfer)
    monkeypatch.setattr(gguf_download, "_validate_gguf_file", lambda _path: None)


def _split_paths(tmp_path: Path) -> tuple[tuple[tuple[str, str], ...], Path, Path]:
    first_name = "model-00001-of-00002.gguf"
    second_name = "model-00002-of-00002.gguf"
    parts = (
        ("https://example.com/first", first_name),
        ("https://example.com/second", second_name),
    )
    return parts, tmp_path / first_name, tmp_path / second_name


def test_single_gguf_cancelled_during_validation_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_network(monkeypatch)
    from threading import Event

    cancelled = Event()
    model = tmp_path / "single.gguf"
    monkeypatch.setattr(gguf_download, "_validate_gguf_file", lambda _path: cancelled.set())

    with pytest.raises(gguf_download.GGUFDownloadError, match="cancelled"):
        gguf_download.download_gguf(
            "https://example.com/single.gguf", model.name, tmp_path, cancellation_event=cancelled
        )

    assert not model.exists()
    assert not list(tmp_path.glob(".download-*.part"))


def test_single_gguf_cancel_during_publication_rolls_back_only_its_own_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_network(monkeypatch)
    from threading import Event

    cancelled = Event()
    target = tmp_path / "single.gguf"
    original_publish = gguf_download._publish

    def publish_and_cancel(staged: Path, destination: Path, *, allow_overwrite: bool) -> None:
        original_publish(staged, destination, allow_overwrite=allow_overwrite)
        cancelled.set()

    monkeypatch.setattr(gguf_download, "_publish", publish_and_cancel)
    with pytest.raises(gguf_download.GGUFDownloadError, match="cancelled"):
        gguf_download.download_gguf(
            "https://example.com/single.gguf", target.name, tmp_path, cancellation_event=cancelled
        )

    assert not target.exists()


def test_split_gguf_cancelled_during_final_validation_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_network(monkeypatch)
    from threading import Event

    cancelled = Event()
    parts, first, second = _split_paths(tmp_path)
    validations = 0

    def validate(_staged: Path) -> None:
        nonlocal validations
        validations += 1
        if validations == 2:
            cancelled.set()

    monkeypatch.setattr(gguf_download, "_validate_gguf_file", validate)
    with pytest.raises(gguf_download.GGUFDownloadError, match="cancelled"):
        gguf_download.download_gguf_set(parts, tmp_path, cancellation_event=cancelled)

    assert validations == 2
    assert not first.exists() and not second.exists()
    assert not list(tmp_path.glob(".download-*.part"))


def test_split_cancel_during_publication_keeps_a_replaced_foreign_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_network(monkeypatch)
    from threading import Event

    cancelled = Event()
    parts, first, second = _split_paths(tmp_path)
    original_publish = gguf_download._publish
    foreign_content = b"replacement from a different writer"

    def publish_swap_and_cancel(staged: Path, destination: Path, *, allow_overwrite: bool) -> None:
        original_publish(staged, destination, allow_overwrite=allow_overwrite)
        if destination == first:
            foreign = tmp_path / ".foreign-shard"
            foreign.write_bytes(foreign_content)
            os.replace(foreign, first)
            cancelled.set()

    monkeypatch.setattr(gguf_download, "_publish", publish_swap_and_cancel)
    with pytest.raises(gguf_download.GGUFDownloadError, match="cancelled"):
        gguf_download.download_gguf_set(parts, tmp_path, cancellation_event=cancelled)

    assert first.read_bytes() == foreign_content
    assert not second.exists()
    assert not list(tmp_path.glob(".download-*.part"))


def test_split_rollback_does_not_delete_a_replaced_earlier_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_network(monkeypatch)
    parts, first, second = _split_paths(tmp_path)
    original_publish = gguf_download._publish
    calls = 0
    foreign_bytes = b"first shard replaced by another program"

    def publish_with_competitor(staged: Path, destination: Path, *, allow_overwrite: bool) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            foreign = tmp_path / ".foreign.part"
            foreign.write_bytes(foreign_bytes)
            os.replace(foreign, first)
            second.write_bytes(b"another program's second shard")
        original_publish(staged, destination, allow_overwrite=allow_overwrite)

    monkeypatch.setattr(gguf_download, "_publish", publish_with_competitor)

    with pytest.raises(gguf_download.GGUFDownloadError, match="already exists"):
        gguf_download.download_gguf_set(parts, tmp_path)

    assert first.read_bytes() == foreign_bytes
    assert second.read_bytes() == b"another program's second shard"
    assert not list(tmp_path.glob(".rollback-*.part"))


def test_split_rollback_preserves_a_file_swapped_between_check_and_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_network(monkeypatch)
    parts, first, second = _split_paths(tmp_path)
    original_publish = gguf_download._publish
    original_move = gguf_download._move_no_replace
    foreign_bytes = b"foreign shard saved during rollback"
    moved_during_rollback = False

    def fail_second_publish(staged: Path, destination: Path, *, allow_overwrite: bool) -> None:
        if destination == second:
            second.write_bytes(b"occupied second shard")
        original_publish(staged, destination, allow_overwrite=allow_overwrite)

    def swap_before_quarantine(source: Path, destination: Path) -> None:
        nonlocal moved_during_rollback
        if source == first and destination.name.startswith(".rollback-"):
            moved_during_rollback = True
            foreign = tmp_path / ".foreign.part"
            foreign.write_bytes(foreign_bytes)
            os.replace(foreign, first)
        original_move(source, destination)

    monkeypatch.setattr(gguf_download, "_publish", fail_second_publish)
    monkeypatch.setattr(gguf_download, "_move_no_replace", swap_before_quarantine)

    with pytest.raises(gguf_download.GGUFDownloadError, match="already exists"):
        gguf_download.download_gguf_set(parts, tmp_path)

    assert moved_during_rollback
    assert first.read_bytes() == foreign_bytes
    assert second.read_bytes() == b"occupied second shard"
    assert not list(tmp_path.glob(".rollback-*.part"))


def test_split_rollback_removes_only_its_own_unmodified_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_network(monkeypatch)
    parts, first, second = _split_paths(tmp_path)
    original_publish = gguf_download._publish

    def fail_second_publish(staged: Path, destination: Path, *, allow_overwrite: bool) -> None:
        if destination == second:
            second.write_bytes(b"other writer's shard")
        original_publish(staged, destination, allow_overwrite=allow_overwrite)

    monkeypatch.setattr(gguf_download, "_publish", fail_second_publish)

    with pytest.raises(gguf_download.GGUFDownloadError, match="already exists"):
        gguf_download.download_gguf_set(parts, tmp_path)

    assert not first.exists()
    assert second.read_bytes() == b"other writer's shard"
    assert not list(tmp_path.glob(".rollback-*.part"))

def _kill_after_split_publication(
    folder: Path, parts: tuple[tuple[str, str], ...], stop_after: int,
) -> None:
    """A separate process dies without running the download's finally block."""
    program = """
import os
import sys
from pathlib import Path
from cortex_backend.llamacpp import download as d

class FakeTransfer:
    def __init__(self, *, staging_path, **kwargs):
        self.path = staging_path
    def run(self):
        self.path.write_bytes(b'GGUF' + b'\\0' * 28)

d._GGUFTransfer = FakeTransfer
d._validate_gguf_file = lambda _path: None
publish = d._publish
count = 0

def crash_after_publish(staged, dest, *, allow_overwrite):
    global count
    publish(staged, dest, allow_overwrite=allow_overwrite)
    count += 1
    if count == int(sys.argv[2]):
        os._exit(71)

d._publish = crash_after_publish
d.download_gguf_set(
    [
        ('https://example.com/first', 'model-00001-of-00002.gguf'),
        ('https://example.com/second', 'model-00002-of-00002.gguf'),
    ],
    Path(sys.argv[1]),
)
"""
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(root / "backend")}
    ran = subprocess.run(
        [sys.executable, "-c", program, str(folder), str(stop_after)],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    assert ran.returncode == 71, ran.stderr
    assert len(tuple(folder.glob(".split-publish-*.json"))) == 1


def test_interrupted_split_publication_recovers_after_a_real_process_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts, first, second = _split_paths(tmp_path)
    unrelated = tmp_path / "unrelated.gguf"
    unrelated.write_bytes(b"Unrelated model - never delete")
    _kill_after_split_publication(tmp_path, parts, stop_after=1)
    assert first.exists() and not second.exists()
    journal = next(tmp_path.glob(".split-publish-*.json"))
    # Even signed source URLs must not be saved in clear text.
    record = json.loads(journal.read_text(encoding="utf-8"))
    assert record["source_fingerprint"] and "https://" not in journal.read_text(encoding="utf-8")

    _stub_network(monkeypatch)
    finished = gguf_download.download_gguf_set(parts, tmp_path)
    assert finished == (first, second)
    assert first.exists() and second.exists()
    assert unrelated.read_bytes() == b"Unrelated model - never delete"
    assert not tuple(tmp_path.glob(".split-publish-*.json"))
    assert not tuple(tmp_path.glob(".download-*.part"))


def test_restart_recognizes_all_shards_published_before_process_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts, first, second = _split_paths(tmp_path)
    _kill_after_split_publication(tmp_path, parts, stop_after=2)
    assert first.exists() and second.exists()
    before = [gguf_download._file_identity(path) for path in (first, second)]

    class ForbiddenNetwork:
        def __init__(self, **kwargs):
            raise AssertionError("a completed split set must not download again")

    monkeypatch.setattr(gguf_download, "_GGUFTransfer", ForbiddenNetwork)
    restored = gguf_download.download_gguf_set(parts, tmp_path)
    assert restored == (first, second)
    assert [gguf_download._file_identity(path) for path in restored] == before
    assert not tuple(tmp_path.glob(".split-publish-*.json"))


def test_crash_recovery_never_deletes_foreign_shard_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts, first, second = _split_paths(tmp_path)
    _kill_after_split_publication(tmp_path, parts, stop_after=1)
    foreign = tmp_path / ".other-writer.part"
    foreign.write_bytes(b"somebody else's existing model")
    os.replace(foreign, first)

    _stub_network(monkeypatch)
    with pytest.raises(gguf_download.GGUFDownloadError, match="already exists"):
        gguf_download.download_gguf_set(parts, tmp_path)
    assert first.read_bytes() == b"somebody else's existing model"
    assert not second.exists()


def test_tampered_split_manifest_fails_closed_without_deleting_a_model(
    tmp_path: Path,
) -> None:
    parts, first, second = _split_paths(tmp_path)
    first.write_bytes(b"user-owned model")
    manifest = gguf_download._split_manifest_path(tmp_path, [name for _url, name in parts])
    manifest.write_text('{"version":1,"entries":[]}', encoding="utf-8")

    with pytest.raises(gguf_download.GGUFDownloadError, match="could not be verified"):
        gguf_download.download_gguf_set(parts, tmp_path)
    assert first.read_bytes() == b"user-owned model"
    assert not second.exists()
    assert manifest.exists()


