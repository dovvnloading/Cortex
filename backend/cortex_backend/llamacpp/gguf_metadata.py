"""Best-effort GGUF metadata reads via the ``gguf`` package.

``gguf.GGUFReader`` memory-maps the file and eagerly parses only key-value
metadata and tensor *descriptors* (not tensor weight data), so this reads
architecture/context-length/quantization/etc. off a multi-gigabyte model file
without loading any weights into memory.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def read_gguf_metadata(path: Path) -> GGUFMetadata | None:
    """Read key-value metadata off a GGUF file without loading tensor weights.

    Never raises: an unreadable or corrupt file returns ``None`` so a single
    bad file in the models directory can't break the whole folder scan.
    """
    try:
        import gguf  # local import: keep the dependency optional at module import time
    except ImportError:
        logger.warning(
            "The 'gguf' package is not installed; GGUF metadata will be unavailable."
        )
        return None
    try:
        reader = gguf.GGUFReader(str(path), mode="r")
    except Exception:
        logger.warning("Failed to open GGUF file for metadata: %s", path.name, exc_info=True)
        return None
    try:
        architecture = _field_value(reader, "general.architecture")
        architecture = str(architecture).strip() or None if architecture is not None else None
        context_length = None
        if architecture:
            context_length = _coerce_int(_field_value(reader, f"{architecture}.context_length"))
        return GGUFMetadata(
            architecture=architecture,
            context_length=context_length,
            quantization_label=_quantization_label(reader, path, gguf),
            parameter_size_label=_parameter_size_label(path),
        )
    except Exception:
        logger.warning("Failed to read GGUF metadata for %s.", path.name, exc_info=True)
        return None


def _field_value(reader: Any, key: str) -> Any:
    field = reader.get_field(key)
    if field is None:
        return None
    try:
        return field.contents()
    except Exception:
        return None


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _quantization_label(reader: Any, path: Path, gguf_module: Any) -> str | None:
    file_type = _coerce_int(_field_value(reader, "general.file_type"))
    if file_type is not None:
        try:
            return gguf_module.LlamaFileType(file_type).name.removeprefix("MOSTLY_")
        except ValueError:
            pass
    match = _QUANT_LABEL_PATTERN.search(path.stem)
    return match.group(1).upper() if match else None


def _parameter_size_label(path: Path) -> str | None:
    match = _PARAM_LABEL_PATTERN.search(path.stem)
    return f"{match.group(1)}B" if match else None
