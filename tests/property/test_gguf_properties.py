"""Property tests for the two GGUF readers.

A GGUF file is bytes from the network, or from a folder the user pointed Cortex
at, and both readers walk its length fields:

* ``_validate_gguf_file`` (``llamacpp/download.py``) is the strict gate a
  download passes before it is published into the models folder. It must raise
  only ``GGUFDownloadError``.
* ``read_gguf_metadata`` (``llamacpp/gguf_metadata.py``) reads four values off a
  file already on disk for the model list. It must never raise at all: one bad
  file cannot be allowed to break the folder scan.

The example files are assembled byte by byte, not with ``gguf.GGUFWriter``, so a
test can put any value in any field: well-formed files (of every metadata type,
nested arrays included, with real tensors), and then the same files truncated,
bit-flipped, or with a length field overwritten by a hostile number.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct
import tracemalloc
from pathlib import Path

import gguf
from hypothesis import example, given, strategies as st
import pytest

from cortex_backend.llamacpp import gguf_metadata
from cortex_backend.llamacpp.download import GGUFDownloadError, _validate_gguf_file
from cortex_backend.llamacpp.gguf_metadata import GGUFMetadata, read_gguf_metadata


@pytest.fixture(scope="module")
def candidate(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One file every example is written to; the readers only ever see a path."""

    return tmp_path_factory.mktemp("gguf-properties") / "candidate.gguf"


# -- Building files ---------------------------------------------------------------

_MAGIC = b"GGUF"
_DEFAULT_ALIGNMENT = 32
_TYPE_IDS = {
    "u8": 0, "i8": 1, "u16": 2, "i16": 3, "u32": 4, "i32": 5, "f32": 6, "bool": 7,
    "string": 8, "u64": 10, "i64": 11, "f64": 12,
}
_FORMATS = {
    "u8": "<B", "i8": "<b", "u16": "<H", "i16": "<h", "u32": "<I", "i32": "<i",
    "f32": "<f", "u64": "<Q", "i64": "<q", "f64": "<d",
}
_INTEGER_RANGES = {
    "u8": (0, 2**8 - 1), "i8": (-(2**7), 2**7 - 1), "u16": (0, 2**16 - 1), "i16": (-(2**15), 2**15 - 1),
    "u32": (0, 2**32 - 1), "i32": (-(2**31), 2**31 - 1), "u64": (0, 2**64 - 1), "i64": (-(2**63), 2**63 - 1),
}
# Element types with a known size whose tensors the validator can measure.
_TENSOR_TYPES = {0: 4, 1: 2}  # F32, F16: (id, bytes per element)
_ALIGNMENTS = (1, 2, 4, 8, 16, 32, 64, 128)
# Floats an integer conversion chokes on, offered often enough to be found.
_SPECIAL_FLOATS = (math.inf, -math.inf, math.nan, 0.0, -0.0)


def _string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _scalar(draw, kind: str) -> bytes:
    if kind == "bool":
        return bytes([draw(st.integers(0, 1))])
    if kind == "string":
        return _string(draw(st.text(max_size=16)))
    if kind in ("f32", "f64"):
        number = st.floats(width=32 if kind == "f32" else 64)
        return struct.pack(_FORMATS[kind], draw(st.one_of(st.sampled_from(_SPECIAL_FLOATS), number)))
    low, high = _INTEGER_RANGES[kind]
    return struct.pack(_FORMATS[kind], draw(st.integers(low, high)))


def _array_body(draw, depth: int) -> bytes:
    """An array's element type, count and elements; arrays nest up to three deep."""

    kinds = [*_TYPE_IDS, *(["array"] if depth < 2 else [])]
    element = draw(st.sampled_from(kinds))
    count = draw(st.integers(0, 5))
    if element == "array":
        return struct.pack("<IQ", 9, count) + b"".join(_array_body(draw, depth + 1) for _ in range(count))
    return struct.pack("<IQ", _TYPE_IDS[element], count) + b"".join(_scalar(draw, element) for _ in range(count))


