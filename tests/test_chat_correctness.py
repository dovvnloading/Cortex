"""Regression tests for persisted chat state and context sizing."""

from pathlib import Path
import tempfile
import unittest

from cortex_backend.api.schemas import AddMessageRequest, ChatMessage
from cortex_backend.repositories.chats import InMemoryChatRepository, LegacyDatabaseChatRepository
from cortex_backend.repositories.legacy_storage import DatabaseManager
from cortex_backend.core.generation import GenerationAttachment
from cortex_backend.core.settings import CortexSettings
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

    def test_default_context_window_survives_a_realistic_long_conversation(self):
        """Regression guard for a bug where the shipped num_ctx default was
        small enough that ordinary conversations lost most of their history
        to the context-budget trim -- not because any model "forgot", but
        because the built-in system/memory/code-execution prompts (up to
        ~2000 tokens) ate most of an already-small budget before a single
        word of the conversation was counted. At the old 4096 default, a
        30-exchange conversation like this one kept as few as 4 of 30
        exchanges. Reads the default from CortexSettings rather than
        hardcoding it, so this stays meaningful if the default changes again.
        """
        turn = "Can you walk me through why the connection pool keeps timing out under load?"
        reply = (
            "The timeout usually means every connection is checked out and none are "
            "returned before the next request needs one. Check whether connections "
            "are closed in a finally block even on exceptions, and whether the pool "
            "size actually matches your real concurrency."
        )
        messages = []
        for index in range(30):
            messages.append({"role": "user", "content": f"{turn} (turn {index})"})
            messages.append({"role": "assistant", "content": f"{reply} (turn {index})"})

        default_num_ctx = CortexSettings().generation.num_ctx
        history = SynthesisAgent.fit_history_to_context(
            messages,
            query="Given all that, what should I change first?",
            permanent_memories=[
                "Prefers Python for backend work.",
                "Works on a small internal tools team of four engineers.",
                "Wants direct answers with caveats stated plainly.",
                "Currently debugging a connection-pool timeout issue in production.",
                "Uses PostgreSQL with SQLAlchemy's pooled engine.",
            ],
            memories_enabled=True,
            user_system_instructions="Always include a code example when relevant, and be concise.",
            num_ctx=default_num_ctx,
            code_execution_eligible=True,
        )

        kept_exchanges = history.count("User: ")
        self.assertGreaterEqual(
            kept_exchanges,
            25,
            f"Only {kept_exchanges}/30 exchanges survived at the shipped default "
            f"num_ctx={default_num_ctx} with memory and code-execution eligibility "
            "both on -- the default is too small relative to the built-in prompt "
            "overhead and conversations will appear to lose their memory.",
        )

    def test_oversized_newest_exchange_does_not_wipe_the_rest_of_history(self):
        """Regression guard: fit_history_to_context used to stop walking the
        moment the single newest exchange alone exceeded the budget, discarding
        every older exchange too and returning "No history available." even
        though ten small exchanges right before it would easily have fit. The
        newest exchange being oversized should just be dropped on its own.
        """
        messages = []
        for index in range(10):
            messages.append({"role": "user", "content": f"Question number {index} about the project"})
            messages.append({"role": "assistant", "content": f"Short answer number {index}."})
        messages.append({"role": "user", "content": "Please write the full module"})
        messages.append({"role": "assistant", "content": "X" * 35_000})

        history = SynthesisAgent.fit_history_to_context(
            messages,
            query="now explain what you just did",
            permanent_memories=[],
            memories_enabled=True,
            user_system_instructions=None,
            num_ctx=8192,
        )

        self.assertNotEqual(history, "No history available.")
        self.assertEqual(history.count("User: "), 10)
        self.assertIn("Question number 9", history)
        self.assertNotIn("X" * 100, history)

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

    def test_followup_calls_reuse_the_turns_context_size(self):
        """Regression test for an out-of-memory crash *after* a good answer.

        The title deliberately reuses the chat model. num_ctx is a per-request
        option for Ollama and a launch flag for llama-server, so a title call
        that omits it does not quietly fall back to a default -- it asks the
        runtime for a differently-sized copy of a model already in memory and
        forces a full unload/reload, moments after generation left memory at
        its peak. On a machine near its limit that reload is the crash.
        """
        client = _CapturingClient({"content": "Some answer"})
        agent = SynthesisAgent("chat", "chat", "translate", client)

        agent.generate_chat_title("User: hi\nAssistant: hello", options={"num_ctx": 16384})
        self.assertEqual(client.last_options.get("num_ctx"), 16384)
        # Determinism is the call's own concern, never inherited from the chat.
        self.assertEqual(client.last_options.get("temperature"), 0.2)

        agent.translate_text("hello", "Spanish", options={"num_ctx": 16384})
        self.assertEqual(client.last_options.get("num_ctx"), 16384)
        self.assertEqual(client.last_options.get("temperature"), 0.1)

        # Only sizing is carried over; sampling from the chat turn must not
        # leak into a call that needs to be deterministic.
        agent.generate_chat_title(
            "User: hi\nAssistant: hello",
            options={"num_ctx": 8192, "temperature": 1.4, "top_p": 0.2, "seed": 7},
        )
        self.assertEqual(client.last_options, {"num_ctx": 8192, "temperature": 0.2})

        # And omitting options entirely must not invent a num_ctx.
        agent.generate_chat_title("User: hi\nAssistant: hello")
        self.assertNotIn("num_ctx", client.last_options)

    def test_vector_memory_is_not_initialized_until_integrated(self):
        source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("VectorDatabaseManager()", source)
        self.assertNotIn("embedding_model", source)


if __name__ == "__main__":
    unittest.main()
