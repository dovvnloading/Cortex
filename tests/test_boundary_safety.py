"""Headless tests for model-command validation and rendered-content safety."""

import unittest

from cortex_backend.core.generation import MemoryCommand, TranslationResult
from cortex_backend.repositories.legacy_storage import PermanentMemoryManager
from cortex_backend.services.llm import SynthesisAgent
from cortex_backend.services.memory_commands import apply_memory_command


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


class _TitleClient:
    def chat(self, **kwargs):
        return {"message": {"content": '  "Cortex launch planning"\n'}}


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

    def test_translation_failure_is_explicit(self):
        agent = SynthesisAgent("chat", "title", "translate", _FailingClient())

        result = agent.translate_text("private response", "Spanish")

        self.assertIsInstance(result, TranslationResult)
        self.assertFalse(result.success)
        self.assertIsNone(result.text)
        self.assertEqual(result.error, "Translation failed. Please try again.")

    def test_generation_logging_does_not_emit_raw_response(self):
        import logging

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

    def test_chat_title_generation_uses_the_title_model_and_normalizes_output(self):
        agent = SynthesisAgent("chat", "title", "translate", _TitleClient())

        title = agent.generate_chat_title("User: Plan the Cortex launch")

        self.assertEqual(title, "Cortex launch planning")

    def test_chat_title_generation_failure_is_non_fatal(self):
        agent = SynthesisAgent("chat", "title", "translate", _FailingClient())

        self.assertIsNone(agent.generate_chat_title("User: Plan the Cortex launch"))


if __name__ == "__main__":
    unittest.main()