def _value(draw) -> tuple[int, bytes]:
    """Any metadata value, as ``(type id, encoded bytes)``."""

    kind = draw(st.sampled_from([*_TYPE_IDS, "array"]))
    if kind == "array":
        return 9, _array_body(draw, 0)
    return _TYPE_IDS[kind], _scalar(draw, kind)


def _odd_value(draw) -> tuple[int, bytes]:
    """A value for a key that should hold a string or an integer: often a float that has no integer form."""

    kind = draw(st.sampled_from(("float32", "float64", "any", "any")))
    if kind == "any":
        return _value(draw)
    special = struct.pack("<f" if kind == "float32" else "<d", draw(st.sampled_from(_SPECIAL_FLOATS)))
    return (_TYPE_IDS["f32"] if kind == "float32" else _TYPE_IDS["f64"]), special


def _entry(key: str, type_id: int, payload: bytes) -> bytes:
    return _string(key) + struct.pack("<I", type_id) + payload


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


@dataclass(frozen=True)
class _Built:
    data: bytes
    architecture: str
    context_length: int | None
    quantization: str | None


_EXTRA_KEYS = st.from_regex(r"[a-z0-9_.]{0,20}", fullmatch=True).map(lambda tail: f"x.{tail}").filter(
    lambda key: not key.endswith(".context_length")
)
_FILE_TYPES = st.sampled_from(sorted({member.value for member in gguf.LlamaFileType}))


@st.composite
def _well_formed_files(draw, odd_values: bool = False) -> _Built:
    """A GGUF file the strict validator must accept, with the metadata it must yield.

    With ``odd_values`` the three keys the metadata reader looks for carry any
    type at all -- strings, infinite and NaN floats, booleans, nested arrays --
    so the file is still well formed but says nothing sensible, and no metadata
    is expected of it.
    """

    version = draw(st.sampled_from((2, 3)))
    architecture = draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12))
    entries = [_entry("general.architecture", 8, _string(architecture))]

    context_length: int | None = None
    if draw(st.booleans()):
        kind = draw(st.sampled_from(("u16", "i16", "u32", "i32", "u64", "i64")))
        context_length = draw(st.integers(0, 2**15 - 1))
        entries.append(
            _entry(f"{architecture}.context_length", _TYPE_IDS[kind], struct.pack(_FORMATS[kind], context_length))
        )
    quantization: str | None = None
    if draw(st.booleans()):
        file_type = draw(_FILE_TYPES)
        quantization = gguf.LlamaFileType(file_type).name.removeprefix("MOSTLY_")
        entries.append(_entry("general.file_type", 4, struct.pack("<I", file_type)))
    if odd_values:
        entries = [
            _entry(key, *_odd_value(draw))
            for key in ("general.architecture", f"{architecture}.context_length", "general.file_type")
        ]
        context_length = quantization = None
    alignment = _DEFAULT_ALIGNMENT
    if draw(st.booleans()):
        alignment = draw(st.sampled_from(_ALIGNMENTS))
        entries.append(_entry("general.alignment", 4, struct.pack("<I", alignment)))
    for _ in range(draw(st.integers(0, 5))):
        type_id, payload = _value(draw)
        entries.append(_entry(draw(_EXTRA_KEYS), type_id, payload))
    entries = draw(st.permutations(entries))

    descriptors = b""
    data_size = 0
    tensors = draw(
        st.lists(
            st.tuples(
                st.text(alphabet="abcdefghijklmnopqrstuvwxyz._0123456789", min_size=1, max_size=20),
                st.lists(st.integers(1, 4), min_size=1, max_size=4),
                st.sampled_from(sorted(_TENSOR_TYPES)),
            ),
            max_size=3,
        )
    )
    for name, dimensions, tensor_type in tensors:
        offset = _align(data_size, alignment)
        elements = 1
        for extent in dimensions:
            elements *= extent
        data_size = offset + elements * _TENSOR_TYPES[tensor_type]
        descriptors += (
            _string(name)
            + struct.pack("<I", len(dimensions))
            + b"".join(struct.pack("<Q", extent) for extent in dimensions)
            + struct.pack("<IQ", tensor_type, offset)
        )

    head = struct.pack("<4sIQQ", _MAGIC, version, len(tensors), len(entries)) + b"".join(entries) + descriptors
    # The file ends with its last tensor: no slack, so every shorter file is cut.
    data = head + bytes(_align(len(head), alignment) - len(head)) + bytes(data_size)
    return _Built(data, architecture, context_length, quantization)


