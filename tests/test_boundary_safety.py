"""Headless tests for model-command validation and rendered-content safety."""

import sys
import unittest
import logging
from pathlib import Path


PROJECT_DIR = Path(__file__).parents[1] / "Chat_LLM" / "Chat_LLM"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from generation_types import MemoryCommand, TranslationResult  # noqa: E402
from memory_commands import apply_memory_command  # noqa: E402
from memory import PermanentMemoryManager  # noqa: E402
from safe_rendering import is_safe_external_url, markdown_to_safe_html  # noqa: E402
from synthesis_agent import SynthesisAgent  # noqa: E402


class _FakeMemoryManager:
    def __init__(self):
        self.added = []
        self.clear_calls = 0

    def add_memo(self, memo):
        self.added.append(memo)

    def clear_memos(self):
        self.clear_calls += 1


class _FailingClient:
    def chat(self, **kwargs):
        raise RuntimeError("provider failure")


class _ResponseClient:
    def chat(self, **kwargs):
        return {"message": {"content": "visible answer", "thinking": "private thoughts"}}


class BoundarySafetyTests(unittest.TestCase):
    def test_memory_command_accepts_valid_json_and_deduplicates(self):
        agent = SynthesisAgent("chat", "title", "translate", _FailingClient())

        answer, thoughts, command = agent._parse_and_clean_response(
            'Hello <memory_command>{"add":["User likes tea", "user likes tea"], "clear": false}</memory_command>',
            None,
        )

        self.assertEqual(answer, "Hello")
        self.assertIsNone(thoughts)
        self.assertEqual(command, MemoryCommand(("User likes tea",), False))

    def test_malformed_oversized_and_legacy_commands_are_inert(self):
        agent = SynthesisAgent("chat", "title", "translate", _FailingClient())

        _, _, malformed = agent._parse_and_clean_response(
            '<memory_command>{"add":"not a list", "clear": true}</memory_command>',
            None,
        )
        _, _, oversized = agent._parse_and_clean_response(
            '<memory_command>{"add":["' + ("x" * 501) + '"]}</memory_command>',
            None,
        )
        answer, _, legacy = agent._parse_and_clean_response(
            "Answer <memo>secret</memo><clear_memory />", None
        )

        self.assertFalse(malformed.has_actions)
        self.assertFalse(oversized.has_actions)
        self.assertFalse(legacy.has_actions)
        self.assertEqual(answer, "Answer")

    def test_clear_memory_always_uses_confirmation_callback(self):
        manager = _FakeMemoryManager()
        command = MemoryCommand(("keep this fact",), True)

        skipped = apply_memory_command(manager, command, confirm_clear=lambda: False)
        self.assertTrue(skipped.clear_skipped)
        self.assertEqual(manager.clear_calls, 0)
        self.assertEqual(manager.added, ["keep this fact"])

        confirmed = apply_memory_command(manager, MemoryCommand((), True), confirm_clear=lambda: True)
        self.assertTrue(confirmed.cleared)
        self.assertEqual(manager.clear_calls, 1)

    def test_memory_storage_validation_caps_and_deduplicates_entries(self):
        normalized = PermanentMemoryManager.normalize_memos([" Fact ", "fact", "another fact"])
        self.assertEqual(normalized, ["Fact", "another fact"])
        with self.assertRaises(ValueError):
            PermanentMemoryManager.normalize_memos(["x" * 501])
        with self.assertRaises(ValueError):
            PermanentMemoryManager.normalize_memos([str(index) for index in range(101)])

    def test_rendering_removes_active_html_and_rejects_unsafe_links(self):
        rendered = markdown_to_safe_html(
            '<script>alert(1)</script> [safe](https://example.com) '
            '[bad](javascript:alert(1)) <img src="file:///secret">'
        )

        self.assertNotIn("<script", rendered.lower())
        self.assertNotIn("javascript:", rendered.lower())
        self.assertNotIn("<img", rendered.lower())
        self.assertIn('href="https://example.com"', rendered)
        self.assertTrue(is_safe_external_url("https://example.com/path"))
        self.assertFalse(is_safe_external_url("javascript:alert(1)"))
        self.assertFalse(is_safe_external_url("file:///secret"))
        self.assertFalse(is_safe_external_url("data:text/html,hello"))

    def test_translation_failure_is_explicit(self):
        agent = SynthesisAgent("chat", "title", "translate", _FailingClient())

        result = agent.translate_text("private response", "Spanish")

        self.assertIsInstance(result, TranslationResult)
        self.assertFalse(result.success)
        self.assertIsNone(result.text)
        self.assertEqual(result.error, "Translation failed. Please try again.")

    def test_generation_logging_does_not_emit_raw_response(self):
        agent = SynthesisAgent("chat", "title", "translate", _ResponseClient())
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger()
        logger.addHandler(handler)
        try:
            agent.generate("private user prompt", "private history", [], False, None)
        finally:
            logger.removeHandler(handler)

        rendered_logs = "\n".join(record.getMessage() for record in records)
        self.assertNotIn("private user prompt", rendered_logs)
        self.assertNotIn("private history", rendered_logs)
        self.assertNotIn("private thoughts", rendered_logs)


if __name__ == "__main__":
    unittest.main()
