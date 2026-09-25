"""Tests for GGUF folder scanning, id/path resolution, and the combined catalog."""

from __future__ import annotations

import os
import struct
import time
from pathlib import Path

import numpy as np
import pytest

# The gguf availability check must run before any module that reads GGUF
# files is imported, so these imports are legitimately below it.
gguf = pytest.importorskip("gguf")

from cortex_backend.core.generation import ConnectionResult  # noqa: E402
from cortex_backend.llamacpp.model_directory import (  # noqa: E402
    GGUFModelDirectory,
    InvalidGGUFModelId,
    resolve_configured_directory,
    MAX_SCAN_DEPTH,
    resolve_gguf_path,
    to_model_id,
)
from cortex_backend.services.model_catalog import CombinedModelCatalog  # noqa: E402
from cortex_backend.services.models import InstalledModel  # noqa: E402


def _write_gguf(path: Path, *, context_length: int = 4096, quant=None) -> None:
    writer = gguf.GGUFWriter(str(path), "llama")
    writer.add_context_length(context_length)
    writer.add_name(path.stem)
    if quant is not None:
        writer.add_file_type(quant)
    writer.add_tensor("dummy.weight", np.zeros((2, 2), dtype=np.float32))
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()


def test_list_installed_details_scans_gguf_files(tmp_path: Path) -> None:
    _write_gguf(tmp_path / "model-a.Q4_K_M.gguf", context_length=8192, quant=gguf.LlamaFileType.MOSTLY_Q4_K_M)
    _write_gguf(tmp_path / "model-b.gguf", context_length=2048)
    (tmp_path / "not-a-model.txt").write_text("ignore me")

    directory = GGUFModelDirectory(lambda: tmp_path)
    models = directory.list_installed_details()

    names = {model.name for model in models}
    assert names == {"gguf:model-a.Q4_K_M.gguf", "gguf:model-b.gguf"}
    by_name = {model.name: model for model in models}
    assert by_name["gguf:model-a.Q4_K_M.gguf"].context_length == 8192
    assert by_name["gguf:model-a.Q4_K_M.gguf"].quantization_level == "Q4_K_M"
    assert by_name["gguf:model-a.Q4_K_M.gguf"].source == "gguf"
    assert by_name["gguf:model-b.gguf"].context_length == 2048


def test_invalid_gguf_files_are_excluded_from_the_scan(tmp_path: Path) -> None:
    """A file with a '.gguf' extension but non-GGUF content (e.g. an HTML
    page saved after a broken download link) must not appear as a
    selectable model -- it would only fail once the user tries to chat
    with it, with a confusing error far removed from the actual cause."""
    _write_gguf(tmp_path / "real-model.gguf")
    (tmp_path / "broken.gguf").write_bytes(b"<!doctype html><html>not a model</html>")

    directory = GGUFModelDirectory(lambda: tmp_path)
    models = directory.list_installed_details()

    assert {model.name for model in models} == {"gguf:real-model.gguf"}


def test_missing_directory_scans_to_empty_without_raising(tmp_path: Path) -> None:
    directory = GGUFModelDirectory(lambda: tmp_path / "does-not-exist")
    assert directory.list_installed_details() == ()


def test_scan_cache_is_invalidated_by_mtime_and_size(tmp_path: Path) -> None:
    path = tmp_path / "model.gguf"
    _write_gguf(path, context_length=2048)
    directory = GGUFModelDirectory(lambda: tmp_path)
    first = directory.list_installed_details()
    assert first[0].context_length == 2048

    _write_gguf(path, context_length=16384)
    # A rewrite can land on an identical (size, mtime_ns) key on a
    # coarse-resolution or very fast filesystem even though the content
    # changed -- force a detectable mtime bump so this test isn't flaky,
    # independent of the cache's real-world behavior on genuine file edits.
    future = time.time() + 5
    os.utime(path, (future, future))
    second = directory.list_installed_details()
    assert second[0].context_length == 16384