_HOSTILE_INTEGERS = (
    0, 1, 2, 3, 4, 7, 8, 9, 12, 16, 31, 32, 33, 255, 256, 65_535, 65_536, 2**24,
    2**31 - 1, 2**31, 2**32 - 1, 2**32, 2**63 - 1, 2**63, 2**64 - 1,
)


@st.composite
def _corrupted(draw, data: bytes) -> bytes:
    """``data`` after a few edits: flips, hostile numbers, cuts, insertions, deletions."""

    buffer = bytearray(data)
    for _ in range(draw(st.integers(1, 3))):
        if not buffer:
            break
        position = draw(st.integers(0, len(buffer) - 1))
        kind = draw(st.sampled_from(("flip", "u32", "u64", "truncate", "insert", "delete")))
        if kind == "flip":
            buffer[position] ^= draw(st.integers(1, 255))
        elif kind == "u32":
            buffer[position : position + 4] = struct.pack("<I", draw(st.sampled_from(_HOSTILE_INTEGERS)) & 0xFFFFFFFF)
        elif kind == "u64":
            buffer[position : position + 8] = struct.pack("<Q", draw(st.sampled_from(_HOSTILE_INTEGERS)))
        elif kind == "truncate":
            del buffer[position:]
        elif kind == "insert":
            buffer[position:position] = draw(st.binary(max_size=16))
        else:
            del buffer[position : position + draw(st.integers(1, 8))]
    return bytes(buffer)


@st.composite
def _mutants(draw) -> bytes:
    return draw(_corrupted(draw(_well_formed_files()).data))


_HEADER = struct.pack("<4sI", _MAGIC, 3)
_HOSTILE_HEADERS = st.builds(
    lambda version, tensors, entries, body: struct.pack("<4sIQQ", _MAGIC, version, tensors, entries) + body,
    st.sampled_from((2, 3)),
    st.sampled_from(_HOSTILE_INTEGERS),
    st.sampled_from(_HOSTILE_INTEGERS),
    st.binary(max_size=200),
)
_ARBITRARY_FILES = st.one_of(
    st.binary(max_size=300),
    st.binary(max_size=300).map(lambda tail: _HEADER + tail),
    _HOSTILE_HEADERS,
    _mutants(),
)


# -- Running the readers ------------------------------------------------------------


def _validate(path: Path, data: bytes) -> GGUFDownloadError | None:
    """The strict validator's typed rejection of ``data``, or ``None`` if it accepts it.

    Any other exception is deliberately left to propagate: it is the failure
    these tests exist to find.
    """

    path.write_bytes(data)
    try:
        _validate_gguf_file(path)
    except GGUFDownloadError as error:
        return error
    return None


def _read(path: Path, data: bytes) -> GGUFMetadata | None:
    path.write_bytes(data)
    return read_gguf_metadata(path)


# -- Well-formed files are accepted and read back -------------------------------------


@given(built=_well_formed_files())
def test_a_well_formed_file_is_accepted_and_its_metadata_read_back(candidate: Path, built: _Built) -> None:
    assert _validate(candidate, built.data) is None

    metadata = _read(candidate, built.data)

    assert metadata is not None
    assert metadata.architecture == built.architecture
    assert metadata.context_length == built.context_length
    assert metadata.quantization_label == built.quantization


