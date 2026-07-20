"""Typed settings and isolated legacy QSettings adapter tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QSettings
from pydantic import ValidationError

from cortex_backend.core.settings import (
    AppearanceSettings,
    CortexSettings,
    GenerationSettings,
    MemorySettings,
    ModelSettings,
    OnboardingSettings,
    SuggestionSettings,
    TranslationSettings,
)
from cortex_backend.repositories.settings import SettingsRepository
from qt_settings_adapter import QSettingsAdapter


class TypedSettingsTests(unittest.TestCase):
    def test_defaults_match_the_legacy_runtime(self):
        settings = CortexSettings()

        self.assertEqual(settings.appearance.theme, "light")
        self.assertFalse(settings.onboarding.agreement_accepted)
        self.assertEqual(settings.models.chat, "qwen3:8b")
        self.assertEqual(settings.models.title, "granite4:tiny-h")
        self.assertEqual(settings.models.translation, "translategemma:4b")
        self.assertEqual(settings.generation.temperature, 0.7)
        self.assertEqual(settings.generation.num_ctx, 4096)
        self.assertEqual(settings.generation.seed, -1)
        self.assertTrue(settings.memory.enabled)
        self.assertFalse(settings.translation.enabled)
        self.assertEqual(settings.translation.target_language, "Spanish")
        self.assertTrue(settings.suggestions.enabled)
        self.assertEqual(settings.suggestions.model, "qwen3:8b")

    def test_generation_limits_match_the_current_qt_controls(self):
        with self.assertRaises(ValidationError):
            GenerationSettings(temperature=2.01)
        with self.assertRaises(ValidationError):
            GenerationSettings(num_ctx=1024)
        with self.assertRaises(ValidationError):
            GenerationSettings(seed=2147483648)
        with self.assertRaises(ValidationError):
            GenerationSettings(system_instructions="x" * 1801)


class QSettingsAdapterTests(unittest.TestCase):
    def _settings(self, directory: str) -> QSettings:
        return QSettings(
            str(Path(directory) / "legacy.ini"),
            QSettings.Format.IniFormat,
        )

    def test_valid_legacy_values_map_to_every_typed_field(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = self._settings(directory)
            values = {
                "theme": "dark",
                "agreement_accepted": "true",
                "chat_model": "gemma3:4b",
                "temperature": "1.25",
                "num_ctx": "8192",
                "seed": "42",
                "user_system_instructions": "Keep this exact text.",
                "memories_enabled": "false",
                "translation_enabled": "true",
                "target_language": "French",
                "suggestions_enabled": "false",
                "suggestions_model": "qwen3:4b",
            }
            for key, value in values.items():
                legacy.setValue(key, value)
            legacy.sync()

            adapter = QSettingsAdapter(legacy)
            result = adapter.load()

            self.assertIsInstance(adapter, SettingsRepository)
            self.assertEqual(result.invalid_keys, ())
            self.assertEqual(set(result.present_keys), set(values))
            self.assertEqual(result.settings.appearance.theme, "dark")
            self.assertTrue(result.settings.onboarding.agreement_accepted)
            self.assertEqual(result.settings.models.chat, "gemma3:4b")
            self.assertEqual(result.settings.generation.temperature, 1.25)
            self.assertEqual(result.settings.generation.num_ctx, 8192)
            self.assertEqual(result.settings.generation.seed, 42)
            self.assertEqual(
                result.settings.generation.system_instructions,
                "Keep this exact text.",
            )
            self.assertFalse(result.settings.memory.enabled)
            self.assertTrue(result.settings.translation.enabled)
            self.assertEqual(result.settings.translation.target_language, "French")
            self.assertFalse(result.settings.suggestions.enabled)
            self.assertEqual(result.settings.suggestions.model, "qwen3:4b")

    def test_invalid_fields_fall_back_individually_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = self._settings(directory)
            values = {
                "theme": "ultraviolet",
                "chat_model": "gemma3:4b",
                "temperature": "hot",
                "num_ctx": "1024",
                "seed": "2147483648",
                "memories_enabled": "maybe",
                "target_language": "German",
            }
            for key, value in values.items():
                legacy.setValue(key, value)
            legacy.setValue("unknown_key", "leave-me-alone")
            legacy.sync()
            before = {key: legacy.value(key) for key in legacy.allKeys()}

            result = QSettingsAdapter(legacy).load()
            after = {key: legacy.value(key) for key in legacy.allKeys()}

            self.assertEqual(before, after)
            self.assertEqual(
                set(result.invalid_keys),
                {"theme", "temperature", "num_ctx", "seed", "memories_enabled"},
            )
            self.assertEqual(result.settings.appearance.theme, "light")
            self.assertEqual(result.settings.models.chat, "gemma3:4b")
            self.assertEqual(result.settings.generation.temperature, 0.7)
            self.assertEqual(result.settings.generation.num_ctx, 4096)
            self.assertEqual(result.settings.generation.seed, -1)
            self.assertTrue(result.settings.memory.enabled)
            self.assertEqual(result.settings.translation.target_language, "German")

    def test_missing_suggestion_model_follows_the_loaded_chat_model(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = self._settings(directory)
            legacy.setValue("chat_model", "gemma3:12b")
            legacy.sync()

            result = QSettingsAdapter(legacy).load()

            self.assertEqual(result.settings.models.chat, "gemma3:12b")
            self.assertEqual(result.settings.suggestions.model, "gemma3:12b")

    def test_save_round_trips_known_fields_without_deleting_unknown_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = self._settings(directory)
            legacy.setValue("unknown_key", "preserve")
            settings = CortexSettings(
                appearance=AppearanceSettings(theme="dark"),
                onboarding=OnboardingSettings(agreement_accepted=True),
                models=ModelSettings(chat="gemma3:4b"),
                generation=GenerationSettings(
                    temperature=0.25,
                    num_ctx=16384,
                    seed=123,
                    system_instructions="Be direct.",
                ),
                memory=MemorySettings(enabled=False),
                translation=TranslationSettings(
                    enabled=True,
                    target_language="Japanese",
                ),
                suggestions=SuggestionSettings(
                    enabled=False,
                    model="qwen3:4b",
                ),
            )
            adapter = QSettingsAdapter(legacy)

            adapter.save(settings)
            loaded = adapter.load().settings

            self.assertEqual(legacy.value("unknown_key"), "preserve")
            self.assertEqual(legacy.value("agreement_accepted"), "true")
            self.assertEqual(legacy.value("memories_enabled"), "false")
            self.assertEqual(legacy.value("translation_enabled"), "true")
            self.assertEqual(legacy.value("suggestions_enabled"), "false")
            self.assertEqual(loaded, settings)


if __name__ == "__main__":
    unittest.main()
