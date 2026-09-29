"""Canonical local paths without a dependency on a UI framework."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import getpass
import os
from pathlib import Path
import stat
import subprocess
import sys
from pathlib import PureWindowsPath


ORGANIZATION_NAME = "ChatLLM"
APPLICATION_NAME = "ChatLLM-Assistant"

# The reproducible folders that may live under the local cache root.
_WEBVIEW_FOLDER = "webview"
_LLAMACPP_RUNTIME_FOLDER = "llamacpp_runtime"
_GGUF_MODELS_FOLDER = "gguf_models"
_CACHE_FOLDERS = (_WEBVIEW_FOLDER, _LLAMACPP_RUNTIME_FOLDER, _GGUF_MODELS_FOLDER)


class AppPathError(RuntimeError):
    """Raised when Cortex cannot resolve a safe application-data directory."""


_WINDOWS_REPARSE_POINT = 0x0400
_WINDOWS_PRIVATE_GROUP_SIDS = (
    "*S-1-1-0",       # Everyone
    "*S-1-5-11",      # Authenticated Users
    "*S-1-5-32-545",  # Built-in Users
)


def _running_on_windows() -> bool:
    return sys.platform == "win32"


def _is_unc_path(value: str | os.PathLike[str]) -> bool:
    return PureWindowsPath(str(value)).anchor.startswith("\\\\")


def _exists(path: Path) -> bool:
    """Whether ``path`` exists; a path that cannot even be inspected does not."""
    try:
        return path.exists()
    except OSError:
        return False


def _canonical_data_root(data_dir: str | os.PathLike[str]) -> Path:
    value = Path(data_dir).expanduser()
    if _running_on_windows() and _is_unc_path(value):
        raise AppPathError("Cortex data directories cannot use UNC paths.")
    if not value.is_absolute():
        value = Path.cwd() / value

    # Check existing components before resolving so a junction/symlink cannot
    # silently redirect a custom root to another user's data.
    current = Path(value.anchor) if value.anchor else Path(Path.cwd().anchor)
    for component in value.parts[1:] if value.anchor else value.parts:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise AppPathError("Cortex could not inspect the data directory.") from exc
        if stat.S_ISLNK(info.st_mode) or (
            _running_on_windows()
            and bool(getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT)
        ):
            raise AppPathError("Cortex data directories cannot traverse reparse points.")
    try:
        return value.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise AppPathError("Cortex could not canonicalize the data directory.") from exc


def secure_private_path(path: str | os.PathLike[str], *, directory: bool) -> Path:
    """Apply a per-user ACL/mode to a Cortex-owned path, failing closed."""

    target = Path(path)
    try:
        if _running_on_windows():
            identity = os.environ.get("USERDOMAIN", "").strip()
            try:
                username = getpass.getuser().strip()
            except (KeyError, OSError, RuntimeError) as exc:
                raise AppPathError("Cortex could not identify the current Windows user.") from exc
            if not username or any(char in username for char in '\"\r\n'):
                raise AppPathError("Cortex could not identify the current Windows user.")
            account = f"{identity}\\{username}" if identity else username
            rights = "(OI)(CI)F" if directory else "F"
            result = subprocess.run(
                [
                    "icacls",
                    str(target),
                    "/inheritance:r",
                    "/grant:r",
                    f"{account}:{rights}",
                    "/remove:g",
                    *_WINDOWS_PRIVATE_GROUP_SIDS,
                ],
                check=False,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=15,
            )
            if result.returncode != 0:
                raise AppPathError("Cortex could not secure its private data permissions.")
        else:
            os.chmod(target, 0o700 if directory else 0o600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AppPathError("Cortex could not secure its private data permissions.") from exc
    return target


@dataclass(frozen=True, slots=True)
class AppPaths:
    """All durable Cortex paths derived from one explicit data directory.

    Small, precious state (chats, settings, memories, execution records) lives
    under ``data_dir``. Large, reproducible caches -- the llama.cpp runtime,
    downloaded GGUF models and the WebView profile -- live under ``cache_dir``,
    which for a normal Windows install is in ``%LOCALAPPDATA%`` so that a
    roaming profile or Folder Redirection never synchronises gigabytes of
    binaries at logon. Paths built from an explicit directory (``--data-dir``,
    tests) keep everything together under it.
    """

    data_dir: Path
    # Local (non-roaming) root for the cache folders, or None to keep them under
    # ``data_dir`` as every earlier release did.
    cache_root: Path | None = None
    # True when %APPDATA% was a network path and ``data_dir`` is under
    # %LOCALAPPDATA% instead, so the launcher can say so in its log.
    local_fallback: bool = False
    # Where each cache folder lives once ``with_resolved_caches`` has decided it
    # (folder name, path). Empty until then, and every cache property answers
    # from what is on disk at the time it is asked.
    resolved_caches: tuple[tuple[str, Path], ...] = ()

    @classmethod
    def from_data_dir(cls, data_dir: str | os.PathLike[str]) -> AppPaths:
        """Create paths rooted at an injected directory without touching disk."""
        return cls(data_dir=_canonical_data_root(data_dir))

    @classmethod
    def for_windows(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> AppPaths:
        """Match Qt's Windows AppDataLocation for the legacy Cortex identity.

        The data directory stays where it always was, under ``%APPDATA%``. Two
        things come from ``%LOCALAPPDATA%``: the cache root for new caches, and
        -- only when ``%APPDATA%`` is a network path, which Cortex refuses to
        keep a database on -- the data directory itself, so a redirected
        profile no longer stops startup.
        """
        environment = os.environ if environ is None else environ
        app_data = str(environment.get("APPDATA", "")).strip()
        local_app_data = str(environment.get("LOCALAPPDATA", "")).strip()
        if not app_data:
            raise AppPathError(
                "Cortex could not resolve APPDATA for the current Windows user."
            )
        identity = Path(ORGANIZATION_NAME) / APPLICATION_NAME
        cache_root: Path | None = None
        if local_app_data:
            try:
                cache_root = _canonical_data_root(Path(local_app_data) / identity)
            except AppPathError:
                # An unusable local folder must not stop a launch that worked
                # before it existed: keep the caches with the data.
                cache_root = None
        if _is_unc_path(app_data):
            if cache_root is None:
                raise AppPathError(
                    "APPDATA is a network path and Cortex could not use "
                    "LOCALAPPDATA instead."
                )
            return cls(data_dir=cache_root, cache_root=cache_root, local_fallback=True)
        return cls(
            data_dir=_canonical_data_root(Path(app_data) / identity),
            cache_root=cache_root,
        )

    @classmethod
    def for_current_user(
        cls,
        *,
        platform: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> AppPaths:
        """Resolve production paths for the currently supported platform."""
        current_platform = sys.platform if platform is None else platform
        if current_platform != "win32":
            raise AppPathError(
                "This Cortex release supports automatic data-path resolution "
                "on Windows only; inject AppPaths for tests or other platforms."
            )
        return cls.for_windows(environ)

    @property
    def database(self) -> Path:
        return self.data_dir / "cortex_db.sqlite"

    @property
    def legacy_chat_history(self) -> Path:
        return self.data_dir / "chat_history"

    @property
    def permanent_memory(self) -> Path:
        return self.data_dir / "memory_bank.json"

    @property
    def permanent_memory_backup(self) -> Path:
        return self.data_dir / "memory_bank.json.bak"

    @property
    def settings_database(self) -> Path:
        """Settings kept out of the chat database.

        Settings writes take a full-file backup copy first. Colocating them
        with chat history meant every settings save byte-copied the entire
        transcript store -- slow, disk-doubling, and able to fail a theme
        toggle outright once the chat database grew large.
        """
        return self.data_dir / "cortex_settings.sqlite"

    @property
    def execution_database(self) -> Path:
        """Durable execution state kept separate from chat/settings data."""
        return self.data_dir / "execution.sqlite"

    @property
    def execution_artifacts(self) -> Path:
        """Generated artifact root; callers must still enforce per-artifact limits."""
        return self.data_dir / "execution_artifacts"

    @property
    def recipe_bundle_store(self) -> Path:
        """Durable signed recipe generations and their verified activation state."""
        return self.data_dir / "recipe_bundles"

    @property
    def cache_dir(self) -> Path:
        """Where new caches go: the local root, else the data directory."""
        return self.cache_root if self.cache_root is not None else self.data_dir

    def _cache_child(self, name: str) -> Path:
        """Locate one cache folder without ever moving an existing one.

        Earlier releases kept these folders inside ``data_dir``. A folder that is
        already there keeps being used where it is -- nothing is moved, copied
        or deleted -- and only a folder that exists nowhere yet is created under
        ``cache_dir``. A folder already under ``cache_dir`` always wins, so a new
        install that has started filling it keeps it.

        This is a live look at the disk: a legacy folder that appears later
        moves the answer for a folder that does not exist under ``cache_dir``
        yet. Code that must agree with itself over a whole run therefore uses
        ``with_resolved_caches``, which decides once; the launcher does that
        before anything reads a cache folder.
        """
        for pinned_name, pinned in self.resolved_caches:
            if pinned_name == name:
                return pinned
        legacy = self.data_dir / name
        if self.cache_root is None or self.cache_root == self.data_dir:
            return legacy
        local = self.cache_root / name
        if _exists(local) or not _exists(legacy):
            return local
        return legacy

    @property
    def webview_profile(self) -> Path:
        """Keep native webview state isolated from every installed browser profile."""
        return self._cache_child(_WEBVIEW_FOLDER)

    @property
    def llamacpp_runtime_dir(self) -> Path:
        """Cached, app-managed llama-server binaries. Never user-facing."""
        return self._cache_child(_LLAMACPP_RUNTIME_FOLDER)

    @property
    def default_gguf_models_dir(self) -> Path:
        """Default GGUF drop/download folder when ModelSettings.gguf_directory is unset."""
        return self._cache_child(_GGUF_MODELS_FOLDER)

    def with_resolved_caches(self) -> AppPaths:
        """The same paths with where each cache folder lives decided once, now.

        Nothing is created, moved or copied: each folder is located exactly as
        ``_cache_child`` does, and the answer is then kept on the returned paths
        so a folder cannot be found in one place by one consumer and in another
        by the next. Resolving paths that are already resolved changes nothing.
        """
        return replace(
            self,
            resolved_caches=tuple((name, self._cache_child(name)) for name in _CACHE_FOLDERS),
        )

    def without_cache_root(self) -> AppPaths:
        """The same paths with every cache folder kept under ``data_dir``."""
        return replace(self, cache_root=None, resolved_caches=())

    def ensure_data_dir(self) -> Path:
        """Create the data root only when a caller explicitly requests it."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppPathError("Cortex could not create its data directory.") from exc
        # Re-check after creation to catch a raced replacement/reparse point.
        canonical = _canonical_data_root(self.data_dir)
        if canonical != self.data_dir or not canonical.is_dir():
            raise AppPathError("Cortex data directory changed while it was being prepared.")
        secure_private_path(canonical, directory=True)
        return canonical

    def ensure_cache_dir(self) -> Path:
        """Create the local cache root with the same checks and ACL as the data root.

        A no-op that returns ``data_dir`` when caches share the data directory.
        """
        if self.cache_root is None or self.cache_root == self.data_dir:
            return self.data_dir
        try:
            self.cache_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppPathError("Cortex could not create its local cache directory.") from exc
        canonical = _canonical_data_root(self.cache_root)
        if canonical != self.cache_root or not canonical.is_dir():
            raise AppPathError("Cortex cache directory changed while it was being prepared.")
        secure_private_path(canonical, directory=True)
        return canonical
