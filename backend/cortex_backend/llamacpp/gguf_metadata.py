"""Best-effort GGUF metadata reads.

Only four values are wanted -- architecture, context length, quantisation and
parameter size -- and they all sit in the key-value block at the head of the
file. This reads that block directly and stops, decoding only those keys and
seeking past everything else.

It used to call ``gguf.GGUFReader``, which memory-maps the file and eagerly
materialises *every* key, including the tokenizer vocabulary: hundreds of
thousands of strings, each a separate numpy memmap slice. That cost about
eleven seconds per model here, which the folder scan paid once per file, so a
handful of models meant a model list that hung for a minute. The bounded
reader below returns the same four values in a few milliseconds.

Never raises: an unreadable or corrupt file yields ``None`` so one bad file
cannot break the folder scan.
"""

from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)

# Matches llama.cpp-style quant labels in a filename, e.g. "Q4_K_M", "IQ2_XS",
# "Q8_0" -- used only as a fallback when the GGUF's own general.file_type key
# is absent or unrecognized.
_QUANT_LABEL_PATTERN = re.compile(r"(?:^|[._-])(I?Q\d(?:_[A-Z0-9]+)*)(?:[._-]|$)", re.IGNORECASE)
# Matches a parameter-count label like "8B", "3.8B" in a filename.
_PARAM_LABEL_PATTERN = re.compile(r"(?:^|[._-])(\d+(?:\.\d+)?)[Bb](?:illion)?(?:[._-]|$)")

# The first four bytes of every valid GGUF file (https://github.com/ggml-org/ggml/blob/master/docs/gguf.md).
GGUF_MAGIC = b"GGUF"


def is_valid_gguf_file(path: Path) -> bool:
    """Cheap structural check: does this file start with the GGUF magic bytes?

    Used to keep files that aren't actually GGUF models (e.g. an HTML page
    saved with a ``.gguf`` name after a bad download link) out of the model
    list entirely, rather than listing something that will only fail once
    the user tries to chat with it.
    """
    try:
        with path.open("rb") as handle:
            return handle.read(len(GGUF_MAGIC)) == GGUF_MAGIC
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class GGUFMetadata:
    architecture: str | None
    context_length: int | None
    quantization_label: str | None
    parameter_size_label: str | None


# --- bounded key-value reader -------------------------------------------------
#
# Deliberately separate from the strict validator in download.py. That one
# proves a *downloaded* file is structurally sound and raises on anything odd;
# this one reads four keys off a file already on disk and gives up quietly.
# Different jobs, different error contracts.

_GGUF_HEADER_BYTES = 24
_SCALAR_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_TYPE_STRING = 8
_TYPE_ARRAY = 9
# A model's metadata block is small; these only bound a corrupt or hostile file
# so a bad length field cannot turn into an unbounded read or loop.
_MAX_ENTRIES = 100_000
_MAX_KEY_BYTES = 65_535
_MAX_STRING_BYTES = 16 * 1024 * 1024
_MAX_ARRAY_ELEMENTS = 100_000_000
_MAX_NESTING = 16


class _MalformedGGUF(Exception):
    """Internal: this file is not readable as GGUF metadata."""


def _exact(handle: BinaryIO, count: int) -> bytes:
    if count < 0:
        raise _MalformedGGUF("negative length")
    chunk = handle.read(count)
    if len(chunk) != count:
        raise _MalformedGGUF("truncated")
    return chunk


def _uint(handle: BinaryIO, format_string: str) -> int:
    return int(struct.unpack(format_string, _exact(handle, struct.calcsize(format_string)))[0])


def _string(handle: BinaryIO, *, max_bytes: int) -> bytes:
    length = _uint(handle, "<Q")
    if length > max_bytes:
        raise _MalformedGGUF("oversized string")
    return _exact(handle, length)


def _skip(handle: BinaryIO, count: int, end: int) -> None:
    """Seek ``count`` bytes forward, refusing a length the file cannot hold.

    A length read from the file is only a claim. Believed, 2**63 or more made
    ``seek`` itself raise (ValueError on a file, OverflowError in memory).
    Comparing with the file size alone keeps this to one comparison per
    value -- asking for the position here made a real vocabulary 2x slower --
    and a skip that lands past the end is caught by the next read, or by the
    final position check in _read_key_values_from.
    """
    if count > end:
        raise _MalformedGGUF("length larger than the file")
    handle.seek(count, 1)


def _skip_value(handle: BinaryIO, value_type: int, end: int, *, depth: int = 0) -> None:
    """Seek past one value without materialising it.

    Seeking rather than reading is the whole point: the tokenizer vocabulary
    is an array of a few hundred thousand strings, and only its length fields
    need to be read to step over it.
    """
    if value_type in _SCALAR_SIZES:
        _skip(handle, _SCALAR_SIZES[value_type], end)
        return
    if value_type == _TYPE_STRING:
        _skip(handle, _uint(handle, "<Q"), end)
        return
    if value_type != _TYPE_ARRAY:
        raise _MalformedGGUF(f"unknown value type {value_type}")
    if depth >= _MAX_NESTING:
        raise _MalformedGGUF("overly nested metadata")
    element_type = _uint(handle, "<I")
    count = _uint(handle, "<Q")
    if count > _MAX_ARRAY_ELEMENTS:
        raise _MalformedGGUF("oversized array")
    if element_type in _SCALAR_SIZES:
        # Fixed-width elements are one seek, however many there are.
        _skip(handle, _SCALAR_SIZES[element_type] * count, end)
        return
    for _ in range(count):
        _skip_value(handle, element_type, end, depth=depth + 1)


