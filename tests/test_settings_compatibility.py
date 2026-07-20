"""Typed settings and Qt-free legacy-settings compatibility tests."""

from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from cortex_backend.core.settings import (
    CortexSettings,
    GenerationSettings,
)
from cortex_backend.repositories.legacy_settings import LegacySettingsReader
from cortex_backend.repositories.settings import SettingsRepositoryError


def _write_ini(path: Path, values: dict[str, object]) -> None:
    path.write_text(
        "[General]\n" + "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )


class TypedSettingsTests(unittest.TestCase):
    def test_defaults_match_the_web_runtime_contract(self):
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

    def test_generation_limits_remain_validated(self):
        with self.assertRaises(ValidationError):
            GenerationSettings(temperature=2.01)
        with self.assertRaises(ValidationError):
            GenerationSettings(num_ctx=1024)
        with self.assertRaises(ValidationError):
            GenerationSettings(seed=2147483648)
        with self.assertRaises(ValidationError):
            GenerationSettings(system_instructions="x" * 1801)


class LegacySettingsReaderTests(unittest.TestCase):
    def test_valid_legacy_values_map_to_every_typed_field(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.ini"
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
            _write_ini(path, values)

            result = LegacySettingsReader(path).load()

            self.assertEqual(result.source, "legacy_ini")
            self.assertEqual(result.invalid_keys, ())
            self.assertEqual(set(result.present_keys), set(values))
            self.assertEqual(result.settings.appearance.theme, "dark")
            self.assertTrue(result.settings.onboarding.agreement_accepted)
            self.assertEqual(result.settings.models.chat, "gemma3:4b")
            self.assertEqual(result.settings.generation.temperature, 1.25)
            self.assertEqual(result.settings.generation.num_ctx, 8192)
            self.assertEqual(result.settings.generation.seed, 42)
            self.assertFalse(result.settings.memory.enabled)
            self.assertTrue(result.settings.translation.enabled)
            self.assertEqual(result.settings.translation.target_language, "French")
            self.assertFalse(result.settings.suggestions.enabled)
            self.assertEqual(result.settings.suggestions.model, "qwen3:4b")

    def test_invalid_fields_fall_back_individually_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.ini"
            _write_ini(
                path,
                {
                    "theme": "ultraviolet",
                    "chat_model": "gemma3:4b",
                    "temperature": "hot",
                    "num_ctx": "1024",
                    "seed": "2147483648",
                    "memories_enabled": "maybe",
                    "target_language": "German",
                    "unknown_key": "leave-me-alone",
                },
            )
            before = path.read_bytes()

            result = LegacySettingsReader(path).load()

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                set(result.invalid_keys),
                {"theme", "temperature", "num_ctx", "seed", "memories_enabled"},
            )
            self.assertEqual(result.settings.models.chat, "gemma3:4b")
            self.assertEqual(result.settings.translation.target_language, "German")

    def test_missing_suggestion_model_follows_loaded_chat_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.ini"
            _write_ini(path, {"chat_model": "gemma3:12b"})

            result = LegacySettingsReader(path).load()

            self.assertEqual(result.settings.models.chat, "gemma3:12b")
            self.assertEqual(result.settings.suggestions.model, "gemma3:12b")

    def test_legacy_reader_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.ini"
            _write_ini(path, {"theme": "dark"})

            with self.assertRaises(SettingsRepositoryError):
                LegacySettingsReader(path).save(CortexSettings())


if __name__ == "__main__":
    unittest.main()
