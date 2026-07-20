"""Canonical local paths without a dependency on a UI framework."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import sys


ORGANIZATION_NAME = "ChatLLM"
APPLICATION_NAME = "ChatLLM-Assistant"


class AppPathError(RuntimeError):
    """Raised when Cortex cannot resolve a safe application-data directory."""


@dataclass(frozen=True, slots=True)
class AppPaths:
    """All durable Cortex paths derived from one explicit data directory."""

    data_dir: Path

    @classmethod
    def from_data_dir(cls, data_dir: str | os.PathLike[str]) -> "AppPaths":
        """Create paths rooted at an injected directory without touching disk."""
        value = Path(data_dir).expanduser()
        if not value.is_absolute():
            value = value.resolve(strict=False)
        return cls(data_dir=value)

    @classmethod
    def for_windows(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "AppPaths":
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
    ) -> "AppPaths":
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
    def vector_database(self) -> Path:
        """Retain the dormant legacy path without enabling vector memory."""
        return self.data_dir / "cortex_vectors.sqlite"

    @property
    def webview_profile(self) -> Path:
        """Keep native webview state isolated from every installed browser profile."""
        return self.data_dir / "webview"

    def ensure_data_dir(self) -> Path:
        """Create the data root only when a caller explicitly requests it."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir
