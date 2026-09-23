"""Scan a configured folder of ``.gguf`` files into ``InstalledModel`` entries.

The configured directory is the single source of truth for both "which GGUF
models are available" (this module) and "where does a download land"
(``llamacpp/download.py``) -- dropping a file into the folder and finishing a
download both just mean "the next scan will find it."
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
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


# How deep the scan descends below the configured folder. Models are kept one
# folder per repository by every tool that downloads them, so depth 1 already
# covers the normal layout; a little more absorbs "models/gguf/<repo>/file.gguf"
# without turning the scan loose on an arbitrarily deep tree.
MAX_SCAN_DEPTH = 3

# Files that are not chat models even though they end in .gguf. A multimodal
# projector is a companion to a model, and a non-first shard is one slice of
# one. Listing either invites the user to select something llama-server cannot
# load on its own, which surfaces much later as an unexplained crash-loop.
_PROJECTOR_NAME = re.compile(r"(^|[._-])mmproj([._-]|$)", re.IGNORECASE)
_SHARD_NAME = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)
_PROJECTOR_ARCHITECTURES = frozenset({"clip"})


def to_model_id(relative_path: str) -> str:
    """Build a ``gguf:`` id from a path relative to the configured folder.

    Separators are normalised to ``/`` so an id means the same thing however
    the host spells its paths, and so a stored setting stays valid across
    machines.
    """
    return f"{GGUF_PREFIX}{Path(relative_path).as_posix()}"


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
    """Resolve a ``"gguf:<relative path>"`` id to a verified path under ``directory``.

    Ids may name a file in a subfolder, because that is how every tool that
    downloads a model lays them out -- one folder per repository. What they may
    not do is leave the configured folder: absolute paths, drive letters, ``..``
    segments and links that resolve outside the root are all refused, so a
    hand-edited or otherwise adversarial settings blob cannot read or execute
    arbitrary files off the GGUF id alone.

    Containment is checked after resolving, not by inspecting the text, so a
    junction or symlink inside the folder cannot be used to step outside it.
    """
    if not model_id.startswith(GGUF_PREFIX):
        raise InvalidGGUFModelId(f"'{model_id}' is not a GGUF model id.")
    relative = model_id[len(GGUF_PREFIX):]
    if not relative:
        raise InvalidGGUFModelId(f"'{model_id}' is not a valid GGUF model id.")

    candidate_parts = PurePosixPath(relative.replace("\\", "/")).parts
    if not candidate_parts or any(part in ("", ".", "..") for part in candidate_parts):
        raise InvalidGGUFModelId(f"'{model_id}' is not a valid GGUF model id.")
    if PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).is_absolute():
        raise InvalidGGUFModelId(f"'{model_id}' is not a valid GGUF model id.")

    directory = directory.resolve()
    candidate = directory.joinpath(*candidate_parts).resolve()
    if not candidate.is_relative_to(directory):
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
        """Scan the configured directory, and its subfolders, for chat models.

        The scan used to look in exactly one folder. Nothing lays models out
        that way: every downloader writes one folder per repository, so a user
        pointing at their models root saw only whatever happened to be loose at
        the top level and none of the models in subfolders -- and pointing at
        one model's folder showed that model alone. Descending a bounded number
        of levels matches how the files actually sit on disk.

        Never raises: a missing directory or an unreadable file yields fewer
        entries, not an error, since this sits alongside an Ollama inventory
        that must still be usable on its own.
        """
        directory = self._directory()
        try:
            root = directory.resolve()
        except OSError:
            return ()
        models: list[InstalledModel] = []
        fresh_cache: dict[str, tuple[_CacheKey, InstalledModel]] = {}
        for path in self._candidates(root):
            try:
                stat = path.stat()
                relative = path.relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            key = _CacheKey(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
            cached = self._cache.get(relative)
            if cached is not None and cached[0] == key:
                fresh_cache[relative] = cached
                models.append(cached[1])
                continue
            if not is_valid_gguf_file(path):
                logger.warning(
                    "Skipping '%s' in the GGUF models folder: not a valid GGUF file "
                    "(missing magic header). It will not appear in the model list.",
                    relative,
                )
                continue
            model = self._build_installed_model(path, stat, relative)
            if model is None:
                continue
            fresh_cache[relative] = (key, model)
            models.append(model)
        self._cache = fresh_cache
        return tuple(sorted(models, key=lambda item: item.name))

    @staticmethod
    def _candidates(root: Path) -> Iterator[Path]:
        """Yield the ``.gguf`` files worth inspecting, deepest folders last.

        Walks rather than globbing so the depth bound is enforced as it goes,
        and so an unreadable subfolder costs that subfolder instead of the
        whole scan. Names that identify a companion file rather than a model
        are dropped here; the ones that need the file's own metadata to
        identify are dropped in _build_installed_model.
        """
        pending = [(root, 0)]
        while pending:
            folder, depth = pending.pop(0)
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir():
                        if depth < MAX_SCAN_DEPTH and not entry.name.startswith("."):
                            pending.append((entry, depth + 1))
                        continue
                    if entry.suffix.lower() != ".gguf":
                        continue
                except OSError:
                    continue
                if _PROJECTOR_NAME.search(entry.name):
                    logger.info(
                        "Skipping '%s': a multimodal projector is a companion "
                        "file, not a model that can be loaded on its own.",
                        entry.name,
                    )
                    continue
                shard = _SHARD_NAME.search(entry.name)
                if shard and shard.group(1) != "00001":
                    # Only the first shard names the set; llama-server opens
                    # the rest itself. Listing them invites the user to pick a
                    # slice of a model, which fails much later and unhelpfully.
                    continue
                yield entry

    def resolve_path(self, model_id: str) -> Path:
        return resolve_gguf_path(self._directory(), model_id)

    @staticmethod
    def _build_installed_model(path: Path, stat, relative: str) -> InstalledModel | None:
        """Describe one file, or return None when it is not a chat model.

        A multimodal projector usually says so in its filename and is dropped
        before this point, but not always -- the architecture recorded inside
        the file is what actually settles it.
        """
        from datetime import datetime, timezone

        metadata = read_gguf_metadata(path)
        if metadata is not None and (metadata.architecture or "").lower() in _PROJECTOR_ARCHITECTURES:
            logger.info(
                "Skipping '%s': its architecture is %r, a companion projector "
                "rather than a model that can be loaded on its own.",
                relative,
                metadata.architecture,
            )
            return None
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        return InstalledModel(
            name=to_model_id(relative),
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
