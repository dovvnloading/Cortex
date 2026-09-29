"""Regression tests for persisted chat state and context sizing."""

from pathlib import Path
import tempfile
import unittest

from cortex_backend.api.schemas import AddMessageRequest, ChatMessage
from cortex_backend.repositories.chats import InMemoryChatRepository, LegacyDatabaseChatRepository
from cortex_backend.repositories.storage import DatabaseManager
from cortex_backend.core.generation import GenerationAttachment
from cortex_backend.core.settings import CortexSettings
from cortex_backend.services import token_budget
from cortex_backend.services.attachments import MAX_DOCUMENT_TEXT_CHARS
from cortex_backend.services.history_window import HISTORY_OMISSION_NOTE
from cortex_backend.services.llm import PromptTemplate, SynthesisAgent


class _CapturingClient:
    """Records the options a generate() call actually sends to the model."""

    def __init__(self, message: dict):
        self.message = message
        self.last_options: dict | None = None
        self.last_think: bool | None = None

    def chat(self, *, model, messages, options, think=None):
        self.last_options = options
        self.last_think = think
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

        history = SynthesisAgent.fit_history(
            messages,
            query="latest question",
            permanent_memories=["User likes concise answers."],
            memories_enabled=True,
            user_system_instructions="Be helpful.",
            num_ctx=4096,
        )[0]

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
        history = SynthesisAgent.fit_history(
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
        )[0]

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
        though ten small exchanges right before it would easily have fit.

        Retention is now contiguous, so the oversized exchange cannot simply be
        skipped either (that leaves a hole). Its *answer* is cut down to a
        bounded share of the room instead, and everything older stays.
        """
        messages = []
        for index in range(10):
            messages.append({"role": "user", "content": f"Question number {index} about the project"})
            messages.append({"role": "assistant", "content": f"Short answer number {index}."})
        messages.append({"role": "user", "content": "Please write the full module"})
        messages.append({"role": "assistant", "content": "X" * 35_000})

        history = SynthesisAgent.fit_history(
            messages,
            query="now explain what you just did",
            permanent_memories=[],
            memories_enabled=True,
            user_system_instructions=None,
            num_ctx=8192,
        )[0]

        self.assertNotEqual(history, "No history available.")
        # All ten older exchanges and the newest one, which is kept.
        self.assertEqual(history.count("User: "), 11)
        self.assertIn("Question number 0", history)
        self.assertIn("Question number 9", history)
        self.assertIn("Please write the full module", history)
        # Its answer was shortened, visibly, and by a lot.
        self.assertIn("characters omitted", history)
        self.assertLess(history.count("X"), 35_000 // 2)
        # Nothing whole was dropped, so there is no omission note.
        self.assertNotIn(HISTORY_OMISSION_NOTE, history)

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

    def test_only_the_side_calls_switch_reasoning_off(self):
        """Title and translation want a few words back, not a reasoning pass.
        The user's own answer must keep the model's default, or a thinking
        model would lose the reasoning the user asked it to show."""
        client = _CapturingClient({"content": "Some answer"})
        agent = SynthesisAgent("chat", "chat", "translate", client)

        agent.generate("question", "No history available.", [], False, None)
        self.assertIsNone(client.last_think)

        agent.generate_chat_title("User: hi\nAssistant: hello")
        self.assertIs(client.last_think, False)

        client.last_think = None
        agent.translate_text("hello", "Spanish")
        self.assertIs(client.last_think, False)


class _MeasuringClient:
    """Answers like a runtime that counts the prompt: ``chars_per_token`` is the truth."""

    def __init__(self, chars_per_token: float, *, count: int | None = None):
        self.chars_per_token = chars_per_token
        self.count = count
        self.messages: list[dict] | None = None

    def chat(self, *, model, messages, options, think=None, **kwargs):
        self.messages = messages
        if self.count is not None:
            tokens = self.count
        else:
            characters = sum(len(str(message.get("content", ""))) for message in messages)
            tokens = int(characters / self.chars_per_token) + 4 * len(messages)
        return {"message": {"content": "ok"}, "prompt_eval_count": tokens, "eval_count": 5}


def _exchanges(count: int, *, size: int = 200, oversized: dict[int, int] | None = None) -> list[dict]:
    """``count`` synthetic exchanges; ``oversized`` maps an index to a bigger answer."""
    messages: list[dict] = []
    for index in range(count):
        length = (oversized or {}).get(index, size)
        messages.append({"role": "user", "content": f"question-{index}"})
        messages.append({"role": "assistant", "content": f"answer-{index} " + "d" * length})
    return messages


_NO_MEMORY = {
    "permanent_memories": [],
    "memories_enabled": False,
    "user_system_instructions": None,
    "code_execution_eligible": False,
}


class TokenEstimateTests(unittest.TestCase):
    """The estimate that every budget decision starts from."""

    def test_token_estimates_are_calibrated_from_the_previous_turns_prompt_count(self):
        client = _MeasuringClient(chars_per_token=2.6)
        agent = SynthesisAgent("dense-model", "title", "translate", client)
        before = SynthesisAgent.estimate_tokens("x" * 1000, "dense-model")
        # Uncalibrated: the conservative default, not the old fixed four.
        self.assertEqual(before, 286)

        agent.generate(
            "a question", "No history available.", [], False, None, options={"num_ctx": 8192}
        )

        after = SynthesisAgent.estimate_tokens("x" * 1000, "dense-model")
        self.assertAlmostEqual(after, 1000 / 2.6, delta=10)
        self.assertGreater(after, before)
        # What one model taught the registry says nothing about another.
        self.assertEqual(SynthesisAgent.estimate_tokens("x" * 1000, "other-model"), before)
        self.assertEqual(SynthesisAgent.estimate_tokens("x" * 1000), before)

    def test_a_calibrated_model_fits_less_history_than_an_unknown_one(self):
        messages = _exchanges(40, size=400)
        budget = {"query": "next", "num_ctx": 8192, **_NO_MEMORY}
        unknown = SynthesisAgent.fit_history(list(messages), **budget, model="dense-model")[1]

        token_budget.TOKEN_RATIOS.observe("dense-model", ["x" * 4000], 4000 // 2 + 4)
        calibrated = SynthesisAgent.fit_history(list(messages), **budget, model="dense-model")[1]

        self.assertLess(len(calibrated), len(unknown))

    def test_cjk_text_is_never_estimated_below_one_token_per_character(self):
        samples = {
            "japanese": "日本語のテキストです。" * 50,
            "korean": "한국어문장입니다" * 50,
            "chinese": "汉字" * 200,
            "mixed": "abc" * 100 + "日本" * 100,
        }
        token_budget.TOKEN_RATIOS.observe("calibrated", ["x" * 4000], 1004)  # 4.0 chars per token
        for name, sample in samples.items():
            wide = sum(1 for character in sample if ord(character) >= token_budget.WIDE_CHAR_START)
            for model in (None, "calibrated"):
                with self.subTest(sample=name, model=model):
                    self.assertGreaterEqual(SynthesisAgent.estimate_tokens(sample, model), wide)
        # Ordinary text is not inflated by the floor.
        self.assertEqual(SynthesisAgent.estimate_tokens("a" * 350), 100)

    def test_a_learned_ratio_moves_down_at_once_and_up_slowly(self):
        ratios = token_budget.TokenRatioRegistry()
        text = "y" * 4000
        ratios.observe("m", [text], 4000 // 4 + 4)
        self.assertAlmostEqual(ratios.chars_per_token("m"), 4.0)

        # The estimate was too low: believe the runtime straight away.
        ratios.observe("m", [text], 4000 // 2 + 4)
        self.assertAlmostEqual(ratios.chars_per_token("m"), 2.6)

        # One prose-heavy turn is no reason to trust the next code-heavy one.
        ratios.observe("m", [text], 4000 // 4 + 4)
        self.assertAlmostEqual(ratios.chars_per_token("m"), 3.02)

    def test_a_learned_ratio_is_never_bolder_than_the_old_fixed_four(self):
        ratios = token_budget.TokenRatioRegistry()
        ratios.observe("m", ["y" * 4000], 4000 // 6 + 4)  # a reading of six characters per token
        self.assertEqual(ratios.chars_per_token("m"), token_budget.MAX_CHARS_PER_TOKEN)
        self.assertEqual(token_budget.MAX_CHARS_PER_TOKEN, 4.0)

    def test_a_reading_that_cannot_be_the_whole_prompt_is_not_learned_from(self):
        ratios = token_budget.TokenRatioRegistry()
        text = "z" * 4000
        # None, a bool, and counts that are impossible for this much text:
        # too low is a runtime reporting only its uncached tail, too high is
        # nonsense. Neither is clamped and believed.
        for tokens in (None, True, 0, -5, 1, 10, 40, 4000 * 3):
            with self.subTest(tokens=tokens):
                self.assertIsNone(ratios.observe("m", [text], tokens))
        self.assertIsNone(ratios.observe("m", ["short"], 3))
        self.assertIsNone(ratios.observe("m", ["日本語" * 300], 900))
        self.assertIsNone(ratios.observe("", [text], 1004))
        self.assertIsNone(ratios.observe(None, [text], 1004))
        self.assertFalse(ratios.is_calibrated("m"))
        self.assertEqual(ratios.chars_per_token("m"), token_budget.DEFAULT_CHARS_PER_TOKEN)

    def test_the_registry_forgets_the_least_recent_models_instead_of_growing(self):
        ratios = token_budget.TokenRatioRegistry()
        for index in range(40):
            ratios.observe(f"model-{index}", ["a" * 4000], 1004)
        self.assertFalse(ratios.is_calibrated("model-0"))
        self.assertTrue(ratios.is_calibrated("model-39"))

    def test_a_prompt_that_filled_the_window_or_carried_images_is_not_learned_from(self):
        # A count at the window's edge is a runtime that may have cut the
        # prompt: chars-per-token from it would be too high and unsafe.
        full = SynthesisAgent("full-model", "title", "translate", _MeasuringClient(1.0, count=2040))
        full.generate("q", "No history available.", [], False, None, options={"num_ctx": 2048})
        self.assertFalse(token_budget.TOKEN_RATIOS.is_calibrated("full-model"))

        # An image adds tokens that are not text.
        image = GenerationAttachment(
            attachment_id="img", filename="cat.png", mime_type="image/png", kind="image", image_base64="AAAA"
        )
        seeing = SynthesisAgent("vision-model", "title", "translate", _MeasuringClient(1.0, count=900))
        seeing.generate("q", "No history available.", [], False, None, options={"num_ctx": 8192}, attachments=(image,))
        self.assertFalse(token_budget.TOKEN_RATIOS.is_calibrated("vision-model"))

        # The same reading without either is learned from.
        plain = SynthesisAgent("plain-model", "title", "translate", _MeasuringClient(2.5))
        plain.generate("q", "No history available.", [], False, None, options={"num_ctx": 8192})
        self.assertTrue(token_budget.TOKEN_RATIOS.is_calibrated("plain-model"))

    def test_the_whole_prompt_count_is_preferred_over_the_part_a_runtime_evaluated(self):
        class _CachedPrefixClient(_MeasuringClient):
            def chat(self, **kwargs):
                response = super().chat(**kwargs)
                # llama-server: timings count only what it evaluated, usage the whole prompt.
                response["prompt_token_count"] = response["prompt_eval_count"]
                response["prompt_eval_count"] = 30
                return response

        agent = SynthesisAgent("cached-model", "title", "translate", _CachedPrefixClient(2.6))
        agent.generate("q", "No history available.", [], False, None, options={"num_ctx": 8192})

        self.assertAlmostEqual(token_budget.TOKEN_RATIOS.chars_per_token("cached-model"), 2.6, delta=0.1)

    def test_history_selection_leaves_the_safety_margin_free(self):
        budget = {"query": "next", "num_ctx": 4096, **_NO_MEMORY}
        transcript = SynthesisAgent.fit_history(_exchanges(40, size=300), **budget)[0]
        self.assertLess(transcript.count("User: "), 40, "the budget must actually bite")

        prompt = PromptTemplate.build_synthesis_prompt(
            "next", transcript, [], False, None, code_execution_eligible=False
        )
        limit = 4096 - SynthesisAgent.output_token_reservation(4096)
        raw = sum(token_budget.estimate_tokens(message["content"]) + 4 for message in prompt)
        # The estimate alone leaves the margin free; with it, the prompt fits.
        self.assertLessEqual(raw * token_budget.SAFETY_MARGIN, limit + 1)
        self.assertGreater(raw, limit * 0.8)
        self.assertLessEqual(SynthesisAgent.estimate_prompt_tokens(prompt), limit)


class _TokenizingClient:
    """A local-runtime client that can count the prompt before it is asked to chat."""

    def __init__(self, chars_per_token: float, *, fail: bool = False):
        self.chars_per_token = chars_per_token
        self.fail = fail
        self.counted: list[str] = []
        self.sent: list[dict] | None = None

    def tokenize(self, *, model, text, options, cancellation_event=None):
        self.counted.append(text)
        if self.fail:
            raise RuntimeError("synthetic tokenizer failure")
        return int(len(text) / self.chars_per_token)

    def chat(self, *, model, messages, options, think=None, **kwargs):
        self.sent = messages
        return {"message": {"content": "ok"}}


class _WideTokenizingClient(_TokenizingClient):
    """Counts like a byte-fallback vocabulary: several tokens for every CJK character.

    ``tokens_per_wide`` is what the tokenizer really charges per wide character,
    and the estimate's floor is only 1.2, so the truth is well above it.
    """

    def __init__(self, tokens_per_wide: float, *, chars_per_token: float = 4.0):
        super().__init__(chars_per_token)
        self.tokens_per_wide = tokens_per_wide

    def tokenize(self, *, model, text, options, cancellation_event=None):
        self.counted.append(text)
        wide = token_budget.count_wide_characters(text)
        return int(wide * self.tokens_per_wide + (len(text) - wide) / self.chars_per_token)

    def true_tokens(self, messages: list[dict]) -> int:
        return sum(self.tokenize(model="", text=message["content"], options={}) + 4 for message in messages)


class MeasuredPromptTests(unittest.TestCase):
    """A runtime that can count tokens is asked once, about the finished prompt."""

    MODEL = "gguf:test.gguf"

    def _history(self, exchanges: int, size: int) -> list[dict]:
        return SynthesisAgent._paired_history_messages(_exchanges(exchanges, size=size))

    def test_the_prompt_is_counted_once_and_the_count_calibrates_the_estimate(self):
        client = _TokenizingClient(2.0)
        agent = SynthesisAgent(self.MODEL, "title", "translate", client)

        agent.generate(
            "question", "unused", [], False, None, options={"num_ctx": 16384},
            history_messages=self._history(3, 100),
        )

        self.assertEqual(len(client.counted), 1)
        self.assertAlmostEqual(token_budget.TOKEN_RATIOS.chars_per_token(self.MODEL), 2.0, delta=0.15)

    def test_history_is_dropped_whole_when_the_measured_prompt_does_not_fit(self):
        client = _TokenizingClient(2.0)
        agent = SynthesisAgent(self.MODEL, "title", "translate", client)
        history = self._history(40, 800)
        notices: list[str] = []

        agent.generate(
            "question", "unused", [], False, None, options={"num_ctx": 16384},
            history_messages=history,
            on_delta=lambda kind, text: notices.append(text) if kind == "notice" else None,
        )

        sent = client.sent or []
        kept = [message for message in sent[1:-1]]
        self.assertLess(len(kept), len(history))
        self.assertGreater(len(kept), 0)
        # Whole exchanges, oldest first, and the note says so.
        self.assertEqual([message["role"] for message in kept], ["user", "assistant"] * (len(kept) // 2))
        self.assertTrue(kept[0]["content"].startswith(HISTORY_OMISSION_NOTE))
        self.assertTrue(kept[-1]["content"].endswith(history[-1]["content"]))
        # The user is told, once, and the prompt now fits by the measured ratio.
        self.assertEqual(len(notices), 1)
        characters = sum(len(message["content"]) for message in sent)
        limit = 16384 - SynthesisAgent.output_token_reservation(16384)
        self.assertLessEqual(characters / 2.0 + 4 * len(sent), limit)

    def test_wide_text_the_estimate_badly_undercounts_is_still_trimmed_by_the_measured_count(self):
        """The tokenizer's number is exact, so it must be used exactly when the estimate is far off.

        CJK and emoji on a byte-fallback vocabulary cost two or three tokens a
        character against an estimate of 1.2. The count used to be reduced to a
        ratio for the *ordinary* characters and discarded as implausible when
        that ratio fell outside a sane range -- so the prompts the estimate was
        worst for went out whole, with no trim and no notice, and the model's
        runtime then dropped their beginning silently.
        """
        limit = 16384 - SynthesisAgent.output_token_reservation(16384)
        for tokens_per_wide in (1.5, 2.0, 3.0):
            with self.subTest(tokens_per_wide=tokens_per_wide):
                token_budget.TOKEN_RATIOS.reset()
                client = _WideTokenizingClient(tokens_per_wide)
                agent = SynthesisAgent(self.MODEL, "title", "translate", client)
                # About 1.3 times the window in real tokens, whatever the vocabulary.
                per_exchange = int(1.3 * limit / tokens_per_wide / 14)
                history = SynthesisAgent._paired_history_messages(
                    [
                        message
                        for index in range(14)
                        for message in (
                            {"role": "user", "content": chr(0x554F) * 20 + str(index)},
                            {"role": "assistant", "content": chr(0x7B54) * per_exchange},
                        )
                    ]
                )
                notices: list[str] = []

                agent.generate(
                    "question", "unused", [], False, None, options={"num_ctx": 16384},
                    history_messages=history,
                    on_delta=lambda kind, text, sink=notices: sink.append(text) if kind == "notice" else None,
                )

                sent = client.sent or []
                kept = sent[1:-1]
                self.assertGreater(client.true_tokens(list(history) + [{"content": "x"}]), limit)
                self.assertLess(len(kept), len(history), "history must have been dropped")
                self.assertGreater(len(kept), 0, "and not all of it")
                # Whole exchanges from the old end, the note saying so, and the
                # prompt that is sent really fits the window by the tokenizer.
                self.assertTrue(kept[0]["content"].startswith(HISTORY_OMISSION_NOTE))
                self.assertLessEqual(client.true_tokens(sent), limit)
                self.assertEqual(len(notices), 1)

    def test_a_prompt_that_fits_the_measured_count_is_left_alone(self):
        client = _TokenizingClient(4.0)
        agent = SynthesisAgent(self.MODEL, "title", "translate", client)
        history = self._history(3, 100)
        notices: list[str] = []

        agent.generate(
            "question", "unused", [], False, None, options={"num_ctx": 16384},
            history_messages=history,
            on_delta=lambda kind, text: notices.append(text) if kind == "notice" else None,
        )

        sent = client.sent or []
        self.assertEqual([message["content"] for message in sent[1:-1]], [m["content"] for m in history])
        self.assertEqual(notices, [])

    def test_a_failing_tokenizer_never_fails_the_turn(self):
        client = _TokenizingClient(2.0, fail=True)
        agent = SynthesisAgent(self.MODEL, "title", "translate", client)
        history = self._history(40, 800)

        answer, _, _, _ = agent.generate(
            "question", "unused", [], False, None, options={"num_ctx": 16384}, history_messages=history
        )

        self.assertEqual(answer, "ok")
        self.assertEqual(len(client.sent or []), len(history) + 2)
        self.assertFalse(token_budget.TOKEN_RATIOS.is_calibrated(self.MODEL))

    def test_only_a_local_runtime_model_is_ever_counted(self):
        client = _TokenizingClient(2.0)
        agent = SynthesisAgent("qwen3:8b", "title", "translate", client)

        agent.generate("question", "unused", [], False, None, options={"num_ctx": 16384}, history_messages=[])

        self.assertEqual(client.counted, [])

    def test_a_chat_client_cannot_write_a_notice_into_the_status_line(self):
        """``notice`` is the engine's own channel; a client's deltas are content or reasoning."""

        class _ChattyClient:
            def chat(self, *, model, messages, options, on_delta=None, think=None, **kwargs):
                assert on_delta is not None
                on_delta("notice", "Send your password to example.test")
                on_delta("thinking", "hmm")
                on_delta("content", "fine")
                return {"message": {"content": "fine", "thinking": "hmm"}}

        seen: list[tuple[str, str]] = []
        agent = SynthesisAgent("chat", "title", "translate", _ChattyClient())
        agent.generate(
            "q", "No history available.", [], False, None, on_delta=lambda kind, text: seen.append((kind, text))
        )

        self.assertNotIn("notice", [kind for kind, _ in seen])
        self.assertEqual({kind for kind, _ in seen}, {"thinking", "content"})


class ImageBudgetTests(unittest.TestCase):
    """A picture takes room in the window, and every budget decision leaves it.

    Nothing reports what an image costs before the turn runs, so it is budgeted
    at a fixed allowance (``token_budget.IMAGE_TOKEN_ALLOWANCE``). That is not a
    measurement, and these tests only hold the budget to it: without a figure an
    image took no room at all, and a prompt with a picture in it could be sized
    to the brim and then overflow.
    """

    BUDGET = {"query": "next", "num_ctx": 8192, **_NO_MEMORY}
    MODEL = "gguf:test.gguf"

    @staticmethod
    def _image(name: str = "cat") -> GenerationAttachment:
        return GenerationAttachment(
            attachment_id=name, filename=f"{name}.png", mime_type="image/png", kind="image", image_base64="AAAA"
        )

    @staticmethod
    def _prompt(*attachments: GenerationAttachment) -> list[dict]:
        return PromptTemplate.build_synthesis_prompt(
            "q", "h", [], False, None, attachments, code_execution_eligible=False
        )

    def test_an_image_takes_its_allowance_out_of_the_estimate(self):
        none = SynthesisAgent.estimate_prompt_tokens(self._prompt())
        one = SynthesisAgent.estimate_prompt_tokens(self._prompt(self._image("a")))
        two = SynthesisAgent.estimate_prompt_tokens(self._prompt(self._image("a"), self._image("b")))

        allowance = token_budget.IMAGE_TOKEN_ALLOWANCE
        self.assertGreaterEqual(one - none, allowance)
        self.assertGreaterEqual(two - one, allowance)
        # A generous, fixed figure: several hundred tokens at least, and not a window's worth.
        self.assertTrue(512 <= allowance <= 2048)

    def test_history_leaves_room_for_an_attached_image(self):
        messages = _exchanges(60, size=400)
        without = SynthesisAgent.fit_history(list(messages), **self.BUDGET)[1]
        with_image = SynthesisAgent.fit_history(
            list(messages), **self.BUDGET, attachments=(self._image(),)
        )[1]

        self.assertLess(len(with_image), len(without))
        # And what remains, picture included, fits the limit the way any prompt does.
        transcript = SynthesisAgent.fit_history(
            list(messages), **self.BUDGET, attachments=(self._image(),)
        )[0]
        prompt = PromptTemplate.build_synthesis_prompt(
            "next", transcript, [], False, None, (self._image(),), code_execution_eligible=False
        )
        limit = 8192 - SynthesisAgent.output_token_reservation(8192)
        self.assertLessEqual(SynthesisAgent.estimate_prompt_tokens(prompt), limit)

    def test_a_document_gets_less_room_beside_an_image(self):
        document = GenerationAttachment(
            attachment_id="doc", filename="doc.md", mime_type="text/markdown", kind="document",
            text_content="important text. " * 10_000,
        )
        budget = {
            "query": "Summarize.",
            "chat_history": "No history available.",
            "permanent_memories": [],
            "memories_enabled": False,
            "user_system_instructions": None,
            "num_ctx": 8192,
        }

        alone = SynthesisAgent.fit_attachments_to_context((document,), **budget)
        beside = SynthesisAgent.fit_attachments_to_context((document, self._image()), **budget)

        self.assertLess(len(beside[0].text_content or ""), len(alone[0].text_content or ""))
        self.assertEqual(beside[1].kind, "image")

    def _generate(self, client: _TokenizingClient, *attachments: GenerationAttachment) -> list[str]:
        notices: list[str] = []
        agent = SynthesisAgent(self.MODEL, "title", "translate", client)
        history = SynthesisAgent._paired_history_messages(_exchanges(28, size=1000))
        agent.generate(
            "question", "unused", [], False, None, options={"num_ctx": 16384},
            history_messages=history, attachments=attachments,
            on_delta=lambda kind, text: notices.append(text) if kind == "notice" else None,
        )
        return notices

    def test_a_prompt_that_passes_on_its_text_alone_is_trimmed_for_the_image_it_carries(self):
        """The tokenizer counts text. The picture is added at the allowance, or the prompt overflows."""
        limit = 16384 - SynthesisAgent.output_token_reservation(16384)
        allowance = token_budget.IMAGE_TOKEN_ALLOWANCE

        plain_client = _TokenizingClient(2.0)
        self.assertEqual(self._generate(plain_client), [])
        sent = plain_client.sent or []
        text_tokens = sum(int(len(m["content"]) / 2.0) + 4 for m in sent)
        # The setup is only a test of the picture if the text alone just fits.
        self.assertLessEqual(text_tokens, limit)
        self.assertGreater(text_tokens + allowance, limit)

        token_budget.TOKEN_RATIOS.reset()
        seeing_client = _TokenizingClient(2.0)
        notices = self._generate(seeing_client, self._image())

        self.assertEqual(len(notices), 1)
        kept = (seeing_client.sent or [])[1:-1]
        self.assertLess(len(kept), len(sent) - 2)
        self.assertLessEqual(
            sum(int(len(m["content"]) / 2.0) + 4 for m in seeing_client.sent or []) + allowance, limit
        )


class HistoryRetentionTests(unittest.TestCase):
    """What is kept of a long conversation, and how the model and the user are told."""

    BUDGET = {"query": "next", "num_ctx": 8192, **_NO_MEMORY}

    @staticmethod
    def _kept(structured: list[dict]) -> list[int]:
        import re

        return [
            int(match.group(1))
            for message in structured
            if message["role"] == "user" and (match := re.search(r"question-(\d+)", message["content"]))
        ]

    def test_retained_history_is_contiguous_and_marks_omitted_turns(self):
        # One large exchange in the middle: the walk used to skip it and keep
        # the older, smaller ones, so the model saw 0-4 and 6-11 with a hole
        # where the largest answer had been. Big enough to overflow the window
        # by itself, whatever the fixed prompt around it costs.
        messages = _exchanges(12, oversized={5: 40_000})

        transcript, structured = SynthesisAgent.fit_history(list(messages), **self.BUDGET)

        self.assertEqual(self._kept(structured), [6, 7, 8, 9, 10, 11])
        self.assertEqual([message["role"] for message in structured], ["user", "assistant"] * 6)
        self.assertNotIn("question-0", transcript)
        self.assertNotIn("question-5", transcript)
        # Both renderings say, once, where turns were left out.
        self.assertTrue(structured[0]["content"].startswith(HISTORY_OMISSION_NOTE))
        self.assertEqual(sum(HISTORY_OMISSION_NOTE in message["content"] for message in structured), 1)
        self.assertEqual(transcript.count(HISTORY_OMISSION_NOTE), 1)
        self.assertTrue(transcript.startswith("User: " + HISTORY_OMISSION_NOTE))
        # The two forms still hold the same turns.
        for message in structured:
            self.assertIn(message["content"], transcript)

    def test_history_that_fits_is_sent_whole_without_an_omission_note(self):
        transcript, structured = SynthesisAgent.fit_history(_exchanges(6), **self.BUDGET)

        self.assertEqual(self._kept(structured), [0, 1, 2, 3, 4, 5])
        self.assertNotIn(HISTORY_OMISSION_NOTE, transcript)
        self.assertFalse(any(HISTORY_OMISSION_NOTE in message["content"] for message in structured))

    def test_a_tight_window_drops_the_oldest_exchanges_first(self):
        transcript, structured = SynthesisAgent.fit_history(
            _exchanges(60, size=400), query="next", num_ctx=8192, **_NO_MEMORY
        )

        kept = self._kept(structured)
        self.assertGreater(len(kept), 0)
        self.assertLess(len(kept), 60)
        self.assertEqual(kept, list(range(60 - len(kept), 60)))
        self.assertTrue(structured[0]["content"].startswith(HISTORY_OMISSION_NOTE))

    def test_an_exchange_that_cannot_be_kept_takes_the_older_history_with_it_rather_than_leaving_a_gap(self):
        """The newest exchange is a giant question: there is nothing to shorten.

        The alternative is to skip it and keep the older ones, which is the hole
        this change removes. A conversation either continues from a whole recent
        run or, when not even the newest exchange fits, starts fresh -- and the
        user is told so (see the service tests).
        """
        messages = _exchanges(5)
        messages += [
            {"role": "user", "content": "question-5 " + "u" * 60_000},
            {"role": "assistant", "content": "ok"},
        ]

        transcript, structured = SynthesisAgent.fit_history(messages, **self.BUDGET)

        self.assertEqual(structured, [])
        self.assertNotIn("question-0", transcript)
        # The answer here is two characters: nothing was shortened to get there.
        self.assertNotIn("characters omitted", transcript)

    def test_an_oversized_newest_answer_is_shortened_to_leave_room_for_older_history(self):
        messages = _exchanges(6, oversized={5: 60_000})

        transcript, structured = SynthesisAgent.fit_history(messages, **self.BUDGET)

        self.assertEqual(self._kept(structured), [0, 1, 2, 3, 4, 5])
        newest = structured[-1]["content"]
        self.assertIn("characters omitted", newest)
        self.assertTrue(newest.startswith("answer-5 "))
        self.assertLess(len(newest), 60_000 // 2)
        # It was shortened, not dropped, so nothing was left out.
        self.assertNotIn(HISTORY_OMISSION_NOTE, transcript)


class AttachmentBudgetTests(unittest.TestCase):
    """Reference text is budgeted from the window, not from a fixed number."""

    BUDGET = {
        "query": "Summarize.",
        "chat_history": "No history available.",
        "permanent_memories": [],
        "memories_enabled": False,
        "user_system_instructions": None,
    }

    @staticmethod
    def _document(name: str, text: str) -> GenerationAttachment:
        return GenerationAttachment(
            attachment_id=name, filename=f"{name}.md", mime_type="text/markdown", kind="document", text_content=text
        )

    def _kept_chars(self, attachments, num_ctx: int) -> list[int]:
        fitted = SynthesisAgent.fit_attachments_to_context(attachments, num_ctx=num_ctx, **self.BUDGET)
        return [len(item.text_content or "") for item in fitted]

    def test_attachment_budget_scales_with_the_context_window(self):
        document = self._document("big", "important text. " * 10_000)  # 160,000 characters
        small, medium, large, huge = (
            self._kept_chars((document,), num_ctx) for num_ctx in (8192, 32768, 65536, 262144)
        )

        # More window, more of the document. The old fixed cap stopped at 32,000.
        self.assertLess(small[0], medium[0])
        self.assertLess(medium[0], large[0])
        self.assertGreater(large[0], 32_000)
        # A window with room for all of it keeps all of it, untouched.
        self.assertEqual(huge, [len(document.text_content or "")])
        # And the ceiling holds however large the window is.
        self.assertLessEqual(max(small[0], medium[0], large[0], huge[0]), MAX_DOCUMENT_TEXT_CHARS)

    def test_two_attachments_share_the_budget(self):
        first = self._document("first", "alpha " * 30_000)
        second = self._document("second", "omega " * 30_000)

        first_kept, second_kept = self._kept_chars((first, second), 16384)

        # The first used to take the whole budget and the second arrived as
        # nothing but a truncation notice.
        self.assertGreater(second_kept, 1_000)
        self.assertLess(first_kept, len(first.text_content or ""))
        self.assertLess(second_kept, len(second.text_content or ""))
        self.assertAlmostEqual(first_kept, second_kept, delta=200)
        fitted = SynthesisAgent.fit_attachments_to_context((first, second), num_ctx=16384, **self.BUDGET)
        for item in fitted:
            self.assertIn("truncated to fit the model context", item.text_content or "")

    def test_a_small_document_keeps_all_its_text_and_the_large_one_gets_the_rest(self):
        small = self._document("small", "short note. " * 50)
        large = self._document("large", "long text. " * 40_000)

        small_kept, large_kept = self._kept_chars((small, large), 16384)

        self.assertEqual(small_kept, len(small.text_content or ""))
        self.assertLess(large_kept, len(large.text_content or ""))
        # What the small one did not need went to the large one.
        alone = self._kept_chars((large,), 16384)[0]
        self.assertGreater(large_kept, alone - 2_000)

    def test_every_document_keeps_a_floor_even_when_the_window_has_no_room(self):
        documents = tuple(self._document(f"doc{index}", "filler " * 20_000) for index in range(8))

        kept = self._kept_chars(documents, 1024)

        for length in kept:
            self.assertGreaterEqual(length, 200)
            self.assertLess(length, 1_000)

    def test_attachments_that_carry_no_text_are_returned_untouched(self):
        image = GenerationAttachment(
            attachment_id="img", filename="a.png", mime_type="image/png", kind="image", image_base64="AAAA"
        )
        document = self._document("doc", "fits easily")

        fitted = SynthesisAgent.fit_attachments_to_context((image, document), num_ctx=8192, **self.BUDGET)

        self.assertEqual(fitted, (image, document))
        self.assertEqual(SynthesisAgent.fit_attachments_to_context((), num_ctx=8192, **self.BUDGET), ())

    def test_the_budget_follows_the_learned_ratio_and_wide_text(self):
        english = self._document("english", "word " * 40_000)
        japanese = self._document("japanese", "日本語のテキストです。" * 20_000)

        english_kept = self._kept_chars((english,), 16384)[0]
        japanese_kept = self._kept_chars((japanese,), 16384)[0]
        # Wide characters cost more than a token each, so far fewer fit.
        self.assertLess(japanese_kept, english_kept / 2)

        token_budget.TOKEN_RATIOS.observe("dense", ["x" * 4000], 4000 // 2 + 4)
        dense = SynthesisAgent.fit_attachments_to_context(
            (english,), num_ctx=16384, model="dense", **self.BUDGET
        )
        self.assertLess(len(dense[0].text_content or ""), english_kept)


if __name__ == "__main__":
    unittest.main()


class ChatOverviewTests(unittest.TestCase):
    """The overview must agree with the full load on everything it reports.

    A generation turn reads the thread five times. Three of those reads want
    only the title or the revision, and chat_revision() is the message count --
    so the cheap read has to produce exactly the number the expensive one
    would, or a compare-and-swap starts rejecting valid writes.
    """

    def _repository(self, directory):
        database = DatabaseManager(
            db_path=str(Path(directory) / "chats.sqlite"),
            legacy_history_dir=str(Path(directory) / "history"),
        )
        return LegacyDatabaseChatRepository(database)

    def test_overview_matches_the_full_load_for_every_shared_field(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self._repository(directory)
            repository.add_message("t", "user", "first", thread_title="Named")
            for index in range(9):
                repository.add_message("t", "assistant", f"reply {index}")
                repository.add_message("t", "user", f"question {index}")

            chat = repository.get_chat("t")
            overview = repository.get_chat_overview("t")

            self.assertIsNotNone(overview)
            for field in ("id", "title", "timestamp", "group_id"):
                self.assertEqual(overview[field], chat[field], field)
            self.assertEqual(overview["revision"], len(chat["messages"]))

    def test_overview_tracks_the_revision_as_messages_land(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self._repository(directory)
            repository.add_message("t", "user", "one", thread_title="Named")
            self.assertEqual(repository.get_chat_overview("t")["revision"], 1)
            repository.add_message("t", "assistant", "two")
            self.assertEqual(repository.get_chat_overview("t")["revision"], 2)

    def test_overview_follows_a_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self._repository(directory)
            repository.add_message("t", "user", "one", thread_title="New Chat")
            repository.rename_chat("t", "A better title")

            self.assertEqual(repository.get_chat_overview("t")["title"], "A better title")

    def test_overview_of_an_unknown_thread_is_none(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = self._repository(directory)
            self.assertIsNone(repository.get_chat_overview("missing"))

    def test_the_in_memory_repository_reports_the_same_shape(self):
        memory = InMemoryChatRepository()
        memory.create_chat("t", "Named")
        memory.add_message("t", "user", "one")

        overview = memory.get_chat_overview("t")
        chat = memory.get_chat("t")

        self.assertEqual(set(overview), {"id", "title", "timestamp", "group_id", "revision"})
        self.assertEqual(overview["revision"], len(chat["messages"]))
        self.assertEqual(overview["title"], chat["title"])
        self.assertIsNone(memory.get_chat_overview("missing"))
