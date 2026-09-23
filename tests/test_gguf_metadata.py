"""Reading four values off a GGUF file without parsing the whole thing.

The reader replaced ``gguf.GGUFReader``, which materialised every key --
including the tokenizer vocabulary, hundreds of thousands of strings as
separate numpy memmap slices. That cost about eleven seconds per model, paid
once per file by the folder scan. These tests hold the replacement to the same
answers and to its contract: it never raises, whatever the bytes say.
"""

from __future__ import annotations

import struct
from pathlib import Path

import gguf
import numpy as np
import pytest

from cortex_backend.llamacpp.gguf_metadata import (
    GGUF_MAGIC,
    is_valid_gguf_file,
    read_gguf_metadata,
)


def _write_gguf(path: Path, *, architecture: str = "llama", context_length: int = 4096,
                quant=None, vocabulary: int = 0) -> None:
    writer = gguf.GGUFWriter(str(path), architecture)
    writer.add_context_length(context_length)
    writer.add_name(path.stem)
    if quant is not None:
        writer.add_file_type(quant)
    if vocabulary:
        # The shape that made the old reader slow: one large array of strings.
        writer.add_token_list([f"token{index}" for index in range(vocabulary)])
    writer.add_tensor("dummy.weight", np.zeros((2, 2), dtype=np.float32))
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()


def test_reads_the_values_the_model_list_displays(tmp_path: Path) -> None:
    path = tmp_path / "model.gguf"
    _write_gguf(path, architecture="llama", context_length=8192,
                quant=gguf.LlamaFileType.MOSTLY_Q4_K_M)

    metadata = read_gguf_metadata(path)

    assert metadata is not None
    assert metadata.architecture == "llama"
    assert metadata.context_length == 8192
    assert metadata.quantization_label == "Q4_K_M"


def test_a_large_vocabulary_is_stepped_over_rather_than_decoded(tmp_path: Path) -> None:
    """The case the rewrite exists for: the answer must not depend on it."""
    path = tmp_path / "big-vocab.gguf"
    _write_gguf(path, context_length=2048, vocabulary=50_000)

    metadata = read_gguf_metadata(path)

    assert metadata is not None
    assert metadata.context_length == 2048


def test_context_length_is_found_when_it_is_namespaced_differently(tmp_path: Path) -> None:
    path = tmp_path / "odd.gguf"
    _write_gguf(path, architecture="qwen35", context_length=262_144)

    metadata = read_gguf_metadata(path)

    assert metadata is not None
    assert metadata.architecture == "qwen35"
    assert metadata.context_length == 262_144


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("empty", b""),
        ("magic only", GGUF_MAGIC),
        ("not gguf at all", b"<!DOCTYPE html><html>nope</html>" * 8),
        ("truncated header", GGUF_MAGIC + struct.pack("<I", 3) + b"\x00" * 4),
        ("truncated mid-metadata", GGUF_MAGIC + struct.pack("<IQQ", 3, 0, 4) + b"\x00" * 8),
        ("unsupported version", GGUF_MAGIC + struct.pack("<IQQ", 99, 0, 0)),
    ],
)
def test_a_file_that_is_not_readable_gguf_returns_none(tmp_path: Path, name: str, payload: bytes) -> None:
    """Never raises, whatever the bytes are.

    The folder scan calls this per file, so one bad file raising would empty
    the model list rather than omit a single entry.
    """
    path = tmp_path / "bad.gguf"
    path.write_bytes(payload)

    assert read_gguf_metadata(path) is None


def test_an_absurd_length_field_does_not_hang_or_allocate(tmp_path: Path) -> None:
    """A hostile or corrupt length must be refused, not believed.

    One metadata entry whose key claims to be 2**60 bytes long: a reader that
    trusts it either allocates forever or blocks on a read that never
    completes.
    """
    path = tmp_path / "hostile.gguf"
    path.write_bytes(
        GGUF_MAGIC + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", 2**60) + b"partial"
    )

    assert read_gguf_metadata(path) is None


def test_an_oversized_array_count_is_refused(tmp_path: Path) -> None:
    """The array header claims more elements than could exist in the file."""
    path = tmp_path / "hostile-array.gguf"
    key = b"general.junk"
    path.write_bytes(
        GGUF_MAGIC
        + struct.pack("<IQQ", 3, 0, 1)
        + struct.pack("<Q", len(key))
        + key
        + struct.pack("<I", 9)          # ARRAY
        + struct.pack("<I", 8)          # of STRING
        + struct.pack("<Q", 2**62)      # this many of them
    )

    assert read_gguf_metadata(path) is None


def test_the_magic_check_and_the_reader_agree_about_a_bad_file(tmp_path: Path) -> None:
    path = tmp_path / "html.gguf"
    path.write_bytes(b"<html>not a model</html>")

    assert is_valid_gguf_file(path) is False
    assert read_gguf_metadata(path) is None