def _read_value(handle: BinaryIO, value_type: int, end: int) -> Any:
    if value_type == _TYPE_STRING:
        return _string(handle, max_bytes=_MAX_STRING_BYTES).decode("utf-8", "replace")
    formats = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
               6: "<f", 7: "<B", 10: "<Q", 11: "<q", 12: "<d"}
    if value_type in formats:
        return _uint(handle, formats[value_type]) if value_type not in (6, 12) else struct.unpack(
            formats[value_type], _exact(handle, _SCALAR_SIZES[value_type])
        )[0]
    _skip_value(handle, value_type, end)
    return None


# Everything a corrupt or hostile file can make the parsing below raise. The
# reader's contract is to give up quietly, and the folder scan depends on it.
_UNREADABLE = (
    OSError,
    struct.error,
    _MalformedGGUF,
    UnicodeError,
    MemoryError,
    RecursionError,
    ValueError,
    OverflowError,
)


def _read_key_values(path: Path) -> dict[str, Any] | None:
    """Return the keys this module needs, or ``None`` if the file is unreadable."""
    try:
        with path.open("rb") as handle:
            return _read_key_values_from(handle)
    except _UNREADABLE:
        return None


def _read_key_values_from(handle: BinaryIO) -> dict[str, Any] | None:
    """Read the wanted keys from an open handle, or ``None`` if it is unreadable.

    Every other key is stepped over, so the cost is proportional to the number
    of keys rather than to the size of the vocabulary behind them.
    """
    wanted_exact = {"general.architecture", "general.file_type"}
    found: dict[str, Any] = {}
    try:
        end = handle.seek(0, 2)
        handle.seek(0)
        magic, version, _tensor_count, metadata_count = struct.unpack(
            "<4sIQQ", _exact(handle, _GGUF_HEADER_BYTES)
        )
        if magic != GGUF_MAGIC:
            return None
        if version not in (2, 3):
            return None
        if metadata_count > _MAX_ENTRIES:
            return None
        for _ in range(metadata_count):
            key = _string(handle, max_bytes=_MAX_KEY_BYTES).decode("utf-8", "replace")
            value_type = _uint(handle, "<I")
            # context_length is namespaced by architecture, which is not
            # guaranteed to appear first, so take any of them and pick the
            # matching one once the whole block has been read.
            if key in wanted_exact or key.endswith(".context_length"):
                found[key] = _read_value(handle, value_type, end)
            else:
                _skip_value(handle, value_type, end)
        if handle.tell() > end:
            # The last value claimed bytes the file does not have.
            return None
    except _UNREADABLE:
        return None
    return found


def read_gguf_metadata(path: Path) -> GGUFMetadata | None:
    """Read key-value metadata off a GGUF file without loading tensor weights.

    Never raises: an unreadable or corrupt file returns ``None`` so a single
    bad file in the models directory can't break the whole folder scan.
    """
    values = _read_key_values(path)
    if values is None:
        logger.warning("Failed to read GGUF metadata for %s.", path.name)
        return None
    raw_architecture = values.get("general.architecture")
    architecture = str(raw_architecture).strip() or None if raw_architecture is not None else None
    context_length = None
    if architecture:
        context_length = _coerce_int(values.get(f"{architecture}.context_length"))
    if context_length is None:
        # Some files namespace it differently from general.architecture; any
        # single context_length key is unambiguous enough to use.
        lengths = [v for k, v in values.items() if k.endswith(".context_length")]
        if len(lengths) == 1:
            context_length = _coerce_int(lengths[0])
    return GGUFMetadata(
        architecture=architecture,
        context_length=context_length,
        quantization_label=_quantization_label(values.get("general.file_type"), path),
        parameter_size_label=_parameter_size_label(path),
    )


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _quantization_label(raw_file_type: Any, path: Path) -> str | None:
    """Name the quantisation, preferring the file's own declaration.

    The ``gguf`` package is still the source of the label names, but it is only
    consulted for this one small lookup rather than to parse the file.
    """
    file_type = _coerce_int(raw_file_type)
    if file_type is not None:
        try:
            import gguf  # local import: keep the dependency off the import path

            return gguf.LlamaFileType(file_type).name.removeprefix("MOSTLY_")
        except (ImportError, ValueError):
            pass
    match = _QUANT_LABEL_PATTERN.search(path.stem)
    return match.group(1).upper() if match else None


def _parameter_size_label(path: Path) -> str | None:
    match = _PARAM_LABEL_PATTERN.search(path.stem)
    return f"{match.group(1)}B" if match else None