def test_resolve_gguf_path_accepts_a_subfolder_and_rejects_escapes(tmp_path: Path) -> None:
    """Ids name a path under the folder, because that is how models are stored.

    Every downloader writes one folder per repository, so refusing a separator
    meant a model in a subfolder could be listed but never opened. Leaving the
    configured folder is still refused, and containment is checked after
    resolving so a link cannot be used to step outside it.
    """
    path = tmp_path / "model.gguf"
    _write_gguf(path)
    nested = tmp_path / "Repo-Name-gguf" / "nested.gguf"
    nested.parent.mkdir()
    _write_gguf(nested)

    assert resolve_gguf_path(tmp_path, to_model_id("model.gguf")) == path.resolve()
    assert resolve_gguf_path(tmp_path, "gguf:Repo-Name-gguf/nested.gguf") == nested.resolve()
    # Windows spells its separators differently; the id must mean the same thing.
    assert resolve_gguf_path(tmp_path, r"gguf:Repo-Name-gguf\nested.gguf") == nested.resolve()

    for bad in (
        "not-a-gguf-id",
        "gguf:",
        "gguf:../escape.gguf",
        "gguf:Repo-Name-gguf/../../escape.gguf",
        "gguf:/etc/passwd",
        r"gguf:C:\Windows\System32\drivers\etc\hosts",
        "gguf:missing.gguf",
        "gguf:Repo-Name-gguf/missing.gguf",
    ):
        with pytest.raises(InvalidGGUFModelId):
            resolve_gguf_path(tmp_path, bad)


def test_resolve_gguf_path_refuses_a_link_that_leaves_the_folder(tmp_path: Path) -> None:
    """Containment is proven after resolving, not by inspecting the text."""
    root = tmp_path / "models"
    root.mkdir()
    outside = tmp_path / "outside.gguf"
    _write_gguf(outside)
    link = root / "link.gguf"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - needs privilege on Windows
        pytest.skip("this host cannot create symlinks")

    with pytest.raises(InvalidGGUFModelId):
        resolve_gguf_path(root, "gguf:link.gguf")


def test_resolve_configured_directory_prefers_explicit_setting(tmp_path: Path) -> None:
    default_dir = tmp_path / "default"
    assert resolve_configured_directory(None, default_dir) == default_dir
    custom = str(tmp_path / "custom")
    assert resolve_configured_directory(custom, default_dir) == Path(custom)


def test_resolve_configured_directory_falls_back_to_the_parent_of_a_gguf_file(tmp_path: Path) -> None:
    """Pointing the setting at a specific .gguf file (a very natural
    mistake -- "point Cortex at your model") must resolve to that file's
    folder, not silently scan a non-directory and find nothing."""
    default_dir = tmp_path / "default"
    models_dir = tmp_path / "Bonsai-27B-gguf"
    models_dir.mkdir()
    model_file = models_dir / "Bonsai-27B-Q1_0.gguf"
    model_file.write_bytes(b"GGUF" + b"\x00" * 16)

    resolved = resolve_configured_directory(str(model_file), default_dir)

    assert resolved == models_dir


def test_resolve_configured_directory_leaves_a_real_directory_alone(tmp_path: Path) -> None:
    """If the configured path IS an existing directory that merely happens
    to end in '.gguf' (unlikely, but not impossible), don't rewrite it."""
    default_dir = tmp_path / "default"
    odd_dir = tmp_path / "models.gguf"
    odd_dir.mkdir()

    resolved = resolve_configured_directory(str(odd_dir), default_dir)

    assert resolved == odd_dir


class _FakeOllamaCatalog:
    def __init__(self, models: tuple[InstalledModel, ...]) -> None:
        self._models = models

    def inventory(self):
        return self._models, ConnectionResult.connected("ok")

    def list_installed(self):
        return tuple(m.name for m in self._models)

    def pull_model(self, model, **kwargs):
        return True

    def check(self, **kwargs):
        return ConnectionResult.connected("ok")

    def model_supports_vision(self, model):
        return True


def test_combined_catalog_merges_ollama_and_gguf(tmp_path: Path) -> None:
    _write_gguf(tmp_path / "local.gguf")
    ollama_models = (InstalledModel(name="qwen3:8b", source="ollama"),)
    catalog = CombinedModelCatalog(_FakeOllamaCatalog(ollama_models), GGUFModelDirectory(lambda: tmp_path))

    inventory, connection = catalog.inventory()
    names = {m.name for m in inventory}
    assert names == {"qwen3:8b", "gguf:local.gguf"}
    assert connection.success is True
    assert set(catalog.list_installed()) == names

    assert catalog.model_supports_vision("qwen3:8b") is True
    # False, not None: every gate that reads this tests `is False`, so
    # None meant an attached image was accepted, announced in the prompt,
    # and then stripped before the request reached the model.
    assert catalog.model_supports_vision("gguf:local.gguf") is False


def test_the_scan_finds_models_in_subfolders(tmp_path: Path) -> None:
    """The layout every downloader actually produces: one folder per repository.

    The scan used to glob a single directory, so pointing it at a models root
    showed only whatever was loose at the top level, and pointing it at one
    model's folder showed that model alone -- there was no setting that
    revealed both.
    """
    _write_gguf(tmp_path / "loose.gguf")
    (tmp_path / "Repo-One-gguf").mkdir()
    _write_gguf(tmp_path / "Repo-One-gguf" / "one.gguf")
    (tmp_path / "Repo-Two-gguf" / "nested").mkdir(parents=True)
    _write_gguf(tmp_path / "Repo-Two-gguf" / "nested" / "two.gguf")

    models = GGUFModelDirectory(lambda: tmp_path).list_installed_details()

    assert {model.name for model in models} == {
        "gguf:loose.gguf",
        "gguf:Repo-One-gguf/one.gguf",
        "gguf:Repo-Two-gguf/nested/two.gguf",
    }
    # Every id has to open the file it names, or a listed model cannot be used.
    for model in models:
        assert resolve_gguf_path(tmp_path, model.name) == Path(model.path).resolve()


def test_the_scan_stops_descending_at_the_depth_bound(tmp_path: Path) -> None:
    """A bounded walk, so a models folder nested inside a large tree cannot stall."""
    deep = tmp_path
    for level in range(MAX_SCAN_DEPTH + 2):
        deep = deep / f"level{level}"
    deep.mkdir(parents=True)
    _write_gguf(deep / "too-deep.gguf")
    reachable = tmp_path / "level0" / "shallow.gguf"
    _write_gguf(reachable)

    names = {model.name for model in GGUFModelDirectory(lambda: tmp_path).list_installed_details()}

    assert "gguf:level0/shallow.gguf" in names
    assert not any("too-deep" in name for name in names)


def test_companion_files_are_not_offered_as_models(tmp_path: Path) -> None:
    """A projector and a shard slice cannot be loaded on their own.

    Listing them invites the user to pick something llama-server will refuse,
    which surfaces much later as an unexplained crash-loop. Recursion makes
    this matter: these files sit beside the model in its own folder.
    """
    _write_gguf(tmp_path / "model.gguf")
    _write_gguf(tmp_path / "mmproj-model-f16.gguf")
    _write_gguf(tmp_path / "big-00001-of-00003.gguf")
    _write_gguf(tmp_path / "big-00002-of-00003.gguf")
    _write_gguf(tmp_path / "big-00003-of-00003.gguf")

    names = {model.name for model in GGUFModelDirectory(lambda: tmp_path).list_installed_details()}

    assert names == {"gguf:model.gguf", "gguf:big-00001-of-00003.gguf"}, (
        "only the model and the first shard of the set should be offered"
    )


