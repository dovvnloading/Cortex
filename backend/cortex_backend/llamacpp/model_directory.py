"""Scan a configured folder of ``.gguf`` files into ``InstalledModel`` entries.

The configured directory is the single source of truth for both "which GGUF
models are available" (this module) and "where does a download land"
(``llamacpp/download.py``) -- dropping a file into the folder and finishing a
download both just mean "the next scan will find it."
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from cortex_backend.services.chat_client import GGUF_PREFIX
from cortex_backend.services.models import InstalledModel

from .gguf_metadata import is_valid_gguf_file, read_gguf_metadata

logger = logging.getLogger(__name__)


class InvalidGGUFModelId(ValueError):
    """Raised when a ``gguf:`` id doesn't resolve to a file in the configured directory."""


class _CacheKey(NamedTuple):
    size: int
    mtime_ns: int


def to_model_id(filename: str) -> str:
    return f"{GGUF_PREFIX}{filename}"


def resolve_configured_directory(configured: str | None, default_dir: Path) -> Path:
    """The one place "which folder is the GGUF models directory" is decided.

    Used both by whatever builds the ``models_directory`` callables passed
    into ``GGUFModelDirectory``/``LlamaServerManager``/``LlamaCppChatClient``
    and by the download route, so a download always lands exactly where the
    next folder scan will look for it.

    Forgiving on purpose: pointing this setting at a specific ``.gguf`` file
    rather than its containing folder is a very natural mistake -- the
    field is asking the user to "point Cortex at your model" -- and without
    this fallback the folder scan would silently see zero models (a
    directory listing on a file path just raises OSError, which the scan
    swallows to stay resilient) with no visible explanation. Using the
    file's parent directory instead turns that mistake into exactly what
    the user meant.
    """
    if configured:
        path = Path(configured).expanduser()
        if path.suffix.lower() == ".gguf" and not path.is_dir():
            return path.parent
        return path
    return default_dir


def resolve_gguf_path(directory: Path, model_id: str) -> Path:
    """Resolve a ``"gguf:<filename>"`` id to a verified path inside ``directory``.

    Rejects anything that isn't a bare filename directly inside ``directory``
    (no path separators, no ``..``, no absolute paths) so a hand-edited or
    otherwise adversarial settings blob can't be used to read/execute
    arbitrary files off the configured GGUF id alone.
    """
    if not model_id.startswith(GGUF_PREFIX):
        raise InvalidGGUFModelId(f"'{model_id}' is not a GGUF model id.")
    filename = model_id[len(GGUF_PREFIX):]
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise InvalidGGUFModelId(f"'{model_id}' is not a valid GGUF model id.")
    directory = directory.resolve()
    candidate = (directory / filename).resolve()
    if candidate.parent != directory:
        raise InvalidGGUFModelId(f"'{model_id}' does not resolve inside the configured directory.")
    if not candidate.is_file():
        raise InvalidGGUFModelId(f"The GGUF file for '{model_id}' was not found.")
    return candidate


class GGUFModelDirectory:
    """Folder-scan-backed model source, mirroring ``ModelService``'s Ollama surface."""

    def __init__(self, directory: Callable[[], Path]) -> None:
        self._directory = directory
        self._cache: dict[str, tuple[_CacheKey, InstalledModel]] = {}

    def list_installed_details(self) -> tuple[InstalledModel, ...]:
        """Scan ``*.gguf`` files (non-recursive) in the configured directory.

        Never raises: a missing directory or an unreadable file yields fewer
        entries, not an error, since this sits alongside an Ollama inventory
        that must still be usable on its own.
        """
        directory = self._directory()
        try:
            candidates = sorted(directory.glob("*.gguf"))
        except OSError:
            return ()
        models: list[InstalledModel] = []
        fresh_cache: dict[str, tuple[_CacheKey, InstalledModel]] = {}
        for path in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            key = _CacheKey(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
            cached = self._cache.get(path.name)
            if cached is not None and cached[0] == key:
                fresh_cache[path.name] = cached
                models.append(cached[1])
                continue
            if not is_valid_gguf_file(path):
                logger.warning(
                    "Skipping '%s' in the GGUF models folder: not a valid GGUF file "
                    "(missing magic header). It will not appear in the model list.",
                    path.name,
                )
                continue
            model = self._build_installed_model(path, stat)
            fresh_cache[path.name] = (key, model)
            models.append(model)
        self._cache = fresh_cache
        return tuple(sorted(models, key=lambda item: item.name))

    def resolve_path(self, model_id: str) -> Path:
        return resolve_gguf_path(self._directory(), model_id)

    @staticmethod
    def _build_installed_model(path: Path, stat) -> InstalledModel:
        from datetime import datetime, timezone

        metadata = read_gguf_metadata(path)
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        return InstalledModel(
            name=to_model_id(path.name),
            size=stat.st_size,
            modified_at=modified_at,
            capabilities=(),
            # No mmproj/vision support for GGUF in this build. False,
            # not None: the UI's attach gate tests `=== false`.
            supports_vision=False,
            parameter_size=metadata.parameter_size_label if metadata else None,
            quantization_level=metadata.quantization_label if metadata else None,
            family=metadata.architecture if metadata else None,
            context_length=metadata.context_length if metadata else None,
            source="gguf",
            path=str(path),
        )
