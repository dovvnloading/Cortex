"""Regressions for concurrent GGUF publication and split-model rollback.

These tests force the exact filesystem interleavings where a model saved by a
different writer used to be overwritten or deleted. No real network is used.
"""

from __future__ import annotations

import errno
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


def test_post_publish_exception_is_rolled_back_even_before_the_loop_marks_it_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem publication may succeed immediately before its wrapper fails."""
    _stub_network(monkeypatch)
    parts, first, second = _split_paths(tmp_path)
    original_publish = gguf_download._publish

    def publish_then_fail(staged: Path, destination: Path, *, allow_overwrite: bool) -> None:
        original_publish(staged, destination, allow_overwrite=allow_overwrite)
        if destination == first:
            raise OSError("failure after first shard appeared")

    monkeypatch.setattr(gguf_download, "_publish", publish_then_fail)
    with pytest.raises(OSError, match="after first shard"):
        gguf_download.download_gguf_set(parts, tmp_path)

    assert not first.exists()
    assert not second.exists()
    assert not list(tmp_path.glob(".split-publish-*.json"))
    assert not list(tmp_path.glob(".download-*.part"))


def _terminate_after_shard_publish(directory: Path, *, after: int) -> None:
    """Reproduce an OS process death, bypassing Python's finally/rollback."""
    root = Path(__file__).resolve().parents[1]
    code = """
import os
import sys
from pathlib import Path
from cortex_backend.llamacpp import download as gguf

target = Path(sys.argv[1])
exit_after = int(sys.argv[2])
class StubTransfer:
    def __init__(self, *, staging_path, **_kwargs):
        self.path = staging_path
    def run(self):
        self.path.write_bytes(b"GGUF" + bytes(28))

gguf._GGUFTransfer = StubTransfer
gguf._validate_gguf_file = lambda _path: None
original_publish = gguf._publish
published = [0]
def publish_then_die(staged, destination, *, allow_overwrite):
    original_publish(staged, destination, allow_overwrite=allow_overwrite)
    if destination.suffix.lower() == ".gguf":
        published[0] += 1
        if published[0] == exit_after:
            os._exit(70 + exit_after)

gguf._publish = publish_then_die
parts = (
    ("https://example.com/first", "model-00001-of-00002.gguf"),
    ("https://example.com/second", "model-00002-of-00002.gguf"),
)
gguf.download_gguf_set(parts, target)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "backend") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", code, str(directory), str(after)],
        env=env, cwd=root, capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 70 + after, (
        "The child did not die at the requested publication barrier: "
        f"{result.returncode}; stderr={result.stderr[-1200:]}"
    )


@pytest.mark.parametrize("published_before_crash", [1, 2])
def test_split_publication_can_recover_after_a_real_process_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, published_before_crash: int,
) -> None:
    parts, first, second = _split_paths(tmp_path)
    unrelated = tmp_path / "somebody-elses.gguf"
    unrelated.write_bytes(b"independent user's existing model")
    _terminate_after_shard_publish(tmp_path, after=published_before_crash)

    assert first.is_file()
    assert second.exists() is (published_before_crash == 2)
    assert len(list(tmp_path.glob(".split-publish-*.json"))) == 1

    if published_before_crash == 1:
        # A partially published set is conditionally rolled back and retried
        # safely. Staged downloads no longer have to be located by the user.
        _stub_network(monkeypatch)
    else:
        # A fully published set needs no network even when the success
        # acknowledgement and journal cleanup were lost at process exit.
        class NoTransfer:
            def __init__(self, **_kwargs):
                raise AssertionError("a fully published split set must not redownload")
        monkeypatch.setattr(gguf_download, "_GGUFTransfer", NoTransfer)

    recovered = gguf_download.download_gguf_set(parts, tmp_path)
    assert recovered == (first, second)
    assert first.read_bytes() == second.read_bytes() == b"GGUF" + bytes(28)
    assert unrelated.read_bytes() == b"independent user's existing model"
    assert not list(tmp_path.glob(".split-publish-*.json"))
    assert not list(tmp_path.glob(".download-*.part"))


def test_crash_recovery_does_not_remove_a_foreign_shard_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts, first, second = _split_paths(tmp_path)
    _terminate_after_shard_publish(tmp_path, after=1)
    assert list(tmp_path.glob(".split-publish-*.json"))
    foreign = tmp_path / "foreign-replacement.part"
    contents = b"somebody else's new shard"
    foreign.write_bytes(contents)
    os.replace(foreign, first)  # Different inode; original owner identity gone.

    _stub_network(monkeypatch)
    with pytest.raises(gguf_download.GGUFDownloadError, match="already exists"):
        gguf_download.download_gguf_set(parts, tmp_path)

    assert first.read_bytes() == contents
    assert not second.exists()
    assert not list(tmp_path.glob(".split-publish-*.json"))


def test_crash_recovery_fails_closed_for_different_download_with_same_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts, first, _second = _split_paths(tmp_path)
    _terminate_after_shard_publish(tmp_path, after=1)
    _stub_network(monkeypatch)
    another_source = tuple(
        (url.replace("example.com", "different.example.org"), name)
        for url, name in parts
    )
    with pytest.raises(gguf_download.GGUFDownloadError, match="already exists"):
        gguf_download.download_gguf_set(another_source, tmp_path)
    assert first.exists()
    assert len(list(tmp_path.glob(".split-publish-*.json"))) == 1


def test_crash_recovery_can_restart_when_unpublished_stage_was_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts, first, second = _split_paths(tmp_path)
    _terminate_after_shard_publish(tmp_path, after=1)
    for stage in tmp_path.glob(".download-*.part"):
        stage.unlink()
    _stub_network(monkeypatch)

    assert gguf_download.download_gguf_set(parts, tmp_path) == (first, second)
    assert first.exists() and second.exists()
    assert not list(tmp_path.glob(".split-publish-*.json"))
    assert not list(tmp_path.glob(".download-*.part"))