@given(built=_well_formed_files(odd_values=True))
def test_any_value_under_a_wanted_key_never_breaks_the_reader(candidate: Path, built: _Built) -> None:
    assert _validate(candidate, built.data) is None

    # Well formed, so the model list must be able to describe it -- however odd.
    assert isinstance(_read(candidate, built.data), GGUFMetadata)


@st.composite
def _cut_files(draw) -> tuple[_Built, bytes]:
    built = draw(_well_formed_files())
    return built, built.data[: draw(st.integers(0, len(built.data) - 1))]


@given(cut=_cut_files())
def test_every_proper_prefix_of_a_valid_file_is_rejected_as_truncated(candidate: Path, cut) -> None:
    built, prefix = cut
    assert len(prefix) < len(built.data)

    assert _validate(candidate, built.data) is None, "the untruncated file must be valid for this to mean anything"
    rejection = _validate(candidate, prefix)

    assert rejection is not None, f"a file cut to {len(prefix)} of {len(built.data)} bytes was accepted"


# -- Arbitrary bytes ----------------------------------------------------------------


@given(data=_ARBITRARY_FILES)
@example(data=b"")
@example(data=_HEADER)
@example(data=struct.pack("<4sIQQ", _MAGIC, 3, 2**64 - 1, 2**64 - 1))
def test_arbitrary_bytes_fail_only_with_the_typed_error_and_never_break_the_reader(
    candidate: Path, data: bytes
) -> None:
    rejection = _validate(candidate, data)
    metadata = _read(candidate, data)

    assert metadata is None or isinstance(metadata, GGUFMetadata)
    if rejection is None:
        # A file that passed the download gate must be one the model list can read.
        assert metadata is not None
    else:
        assert str(rejection), "a rejection has to say why"
        assert rejection.user_message == str(rejection)


# -- Length fields are claims, not allocations ------------------------------------

_ALLOCATION_SLACK = 1024 * 1024


def _peak_allocation(action) -> int:
    """The most memory ``action`` held at once, in bytes, above what was held before it."""

    tracemalloc.start()
    try:
        before, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        action()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak - before


@given(data=st.one_of(_HOSTILE_HEADERS, _mutants()))
@example(data=struct.pack("<4sIQQ", _MAGIC, 3, 0, 1) + _entry("general.architecture", 8, struct.pack("<Q", 2**63)))
@example(data=struct.pack("<4sIQQ", _MAGIC, 3, 0, 1) + _entry("general.architecture", 8, struct.pack("<Q", 16 * 2**20)))
@example(data=struct.pack("<4sIQQ", _MAGIC, 3, 0, 1) + _entry("x.a", 9, struct.pack("<IQ", 0, 2**40)))
@example(data=struct.pack("<4sIQQ", _MAGIC, 3, 2**20, 0) + bytes(64))
def test_a_length_field_is_never_believed_beyond_the_documented_bounds(candidate: Path, data: bytes) -> None:
    candidate.write_bytes(data)
    assert len(data) < 4096

    validator_peak = _peak_allocation(lambda: _swallow(lambda: _validate_gguf_file(candidate)))
    reader_peak = _peak_allocation(lambda: read_gguf_metadata(candidate))

    # The validator checks every length against the bytes that are left before it
    # reads, so a small file cannot make it allocate more than a small file's worth.
    assert validator_peak < _ALLOCATION_SLACK, f"the strict validator allocated {validator_peak} bytes"
    # The reader believes a string length up to its own cap before reading, so that
    # cap is the most it may ever hold.
    assert reader_peak < gguf_metadata._MAX_STRING_BYTES + _ALLOCATION_SLACK, (
        f"the metadata reader allocated {reader_peak} bytes"
    )


def _swallow(action) -> None:
    try:
        action()
    except GGUFDownloadError:
        pass
