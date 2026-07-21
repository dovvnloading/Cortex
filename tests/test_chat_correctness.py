"""Regression tests for persisted chat state and context sizing."""

from pathlib import Path
import tempfile
import unittest

from cortex_backend.repositories.chats import LegacyDatabaseChatRepository
from cortex_backend.repositories.legacy_storage import DatabaseManager
from cortex_backend.services.llm import SynthesisAgent


class ChatCorrectnessTests(unittest.TestCase):
    def test_generated_new_chat_title_is_normalized(self):
        self.assertEqual(SynthesisAgent.normalize_title('  "New Chat"  '), "New Chat")
        self.assertEqual(SynthesisAgent.normalize_title("**AI Purpose Explained**"), "AI Purpose Explained")
        self.assertEqual(SynthesisAgent.normalize_title("### [Cortex planning](https://example.test)"), "Cortex planning")
        self.assertEqual(SynthesisAgent.normalize_title(""), "Untitled Chat")
        self.assertLessEqual(len(SynthesisAgent.normalize_title("x" * 200)), 80)

    def test_fork_uses_persisted_message_id_not_visible_widget_count(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(db_path=str(Path(directory) / "chats.sqlite"))
            repository = LegacyDatabaseChatRepository(database)
            source_id = "source"
            database.create_chat_from_messages(
                source_id,
                "Topic",
                [
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                    {"role": "user", "content": "three"},
                    {"role": "assistant", "content": "four"},
                ],
            )
            message_id = database.load_chat(source_id)["messages"][2]["id"]

            repository.fork_chat(source_id, str(message_id), "forked")

            forked = database.load_chat("forked")
            self.assertEqual(
                [message["content"] for message in forked["messages"]],
                ["one", "two", "three"],
            )

    def test_regeneration_after_loading_removes_only_last_assistant(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(db_path=str(Path(directory) / "chats.sqlite"))
            database.create_chat_from_messages(
                "thread-1",
                "Topic",
                [
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "answer"},
                ],
            )

            database.delete_last_assistant_message("thread-1")

            remaining = database.load_chat("thread-1")["messages"]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["role"], "user")

    def test_context_budget_keeps_recent_history_and_reserves_output(self):
        messages = []
        for index in range(8):
            messages.extend(
                [
                    {"role": "user", "content": f"old-{index} " + ("details " * 80)},
                    {"role": "assistant", "content": f"reply-{index} " + ("context " * 80)},
                ]
            )

        history = SynthesisAgent.fit_history_to_context(
            messages,
            query="latest question",
            permanent_memories=["User likes concise answers."],
            memories_enabled=True,
            user_system_instructions="Be helpful.",
            num_ctx=4096,
        )

        self.assertIn("old-7", history)
        self.assertNotIn("old-0", history)
        self.assertEqual(SynthesisAgent.output_token_reservation(4096), 1024)

    def test_context_budget_trims_oversized_permanent_memory(self):
        memories = [f"memory-{index} " + ("detail " * 120) for index in range(20)]

        fitted = SynthesisAgent.fit_memories_to_context(
            memories,
            query="latest question",
            user_system_instructions=None,
            num_ctx=4096,
        )

        self.assertLess(len(fitted), len(memories))
        self.assertEqual(fitted[-1].split()[0], "memory-19")

    def test_vector_memory_is_not_initialized_until_integrated(self):
        source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("VectorDatabaseManager()", source)
        self.assertNotIn("embedding_model", source)


if __name__ == "__main__":
    unittest.main()
