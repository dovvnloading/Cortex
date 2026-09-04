"""Canonical local paths without a dependency on a UI framework."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import getpass
import os
from pathlib import Path
import stat
import subprocess
import sys
from pathlib import PureWindowsPath


ORGANIZATION_NAME = "ChatLLM"
APPLICATION_NAME = "ChatLLM-Assistant"


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


def _canonical_data_root(data_dir: str | os.PathLike[str]) -> Path:
    value = Path(data_dir).expanduser()
    if _running_on_windows() and PureWindowsPath(str(value)).anchor.startswith("\\\\"):
        raise AppPathError("Cortex data directories cannot use UNC paths.")
    if not value.is_absolute():
        value = Path.cwd() / value

    # Check existing components before resolving so a junction/symlink cannot
    # silently redirect a custom root to another user's data.
    current = Path(value.anchor) if value.anchor else Path.cwd().anchor
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
    """All durable Cortex paths derived from one explicit data directory."""

    data_dir: Path

    @classmethod
    def from_data_dir(cls, data_dir: str | os.PathLike[str]) -> AppPaths:
        """Create paths rooted at an injected directory without touching disk."""
        return cls(data_dir=_canonical_data_root(data_dir))

    @classmethod
    def for_windows(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> AppPaths:
        """Match Qt's Windows AppDataLocation for the legacy Cortex identity."""
        environment = os.environ if environ is None else environ
        app_data = str(environment.get("APPDATA", "")).strip()
        if not app_data:
            raise AppPathError(
                "Cortex could not resolve APPDATA for the current Windows user."
            )
        return cls.from_data_dir(
            Path(app_data) / ORGANIZATION_NAME / APPLICATION_NAME
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
        """Durable Phase 1 execution state kept separate from chat/settings data."""
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
    def webview_profile(self) -> Path:
        """Keep native webview state isolated from every installed browser profile."""
        return self.data_dir / "webview"

    @property
    def llamacpp_runtime_dir(self) -> Path:
        """Cached, app-managed llama-server binaries. Never user-facing."""
        return self.data_dir / "llamacpp_runtime"

    @property
    def default_gguf_models_dir(self) -> Path:
        """Default GGUF drop/download folder when ModelSettings.gguf_directory is unset."""
        return self.data_dir / "gguf_models"

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
