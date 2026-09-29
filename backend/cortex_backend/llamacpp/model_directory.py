"""Scan a configured folder of ``.gguf`` files into ``InstalledModel`` entries.

The configured directory is the single source of truth for both "which GGUF
models are available" (this module) and "where does a download land"
(``llamacpp/download.py``) -- dropping a file into the folder and finishing a
download both just mean "the next scan will find it."
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Iterator
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
# projector is a companion to a model, a non-first shard is one slice of one,
# and a split set with a part missing cannot be loaded at all. Listing any of
# them invites the user to select something llama-server cannot load on its
# own, which surfaces much later as an unexplained launch failure.
_PROJECTOR_NAME = re.compile(r"(^|[._-])mmproj([._-]|$)", re.IGNORECASE)
_SHARD_NAME = re.compile(r"^(?P<stem>.+)-(?P<index>\d{5})-of-(?P<total>\d{5})\.gguf$", re.IGNORECASE)
_PROJECTOR_ARCHITECTURES = frozenset({"clip"})


class _ShardKey(NamedTuple):
    """One split set: its shared name (case-folded, Windows ignores case) and part count."""

    stem: str
    total: int


def _parse_shard(name: str) -> tuple[_ShardKey, int] | None:
    """The set and 1-based part number named by a ``-NNNNN-of-NNNNN.gguf`` file name."""
    match = _SHARD_NAME.match(name)
    if match is None:
        return None
    return _ShardKey(match.group("stem").casefold(), int(match.group("total"))), int(match.group("index"))


def _shard_sets(names: Iterable[str]) -> dict[_ShardKey, set[int]]:
    """Which part numbers are present, per split set, among the given file names."""
    sets: dict[_ShardKey, set[int]] = {}
    for name in names:
        parsed = _parse_shard(name)
        if parsed is not None:
            key, index = parsed
            sets.setdefault(key, set()).add(index)
    return sets


def _shard_set_is_complete(key: _ShardKey, present: set[int]) -> bool:
    # Counting the present parts that fall inside 1..total keeps this
    # proportional to the files that exist, not to a total the name merely
    # claims -- and a set claiming zero parts is nonsense, never complete.
    return key.total >= 1 and sum(1 for index in present if 1 <= index <= key.total) == key.total


def _companion_reason(name: str, shard_sets: dict[_ShardKey, set[int]]) -> str | None:
    """Why a file name says it cannot be chosen as a model, or None when it can.

    Judged from names alone, so it is cheap enough to run for every file of
    every scan and on every request. ``shard_sets`` must describe the folder
    that holds the file.
    """
    if _PROJECTOR_NAME.search(name):
        return "it is a multimodal projector that goes with a model, not a model itself"
    parsed = _parse_shard(name)
    if parsed is None:
        return None
    key, index = parsed
    if not _shard_set_is_complete(key, shard_sets.get(key, set())):
        return "some parts of this split model are missing"
    if index != 1:
        # Only the first part names the set; llama-server opens the rest itself.
        return "it is one part of a split model; the first part names the whole set"
    return None


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

    A file the folder scan hides -- a projector, a later part of a split model,
    a split model with a part missing -- is refused as well, so a stale or
    hand-edited setting cannot select something that was never offered and that
    llama-server would only fail to load.
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
    reason = _unusable_as_a_model(candidate)
    if reason is not None:
        raise InvalidGGUFModelId(f"'{model_id}' cannot be used as a model: {reason}.")
    return candidate


def _unusable_as_a_model(path: Path) -> str | None:
    """The reason the scan would hide this existing file, or None when it is a model.

    Best effort: a folder or header that cannot be read is not evidence that the
    file is a companion, so the answer then is "usable" and llama-server has the
    final say.
    """
    try:
        siblings = [entry.name for entry in path.parent.iterdir()]
    except OSError:
        siblings = [path.name]
    reason = _companion_reason(path.name, _shard_sets(siblings))
    if reason is not None:
        return reason
    if _reads_as_projector(path):
        return "it is a multimodal projector that goes with a model, not a model itself"
    return None


def _reads_as_projector(path: Path) -> bool:
    """Whether the architecture recorded inside the file is a projector's."""
    try:
        metadata = read_gguf_metadata(path)
    except Exception:
        return False
    return metadata is not None and (metadata.architecture or "").lower() in _PROJECTOR_ARCHITECTURES


class GGUFModelDirectory:
    """Folder-scan-backed model source, mirroring ``ModelService``'s Ollama surface."""

    def __init__(self, directory: Callable[[], Path]) -> None:
        self._directory = directory
        self._cache: dict[str, tuple[_CacheKey, InstalledModel]] = {}
        # Split sets found incomplete by the previous scan. A scan runs on every
        # model-list request, so one that stays broken is reported once, not
        # once per scan; it is reported again if it is repaired and breaks again.
        self._reported_incomplete_sets: set[tuple[Path, _ShardKey]] = set()

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
        incomplete_sets: set[tuple[Path, _ShardKey]] = set()
        for path in self._candidates(root, incomplete_sets):
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
            try:
                model = self._build_installed_model(path, stat, relative)
            except Exception as exc:
                logger.warning(
                    "Skipping '%s' in the GGUF models folder (%s).",
                    relative,
                    type(exc).__name__,
                )
                continue
            if model is None:
                continue
            fresh_cache[relative] = (key, model)
            models.append(model)
        self._cache = fresh_cache
        self._reported_incomplete_sets = incomplete_sets
        return tuple(sorted(models, key=lambda item: item.name))

    def _candidates(self, root: Path, incomplete_sets: set[tuple[Path, _ShardKey]]) -> Iterator[Path]:
        """Yield the ``.gguf`` files worth inspecting, deepest folders last.

        Walks rather than globbing so the depth bound is enforced as it goes,
        and so an unreadable subfolder costs that subfolder instead of the
        whole scan. Names that identify a companion file rather than a model
        are dropped here; the ones that need the file's own metadata to
        identify are dropped in _build_installed_model.

        Split sets are judged per folder on every scan, never from the cache:
        whether a set is whole depends on files other than the one listed.
        ``incomplete_sets`` collects the ones that are not, so the caller can
        tell a set that has just broken from one already reported.
        """
        pending = [(root, 0)]
        while pending:
            folder, depth = pending.pop(0)
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                continue
            files: list[Path] = []
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
                files.append(entry)
            shard_sets = _shard_sets(entry.name for entry in files)
            for key, present in shard_sets.items():
                if _shard_set_is_complete(key, present):
                    continue
                incomplete_sets.add((folder, key))
                if (folder, key) not in self._reported_incomplete_sets:
                    logger.warning(
                        "Not listing a split model in the GGUF models folder: "
                        "some of its %d parts are missing. Put every part in "
                        "the same folder to use it.",
                        key.total,
                    )
            for entry in files:
                reason = _companion_reason(entry.name, shard_sets)
                if reason is not None:
                    logger.debug("Not listing '%s': %s.", entry.name, reason)
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

        try:
            metadata = read_gguf_metadata(path)
        except Exception as exc:
            # The reader promises not to raise; if it ever does, that costs
            # this file its details, never the rest of the scan.
            logger.warning(
                "Could not read the details of '%s' (%s); listing it without them.",
                relative,
                type(exc).__name__,
            )
            metadata = None
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
