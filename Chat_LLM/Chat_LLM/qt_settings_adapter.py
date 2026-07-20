"""Temporary QSettings adapter for the staged Qt-to-web migration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QSettings
from pydantic import ValidationError

from cortex_backend.core.settings import CortexSettings
from cortex_backend.repositories.settings import (
    SettingsReadResult,
    SettingsRepositoryError,
)


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
    _LegacyField("suggestions_enabled", "suggestions", "enabled"),
    _LegacyField("suggestions_model", "suggestions", "model"),
)

_LEGACY_BOOLEAN_KEYS = {
    "agreement_accepted",
    "memories_enabled",
    "translation_enabled",
    "suggestions_enabled",
}


class QSettingsAdapter:
    """Read and write typed snapshots while the legacy Qt UI remains active."""

    def __init__(self, settings: QSettings | None = None):
        self._settings = settings if settings is not None else QSettings()

    @staticmethod
    def _normalize_legacy_value(legacy_field: _LegacyField, raw_value: Any) -> Any:
        if legacy_field.key not in _LEGACY_BOOLEAN_KEYS:
            return raw_value
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, str):
            normalized = raw_value.strip().lower()
            if normalized in {"true", "false"}:
                return normalized == "true"
        raise ValueError(f"{legacy_field.key} must be a legacy boolean")

    @staticmethod
    def _replace_field(
        current: CortexSettings,
        legacy_field: _LegacyField,
        raw_value: Any,
    ) -> CortexSettings:
        section = getattr(current, legacy_field.section)
        section_data = section.model_dump()
        section_data[legacy_field.field] = raw_value
        validated_section = type(section).model_validate(section_data)
        return current.model_copy(
            update={legacy_field.section: validated_section},
        )

    def load(self, *, defaults: CortexSettings | None = None) -> SettingsReadResult:
        current = defaults or CortexSettings()
        present: list[str] = []
        invalid: list[str] = []

        self._settings.sync()
        if self._settings.status() != QSettings.Status.NoError:
            raise SettingsRepositoryError("QSettings could not load Cortex settings.")

        for legacy_field in _LEGACY_FIELDS:
            if not self._settings.contains(legacy_field.key):
                continue
            present.append(legacy_field.key)
            raw_value = self._settings.value(legacy_field.key)
            try:
                raw_value = self._normalize_legacy_value(legacy_field, raw_value)
                current = self._replace_field(current, legacy_field, raw_value)
            except (ValidationError, TypeError, ValueError):
                invalid.append(legacy_field.key)

        if not self._settings.contains("suggestions_model"):
            suggestions = current.suggestions.model_copy(
                update={"model": current.models.chat},
            )
            current = current.model_copy(update={"suggestions": suggestions})

        return SettingsReadResult(
            settings=current,
            source="qsettings",
            present_keys=tuple(present),
            invalid_keys=tuple(invalid),
        )

    def save(self, settings: CortexSettings) -> None:
        if not isinstance(settings, CortexSettings):
            raise TypeError("settings must be a validated CortexSettings snapshot")

        values = {
            "theme": settings.appearance.theme,
            "agreement_accepted": self._legacy_bool(
                settings.onboarding.agreement_accepted
            ),
            "chat_model": settings.models.chat,
            "temperature": settings.generation.temperature,
            "num_ctx": settings.generation.num_ctx,
            "seed": settings.generation.seed,
            "user_system_instructions": settings.generation.system_instructions,
            "memories_enabled": self._legacy_bool(settings.memory.enabled),
            "translation_enabled": self._legacy_bool(settings.translation.enabled),
            "target_language": settings.translation.target_language,
            "suggestions_enabled": self._legacy_bool(settings.suggestions.enabled),
            "suggestions_model": settings.suggestions.model,
        }
        for key, value in values.items():
            self._settings.setValue(key, value)
        self._settings.sync()
        if self._settings.status() != QSettings.Status.NoError:
            raise SettingsRepositoryError("QSettings could not save Cortex settings.")

    @staticmethod
    def _legacy_bool(value: bool) -> str:
        return "true" if value else "false"
