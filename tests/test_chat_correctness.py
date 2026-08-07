"""Regression tests for persisted chat state and context sizing."""

from pathlib import Path
import tempfile
import unittest

from cortex_backend.api.schemas import AddMessageRequest, ChatMessage
from cortex_backend.repositories.chats import InMemoryChatRepository, LegacyDatabaseChatRepository
from cortex_backend.repositories.legacy_storage import DatabaseManager
from cortex_backend.core.generation import GenerationAttachment
from cortex_backend.services.llm import SynthesisAgent


class _CapturingClient:
    """Records the options a generate() call actually sends to the model."""

    def __init__(self, message: dict):
        self.message = message
        self.last_options: dict | None = None

    def chat(self, *, model, messages, options):
        self.last_options = options
        return {"message": self.message}


class ChatCorrectnessTests(unittest.TestCase):
    def test_reasoning_metadata_is_scoped_to_assistant_messages(self):
        user_response = ChatMessage(role="user", content="Question", thoughts="must not leak")
        user_request = AddMessageRequest(role="user", content="Question", thoughts="must not persist")
        self.assertIsNone(user_response.thoughts)
        self.assertIsNone(user_request.thoughts)

        repository = InMemoryChatRepository(
            [{
                "id": "thread-1",
                "title": "Topic",
                "timestamp": "2026-01-01T00:00:00Z",
                "messages": [{"role": "user", "content": "Question", "thoughts": "legacy leak"}],
            }]
        )
        loaded = repository.get_chat("thread-1")
        self.assertIsNotNone(loaded)
        self.assertIsNone(loaded["messages"][0]["thoughts"])
        repository.add_message("thread-1", "user", "Follow-up", thoughts="another leak")
        self.assertIsNone(repository.get_chat("thread-1")["messages"][-1]["thoughts"])

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

    def test_context_budget_trims_document_reference_text_but_keeps_attachment_identity(self):
        attachment = GenerationAttachment(
            attachment_id="doc-1",
            filename="large.md",
            mime_type="text/markdown",
            kind="document",
            text_content="important " * 20_000,
        )

        fitted = SynthesisAgent.fit_attachments_to_context(
            (attachment,),
            query="Summarize the attachment.",
            chat_history="No history available.",
            permanent_memories=[],
            memories_enabled=False,
            user_system_instructions=None,
            num_ctx=1024,
        )

        self.assertEqual(fitted[0].attachment_id, "doc-1")
        self.assertEqual(fitted[0].filename, "large.md")
        self.assertLess(len(fitted[0].text_content or ""), len(attachment.text_content or ""))
        self.assertIn("truncated to fit the model context", fitted[0].text_content or "")

    def test_generate_does_not_cap_output_length_below_the_configured_context(self):
        # A reasoning-capable model spends tokens on an invisible "thinking"
        # block before writing any visible answer. Regression guard for a
        # bug where generate() silently forced num_predict down to the small
        # (max 1024) budget meant for trimming attachments/history, so the
        # thinking block alone would exhaust it and the model was cut off
        # before ever producing an answer -- persisting a chat with empty
        # content next to a full reasoning trace.
        client = _CapturingClient({"content": "the answer", "thinking": "reasoning..."})
        agent = SynthesisAgent("chat", "title", "translate", client)

        agent.generate("question", "No history available.", [], False, None, options={"num_ctx": 8192})

        self.assertIsNotNone(client.last_options)
        self.assertNotIn("num_predict", client.last_options)

    def test_generate_surfaces_an_empty_answer_next_to_its_reasoning_rather_than_dropping_it(self):
        # If a model still returns nothing usable despite the fix above, the
        # empty answer must reach the caller intact (paired with whatever
        # reasoning came back) instead of being silently swapped for
        # something else -- the frontend is responsible for explaining an
        # empty-content/non-empty-thoughts message to the user.
        client = _CapturingClient({"content": "", "thinking": "still reducing the problem..."})
        agent = SynthesisAgent("chat", "title", "translate", client)

        answer, thoughts, _, _ = agent.generate("question", "No history available.", [], False, None)

        self.assertEqual(answer, "")
        self.assertEqual(thoughts, "still reducing the problem...")

    def test_vector_memory_is_not_initialized_until_integrated(self):
        source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("VectorDatabaseManager()", source)
        self.assertNotIn("embedding_model", source)


if __name__ == "__main__":
    unittest.main()
