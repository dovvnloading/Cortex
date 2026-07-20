"""Regression tests for persisted chat state, forking, titles, and context sizing."""

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).parents[1] / "Chat_LLM" / "Chat_LLM"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from Chat_LLM import Orchestrator  # noqa: E402
from memory import DatabaseManager, MemoryManager  # noqa: E402
from synthesis_agent import SynthesisAgent  # noqa: E402


class _RecordingDatabase:
    def __init__(self):
        self.messages = []
        self.chat_exists = False

    def load_chat(self, thread_id):
        return {"id": thread_id} if self.chat_exists else None

    def add_message(self, **kwargs):
        self.chat_exists = True
        self.messages.append(kwargs)

    def update_chat_title(self, thread_id, title):
        self.title = title


class ChatCorrectnessTests(unittest.TestCase):
    def test_first_message_persists_even_when_title_is_new_chat(self):
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator.database_manager = _RecordingDatabase()
        orchestrator.memory_manager = MemoryManager()
        orchestrator.active_thread_id = "thread-1"
        orchestrator.active_thread_title = "New Chat"
        orchestrator.active_thread_persisted = False

        orchestrator.commit_user_message("thread-1", "first message")

        self.assertTrue(orchestrator.active_thread_persisted)
        self.assertEqual(orchestrator.database_manager.messages[0]["thread_title"], "New Chat")
        self.assertEqual(orchestrator.memory_manager.get_full_history()[0]["content"], "first message")

    def test_generated_new_chat_title_does_not_reset_persisted_state(self):
        self.assertEqual(SynthesisAgent.normalize_title('  "New Chat"  '), "New Chat")
        self.assertEqual(SynthesisAgent.normalize_title(""), "Untitled Chat")
        self.assertLessEqual(len(SynthesisAgent.normalize_title("x" * 200)), 80)

    def test_fork_uses_persisted_message_index_not_visible_widget_count(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(db_path=str(Path(directory) / "chats.sqlite"))
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
            orchestrator = Orchestrator.__new__(Orchestrator)
            orchestrator.database_manager = database

            new_thread_id = orchestrator.fork_chat_thread(source_id, 2)
            forked = database.load_chat(new_thread_id)

            self.assertEqual([message["content"] for message in forked["messages"]], ["one", "two", "three"])

    def test_regeneration_after_loading_removes_only_last_assistant(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(db_path=str(Path(directory) / "chats.sqlite"))
            thread_id = "thread-1"
            database.create_chat_from_messages(
                thread_id,
                "Topic",
                [
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "answer"},
                ],
            )
            orchestrator = Orchestrator.__new__(Orchestrator)
            orchestrator.database_manager = database
            orchestrator.active_thread_id = thread_id
            orchestrator.active_thread_persisted = True
            orchestrator.memory_manager = MemoryManager()
            orchestrator.load_chat_thread(thread_id)

            orchestrator.delete_last_assistant_message(thread_id)

            remaining = database.load_chat(thread_id)["messages"]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["role"], "user")

    def test_context_budget_keeps_recent_history_and_reserves_output(self):
        messages = []
        for index in range(8):
            messages.extend([
                {"role": "user", "content": f"old-{index} " + ("details " * 80)},
                {"role": "assistant", "content": f"reply-{index} " + ("context " * 80)},
            ])

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
        source = (PROJECT_DIR / "Chat_LLM.py").read_text(encoding="utf-8")
        self.assertNotIn("VectorDatabaseManager()", source)
        self.assertNotIn("embedding_model", source)


if __name__ == "__main__":
    unittest.main()
