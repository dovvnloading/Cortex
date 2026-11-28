"""Qt-free reader for the legacy Cortex QSettings data.

The old desktop release stored settings in the Windows per-user registry. The
test and recovery paths also support the INI representation produced by
QSettings. The reader is intentionally read-only: SQLite becomes the writable
settings source, while the legacy source remains available for rollback.
"""

from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from pydantic import ValidationError

from cortex_backend.core.paths import APPLICATION_NAME, ORGANIZATION_NAME
from cortex_backend.core.settings import CortexSettings

from .settings import SettingsReadResult, SettingsRepository, SettingsRepositoryError


@dataclass(frozen=True, slots=True)
class _LegacyField:
    key: str
    section: str
    field: str


_LEGACY_FIELDS = (
    _LegacyField("theme", "appearance", "theme"),
    _LegacyField("agreement_accepted", "onboarding", "agreement_accepted"),
    _LegacyField("chat_model", "models", "chat"),
    _LegacyField("temperature", "generation", "temperature"),
    _LegacyField("num_ctx", "generation", "num_ctx"),
    _LegacyField("seed", "generation", "seed"),
    _LegacyField("user_system_instructions", "generation", "system_instructions"),
    _LegacyField("memories_enabled", "memory", "enabled"),
    _LegacyField("translation_enabled", "translation", "enabled"),
    _LegacyField("target_language", "translation", "target_language"),
)

_LEGACY_BOOLEAN_KEYS = {
    "agreement_accepted",
    "memories_enabled",
    "translation_enabled",
}


# The Windows error codes the registry read tells apart.
_ERROR_FILE_NOT_FOUND = 2
_ERROR_NO_MORE_ITEMS = 259


def _read_registry_values(winreg: Any, registry_path: str) -> dict[str, Any]:
    """Every value under the legacy key, or ``{}`` when there is no such key.

    Only ``ERROR_NO_MORE_ITEMS`` ends the enumeration. Every ``OSError`` used
    to: an access-denied or I/O error halfway through returned the values
    read so far as if they were all of them, the missing ones became
    defaults, and that partial snapshot was then committed as the one-time
    migration, so the rest were never imported. Any other error now fails
    the read, which commits nothing and lets the next launch try again.
    A missing key is still a first run with nothing to import, but only
    when opening the key says so.
    """
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path)
    except OSError as exc:
        if _ERROR_FILE_NOT_FOUND in (getattr(exc, "winerror", None), getattr(exc, "errno", None)):
            return {}
        raise SettingsRepositoryError("Legacy Windows settings could not be read.") from exc
    values: dict[str, Any] = {}
    try:
        with key:
            index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError as exc:
                    if getattr(exc, "winerror", None) == _ERROR_NO_MORE_ITEMS:
                        break
                    raise
                values[name] = value
                index += 1
    except OSError as exc:
        raise SettingsRepositoryError("Legacy Windows settings could not be read.") from exc
    return values


class LegacySettingsReader(SettingsRepository):
    """Read legacy settings without importing a UI framework."""

    def __init__(self, source_path: str | Path | None = None):
        self.source_path = Path(source_path) if source_path is not None else None

    def _read_values(self) -> tuple[Mapping[str, Any], str]:
        if self.source_path is not None:
            parser = ConfigParser(interpolation=None)
            try:
                loaded = parser.read(self.source_path, encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise SettingsRepositoryError(
                    "Legacy settings could not be read."
                ) from exc
            if not loaded:
                raise SettingsRepositoryError("Legacy settings file was not found.")
            section = parser["General"] if parser.has_section("General") else {}
            return dict(section), "legacy_ini"

        if os.name != "nt":
            return {}, "legacy_registry_unavailable"

        import winreg

        # A first-run profile has no legacy key. Defaults are still a valid
        # settings snapshot and SQLite will record that no import was needed.
        registry_path = rf"Software\{ORGANIZATION_NAME}\{APPLICATION_NAME}"
        return _read_registry_values(winreg, registry_path), "legacy_registry"

    @staticmethod
    def _normalize_value(field: _LegacyField, raw_value: Any) -> Any:
        if field.key not in _LEGACY_BOOLEAN_KEYS:
            return raw_value
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, int) and raw_value in {0, 1}:
            return bool(raw_value)
        if isinstance(raw_value, str):
            normalized = raw_value.strip().lower()
            if normalized in {"true", "false"}:
                return normalized == "true"
            if normalized in {"1", "0"}:
                return normalized == "1"
        raise ValueError(f"{field.key} must be a legacy boolean")

    @staticmethod
    def _replace_field(
        current: CortexSettings,
        field: _LegacyField,
        raw_value: Any,
    ) -> CortexSettings:
        section = getattr(current, field.section)
        section_data = section.model_dump()
        section_data[field.field] = raw_value
        validated_section = type(section).model_validate(section_data)
        return current.model_copy(update={field.section: validated_section})

    def load(self, *, defaults: CortexSettings | None = None) -> SettingsReadResult:
        current = defaults or CortexSettings()
        values, source = self._read_values()
        present: list[str] = []
        invalid: list[str] = []

        normalized_values = {str(key): value for key, value in values.items()}
        for field in _LEGACY_FIELDS:
            if field.key not in normalized_values:
                continue
            present.append(field.key)
            try:
                current = self._replace_field(
                    current,
                    field,
                    self._normalize_value(field, normalized_values[field.key]),
                )
            except (ValidationError, TypeError, ValueError):
                invalid.append(field.key)

        return SettingsReadResult(
            settings=current,
            source=source,
            present_keys=tuple(present),
            invalid_keys=tuple(invalid),
        )

    def save(
        self, settings: CortexSettings, *, expected_revision: int | None = None
    ) -> None:
        raise SettingsRepositoryError(
            "Legacy settings are read-only; save through the SQLite repository."
        )