def test_a_projector_is_dropped_even_when_its_name_does_not_say_so(tmp_path: Path) -> None:
    """The architecture inside the file is what actually settles it."""
    writer = gguf.GGUFWriter(str(tmp_path / "vision-tower.gguf"), "clip")
    writer.add_name("vision-tower")
    writer.add_tensor("dummy.weight", np.zeros((2, 2), dtype=np.float32))
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()
    _write_gguf(tmp_path / "real.gguf")

    names = {model.name for model in GGUFModelDirectory(lambda: tmp_path).list_installed_details()}

    assert names == {"gguf:real.gguf"}


def test_an_unreadable_subfolder_costs_only_that_subfolder(tmp_path: Path) -> None:
    _write_gguf(tmp_path / "good.gguf")
    missing = tmp_path / "vanished"
    missing.mkdir()
    directory = GGUFModelDirectory(lambda: tmp_path)
    missing.rmdir()

    names = {model.name for model in directory.list_installed_details()}
    assert names == {"gguf:good.gguf"}


def _corrupt_gguf(path: Path, string_length: int) -> None:
    """A GGUF header whose one metadata string claims ``string_length`` bytes."""
    key = b"general.junk"
    path.write_bytes(
        b"GGUF"
        + struct.pack("<IQQ", 3, 0, 1)
        + struct.pack("<Q", len(key))
        + key
        + struct.pack("<I", 8)
        + struct.pack("<Q", string_length)
    )


@pytest.mark.parametrize("string_length", [2**63, 2**64 - 1])
def test_one_corrupt_file_does_not_hide_the_other_models(tmp_path: Path, string_length: int) -> None:
    """A length of 2**63 or more made the metadata reader raise, the error
    escaped the scan, and the catalog swallowed it -- so one corrupt file
    anywhere in the tree emptied the whole GGUF list without a word.
    """
    _write_gguf(tmp_path / "good.gguf", context_length=8192)
    (tmp_path / "Broken-Repo").mkdir()
    _corrupt_gguf(tmp_path / "Broken-Repo" / "broken.gguf", string_length)

    models = {model.name: model for model in GGUFModelDirectory(lambda: tmp_path).list_installed_details()}

    assert models["gguf:good.gguf"].context_length == 8192
    # It has the GGUF magic, so it is still listed -- just without details.
    assert models["gguf:Broken-Repo/broken.gguf"].family is None


def test_a_reader_that_raises_costs_one_file_its_details(tmp_path: Path, monkeypatch) -> None:
    from cortex_backend.llamacpp import model_directory

    _write_gguf(tmp_path / "good.gguf", context_length=8192)
    _write_gguf(tmp_path / "unlucky.gguf")
    real_reader = model_directory.read_gguf_metadata

    def flaky_reader(path: Path):
        if path.name == "unlucky.gguf":
            raise RuntimeError("an error the reader failed to contain")
        return real_reader(path)

    monkeypatch.setattr(model_directory, "read_gguf_metadata", flaky_reader)
    models = {model.name: model for model in GGUFModelDirectory(lambda: tmp_path).list_installed_details()}

    assert models["gguf:good.gguf"].context_length == 8192
    assert models["gguf:unlucky.gguf"].context_length is None


def test_a_failed_gguf_scan_is_logged_and_ollama_still_lists(caplog) -> None:
    class BrokenScan:
        def list_installed_details(self):
            raise ValueError(r"C:\Users\someone\private\models\x.gguf")

    ollama_models = (InstalledModel(name="qwen3:8b", source="ollama"),)
    catalog = CombinedModelCatalog(_FakeOllamaCatalog(ollama_models), BrokenScan())

    with caplog.at_level("WARNING", logger="cortex_backend.services.model_catalog"):
        inventory, _connection = catalog.inventory()

    assert [model.name for model in inventory] == ["qwen3:8b"]
    assert "GGUF model scan failed (ValueError)" in caplog.text
    # The exception message can carry private paths; it stays out of the log.
    assert "someone" not in caplog.text
