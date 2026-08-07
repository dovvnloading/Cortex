"""Tests for GGUF folder scanning, id/path resolution, and the combined catalog."""

from __future__ import annotations

import os
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


def test_resolve_gguf_path_rejects_traversal_and_missing_files(tmp_path: Path) -> None:
    path = tmp_path / "model.gguf"
    _write_gguf(path)

    resolved = resolve_gguf_path(tmp_path, to_model_id("model.gguf"))
    assert resolved == path.resolve()

    with pytest.raises(InvalidGGUFModelId):
        resolve_gguf_path(tmp_path, "not-a-gguf-id")
    with pytest.raises(InvalidGGUFModelId):
        resolve_gguf_path(tmp_path, "gguf:../escape.gguf")
    with pytest.raises(InvalidGGUFModelId):
        resolve_gguf_path(tmp_path, "gguf:sub/dir.gguf")
    with pytest.raises(InvalidGGUFModelId):
        resolve_gguf_path(tmp_path, "gguf:missing.gguf")


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
    assert catalog.model_supports_vision("gguf:local.gguf") is None
