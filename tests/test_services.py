"""Headless tests for the service layer extracted out of the routes."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import unittest

from cortex_backend.core.generation import (
    CodeExecutionProposal,
    GenerationAttachment,
    GenerationSnapshot,
    GenerationStats,
    MemoryCommand,
    ModelOperationError,
    TranslationResult,
)
from cortex_backend.services.generation import GenerationService
from cortex_backend.services.llm import SynthesisAgent
from cortex_backend.services.models import ModelService
from cortex_backend.services.progress import ProgressEvent
from cortex_backend.testing.fake_ollama import FakeGenerationEngine, FakeOllamaState


class _ProgressRecorder:
    def __init__(self):
        self.events: list[ProgressEvent] = []

    def publish(self, event: ProgressEvent) -> None:
        self.events.append(event)


class _FakeEngine:
    """A double that implements the whole GenerationEngine surface.

    It used to implement only part of it, which is why the service probed for
    the rest with getattr. The probes are gone, so a double that skips a member
    is a double of an engine that cannot exist.
    """

    def __init__(self, *, translation: TranslationResult | None = None):
        self.translation = translation or TranslationResult.succeeded("translated")
        self.history_messages: list[dict] | None = None
        self.memory_inputs: list[str] | None = None
        self.options: dict | None = None
        self.title_history: str | None = None
        self.title_response: str | None = None
        self.last_code_proposal = None
        self.last_code_rejection = None
        self._status_callback = None

    def set_status_callback(self, callback) -> None:
        self._status_callback = callback

    def fit_memories_to_context(
        self,
        memories: list[str],
        *,
        query: str,
        user_system_instructions: str | None,
        num_ctx: int,
        code_execution_eligible: bool | None = None,
        bypass_system_prompt: bool = False,
        host_observations=(),
    ) -> list[str]:
        del code_execution_eligible, bypass_system_prompt, host_observations
        self.memory_inputs = list(memories)
        return list(memories)

    def fit_history_to_context(
        self,
        messages: list[dict],
        *,
        query: str,
        permanent_memories: list[str],
        memories_enabled: bool,
        user_system_instructions: str | None,
        num_ctx: int,
        code_execution_eligible: bool | None = None,
        bypass_system_prompt: bool = False,
        host_observations=(),
        attachments=(),
    ) -> str:
        del code_execution_eligible, bypass_system_prompt, host_observations, attachments
        self.history_messages = messages
        return "formatted history"

    def fit_history(self, messages, **kwargs):
        return self.fit_history_to_context(messages, **kwargs), list(messages)

    def fit_attachments_to_context(self, attachments, **kwargs):
        del kwargs
        return tuple(attachments)

    def generate(
        self,
        *,
        query: str,
        chat_history: str,
        permanent_memories: list[str],
        memories_enabled: bool,
        user_system_instructions: str | None,
        options: dict,
        attachments=(),
        cancellation_event=None,
        history_messages=None,
        host_observations=(),
        on_delta=None,
    ) -> tuple[str, str | None, MemoryCommand, GenerationStats | None]:
        del attachments, cancellation_event, history_messages, host_observations
        self.options = options
        stats = GenerationStats(eval_count=10, eval_duration_ms=100.0, tokens_per_second=100.0)
        return "response", "thoughts", MemoryCommand(("remember tea",), False), stats

    def translate_text(self, text: str, target_language: str) -> TranslationResult:
        return self.translation

    def generate_chat_title(self, chat_history: str, *, options=None) -> str | None:
        del options
        self.title_history = chat_history
        return self.title_response


class _StatusReportingEngine(_FakeEngine):
    """An engine that reports startup progress -- e.g. a llama.cpp runtime
    downloading a binary or loading a model -- through set_status_callback."""

    def generate(self, **kwargs):
        if self._status_callback is not None:
            self._status_callback("Starting the local model...")
        return super().generate(**kwargs)


class _CrashingDuringTranslationEngine(_FakeEngine):
    """An engine whose translate_text accepts the modern ``options`` kwarg
    (so the call is never a signature mismatch) but hits a genuine bug --
    e.g. an unexpected response shape -- after it has already started real
    work. Used to prove such a TypeError is not mistaken for a "this engine
    doesn't take options" probe failure and silently retried."""

    def __init__(self):
        super().__init__()
        self.translate_calls = 0
        self.started_real_work = False

    def translate_text(
        self, text: str, target_language: str, options: dict | None = None
    ) -> TranslationResult:
        del text, target_language, options
        self.translate_calls += 1
        self.started_real_work = True
        raise TypeError("boom: bad response shape mid-translation")


class _CrashingDuringTitleEngine(_FakeEngine):
    """Same hazard as _CrashingDuringTranslationEngine, for the chat-title
    retry site."""

    def __init__(self):
        super().__init__()
        self.title_calls = 0
        self.started_real_work = False

    def generate_chat_title(
        self, chat_history: str, options: dict | None = None
    ) -> str | None:
        del chat_history, options
        self.title_calls += 1
        self.started_real_work = True
        raise TypeError("boom: bad response shape mid-title-generation")


class _RecordingTranslationEngine(_FakeEngine):
    """Records what the service hands to the translation call."""

    def __init__(self):
        super().__init__()
        self.translation_cancellation: object = "never called"

    def translate_text(
        self, text: str, target_language: str, *, options=None, cancellation_event=None
    ) -> TranslationResult:
        del text, target_language, options
        self.translation_cancellation = cancellation_event
        return TranslationResult.succeeded("translated")


class _FakeGateway:
    def __init__(self, listings: list[dict]):
        self.listings = iter(listings)
        self.pulled: list[str] = []

    def list(self):
        return next(self.listings)

    def pull(self, model: str):
        self.pulled.append(model)


def _snapshot(**overrides) -> GenerationSnapshot:
    values = {
        "job_id": "job-1",
        "thread_id": "thread-1",
        "user_input": "hello",
        "model": "qwen3:8b",
        "title_model": "granite4:tiny-h",
        "translation_model": "translategemma:4b",
        "model_options": {"temperature": 0.7, "num_ctx": 4096, "seed": -1},
        "memories_enabled": True,
        "translation_enabled": True,
        "target_language": "French",
        "user_system_instructions": "Be concise.",
    }
    values.update(overrides)
    return GenerationSnapshot(**values)


class GenerationServiceTests(unittest.TestCase):
    def test_an_attachment_works_with_the_shipped_double_that_declares_the_protocol(self):
        """FakeGenerationEngine is wired as the GenerationEngine for the
        Playwright e2e backend and the screenshot servers, so the service must
        be able to call every member the protocol declares.

        ``fit_attachments_to_context`` was the one method the protocol did not
        give a ``host_observations`` parameter, while the service passed it to
        all five. The mismatch was invisible to mypy because the argument was
        splatted from a ``dict[str, Any]``; it surfaced only as a TypeError the
        first time anyone attached a file.
        """
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: FakeGenerationEngine(FakeOllamaState()),
        )
        attachment = GenerationAttachment(
            attachment_id="attachment-1",
            filename="notes.txt",
            mime_type="text/plain",
            kind="document",
            text_content="a document the user attached",
        )

        result = service.generate(
            _snapshot(
                memories_enabled=False,
                translation_enabled=False,
                attachments=(attachment,),
                host_observations="a verified computation",
            )
        )

        self.assertTrue(result.response)

    def test_the_translation_call_is_given_the_turn_s_cancellation_event(self):
        """Translation is a second full model call and must honour Stop.

        Every other model call on this path already receives the event; this
        one did not, so pressing Stop while the status read
        "Translating to French..." did nothing until the translation model
        finished on its own. The service checks cancellation immediately before
        and after the call, which is exactly why the gap was invisible: the
        turn does end as cancelled, only after paying for a translation whose
        result is then thrown away.
        """
        event = Event()
        engine = _RecordingTranslationEngine()
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        service.generate(_snapshot(memories_enabled=False), cancellation_event=event)

        self.assertIs(engine.translation_cancellation, event)

    def test_translate_text_forwards_cancellation_to_the_chat_client(self):
        """The other half of the chain: the engine must pass it on.

        ``ChatClient.chat`` has always accepted ``cancellation_event`` and both
        implementations act on it, so once the service forwards the event the
        translation call becomes interruptible for free.
        """
        seen: dict = {}

        class _Client:
            def chat(self, *, model, messages, options, cancellation_event=None):
                del messages, options
                seen["model"] = model
                seen["cancellation_event"] = cancellation_event
                return {"message": {"content": "bonjour"}}

        event = Event()
        agent = SynthesisAgent(
            "qwen3:8b", "granite4:tiny-h", "translategemma:4b", _Client()
        )

        result = agent.translate_text("hello", "French", cancellation_event=event)

        self.assertTrue(result.success)
        self.assertEqual(seen["model"], "translategemma:4b")
        self.assertIs(seen["cancellation_event"], event)

    def test_the_status_callback_does_not_outlive_its_turn(self):
        """A per-turn callback must not stay on the process-wide chat client.

        `SynthesisAgent.set_status_callback` forwards to the chat client, which
        is built once for the process while the engine is built per turn. The
        callback closes over the snapshot -- attachments and all -- so leaving
        it installed kept that turn alive for the life of the process, and any
        later status message reached a finished turn: `generate_chat_title`
        builds a fresh engine and installs no callback of its own, so a model
        load during titling published "loading_model" against a job that had
        already completed.
        """
        installed: list[object] = []

        class _SharedClientEngine(_FakeEngine):
            def set_status_callback(self, callback) -> None:
                installed.append(callback)

        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: _SharedClientEngine(),
        )

        service.generate(_snapshot(memories_enabled=False, translation_enabled=False))

        self.assertTrue(installed, "a callback was never installed at all")
        self.assertIsNone(installed[-1], "the turn's callback was left on the shared client")

    def test_the_status_callback_is_detached_even_when_the_turn_fails(self):
        """The failure path is the one that matters most: it leaves a
        half-finished turn behind, and that is exactly when a stale callback
        would misattribute the next runtime message."""
        installed: list[object] = []

        class _FailingEngine(_FakeEngine):
            def set_status_callback(self, callback) -> None:
                installed.append(callback)

            def generate(self, **kwargs):
                raise ModelOperationError("the model fell over", operation="generation")

        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: _FailingEngine(),
        )

        with self.assertRaises(ModelOperationError):
            service.generate(_snapshot(memories_enabled=False, translation_enabled=False))

        self.assertIsNone(installed[-1], "a failed turn left its callback installed")

    def test_generation_is_headless_and_emits_owned_typed_progress(self):
        engine = _FakeEngine()
        recorder = _ProgressRecorder()
        service = GenerationService(
            history_loader=lambda thread_id: [
                {"role": "assistant", "content": "old"},
                {"role": "user", "content": "current"},
            ],
            memory_loader=lambda: ["remember tea"],
            engine_factory=lambda snapshot: engine,
        )

        result = service.generate(_snapshot(), progress_sink=recorder)

        self.assertEqual(result.response, "translated")
        self.assertEqual(result.thoughts, "thoughts")
        self.assertEqual(result.memory_command.additions, ("remember tea",))
        self.assertEqual(
            [event.phase for event in recorder.events],
            ["analysis", "thoughts", "translation"],
        )
        self.assertTrue(all(event.job_id == "job-1" for event in recorder.events))
        self.assertTrue(all(event.thread_id == "thread-1" for event in recorder.events))
        self.assertEqual(engine.history_messages, [{"role": "assistant", "content": "old"}])
        self.assertEqual(engine.memory_inputs, ["remember tea"])
        self.assertEqual(engine.options["num_ctx"], 4096)

    def test_attachment_reference_text_is_not_crushed_by_a_full_history_fit(self):
        """Regression guard: history used to be fit to the context budget
        first, greedily claiming nearly all of it before attachments were
        ever considered, so a document attached mid-conversation could be
        cut to a tiny fragment even though it would easily have fit had it
        been given any priority over old chat turns. Attachments must now be
        reserved room before history is sized around them.
        """
        class _CapturingChatClient:
            def __init__(self):
                self.last_messages: list[dict] | None = None

            def chat(self, *, model, messages, options, on_delta=None, cancellation_event=None):
                del model, options, on_delta, cancellation_event
                self.last_messages = messages
                return {"message": {"content": "ok", "thinking": None}}

        history_messages = []
        for index in range(20):
            history_messages.append({"role": "user", "content": f"old-{index} " + ("details " * 80)})
            history_messages.append({"role": "assistant", "content": f"reply-{index} " + ("context " * 80)})

        attachment = GenerationAttachment(
            attachment_id="doc-1",
            filename="report.md",
            mime_type="text/markdown",
            kind="document",
            text_content="report line " * 400,
        )

        client = _CapturingChatClient()
        service = GenerationService(
            history_loader=lambda thread_id: history_messages,
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: SynthesisAgent(
                "chat-model", "title-model", "translate-model", client,
            ),
        )
        snapshot = GenerationSnapshot(
            job_id="job-1",
            thread_id="thread-1",
            user_input="Summarize the attached report.",
            model="chat-model",
            title_model="title-model",
            translation_model="translate-model",
            model_options={"temperature": 0.7, "num_ctx": 4096, "seed": -1},
            memories_enabled=False,
            translation_enabled=False,
            target_language="French",
            user_system_instructions=None,
            attachments=(attachment,),
        )

        service.generate(snapshot)

        self.assertIsNotNone(client.last_messages)
        sent_content = "\n".join(str(message.get("content", "")) for message in client.last_messages or [])
        retained_repeats = sent_content.count("report line")
        self.assertGreater(
            retained_repeats,
            300,
            f"Only {retained_repeats}/400 repetitions of the attachment text survived context "
            "fitting -- the attachment was crushed by history claiming the whole budget first.",
        )

    def test_engine_status_callback_reports_as_loading_model_progress(self):
        """An engine backed by a locally-managed runtime (llama.cpp) can
        report its own startup progress through the normal progress sink,
        so a slow first launch doesn't look like Cortex has hung. Engines
        that don't define set_status_callback (the common case) are
        unaffected -- covered by every other test in this file, none of
        which define it."""
        engine = _StatusReportingEngine()
        recorder = _ProgressRecorder()
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        service.generate(_snapshot(memories_enabled=False, translation_enabled=False), progress_sink=recorder)

        loading_events = [event for event in recorder.events if event.phase == "loading_model"]
        self.assertEqual([event.message for event in loading_events], ["Starting the local model..."])

    def test_disabled_memories_remove_model_requested_memory_actions(self):
        engine = _FakeEngine(translation=TranslationResult.succeeded("response"))
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: ["must not be loaded"],
            engine_factory=lambda snapshot: engine,
        )

        result = service.generate(
            _snapshot(memories_enabled=False, translation_enabled=False)
        )

        self.assertEqual(result.memory_command, MemoryCommand())
        self.assertIsNone(engine.memory_inputs)

    def test_ineligible_generation_discards_a_model_code_proposal(self):
        engine = _FakeEngine()
        engine.last_code_proposal = CodeExecutionProposal(
            source="print('should not queue')",
            intent_summary="Test proposal",
        )
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        result = service.generate(
            _snapshot(memories_enabled=False, translation_enabled=False),
        )

        self.assertIsNone(result.code_execution_proposal)

    def test_new_turn_generates_a_bounded_chat_title_without_affecting_response(self):
        engine = _FakeEngine()
        engine.title_response = "Project planning"
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        result = service.generate(
            _snapshot(user_input="Plan a focused launch for Cortex"),
            progress_sink=_ProgressRecorder(),
        )
        title = service.generate_chat_title(
            _snapshot(user_input="Plan a focused launch for Cortex"),
            result.response,
        )

        self.assertEqual(result.response, "translated")
        self.assertEqual(title, "Project planning")
        self.assertEqual(
            engine.title_history,
            "User: Plan a focused launch for Cortex\nAssistant: translated",
        )

    def test_response_generation_does_not_call_optional_title_model(self):
        engine = _FakeEngine()
        engine.title_response = "Should not be used"
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        service.generate(_snapshot(), progress_sink=_ProgressRecorder())

        self.assertIsNone(engine.title_history)

    def test_a_failed_translation_keeps_the_untranslated_answer(self):
        """A post-process failure must not discard work the model already did.

        The assistant turn is persisted only after generate() returns, so
        raising here threw away a finished answer and left the user with
        "Translation failed. Please try again." and nothing else. On a machine
        near its memory limit, loading the translation model is the call most
        likely to fail -- precisely when the answer is most expensive to lose.
        """
        engine = _FakeEngine(
            translation=TranslationResult.failed(
                "Translation failed. Please try again.",
                error_details="transport",
            )
        )
        recorder = _ProgressRecorder()
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        result = service.generate(_snapshot(), progress_sink=recorder)

        self.assertEqual(result.response, "response")
        self.assertIsNotNone(result.translation_error)
        self.assertIn("Translation failed", result.translation_error)
        self.assertIn("translation_failed", [event.phase for event in recorder.events])

    def test_an_invalid_translation_result_keeps_the_answer_too(self):
        engine = _FakeEngine(translation="not a TranslationResult")
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        result = service.generate(_snapshot())

        self.assertEqual(result.response, "response")
        self.assertIsNotNone(result.translation_error)

    def test_translation_type_error_from_inside_the_call_is_not_retried(self):
        """Regression guard: a TypeError raised by translate_text() itself,
        once it has already started real work, must propagate rather than
        being mistaken for a "this engine doesn't accept options" signature
        probe and silently retried -- a retry here would call a real model
        a second, unwanted time.
        """
        engine = _CrashingDuringTranslationEngine()
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        with self.assertRaises(TypeError):
            service.generate(_snapshot())

        self.assertTrue(engine.started_real_work)
        self.assertEqual(engine.translate_calls, 1)

    def test_title_type_error_from_inside_the_call_is_not_retried(self):
        """Same hazard as above for the chat-title retry site: a TypeError
        raised once generate_chat_title() has already started real work
        must not trigger a second call. generate_chat_title()'s outer
        ``except Exception`` still treats the (single) failure as
        non-fatal -- titling is optional -- but the callable itself may run
        at most once.
        """
        engine = _CrashingDuringTitleEngine()
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        title = service.generate_chat_title(_snapshot(), "some response")

        self.assertIsNone(title)
        self.assertTrue(engine.started_real_work)
        self.assertEqual(engine.title_calls, 1)

    def test_backend_service_import_does_not_load_qt(self):
        repository_root = Path(__file__).parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(repository_root / "backend")
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "from cortex_backend.services.generation import GenerationService; "
                    "from cortex_backend.services.models import ModelService; "
                    "assert 'PySide6' not in sys.modules"
                ),
            ],
            cwd=repository_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(process.returncode, 0, process.stderr)


class ModelServiceTests(unittest.TestCase):
    def test_inventory_exposes_ollama_capabilities_and_vision_support(self):
        class CapabilityGateway:
            def list(self):
                return {"models": [{"name": "vision-model"}, {"name": "text-model"}]}

            def show(self, model: str):
                return {
                    "capabilities": ["completion", "vision"]
                    if model == "vision-model"
                    else ["completion"]
                }

        inventory, connection = ModelService(CapabilityGateway()).inventory()

        self.assertTrue(connection.success)
        self.assertEqual(inventory[0].name, "text-model")
        self.assertFalse(inventory[0].supports_vision)
        self.assertEqual(inventory[0].capabilities, ("completion",))
        self.assertTrue(inventory[1].supports_vision)
        self.assertEqual(inventory[1].capabilities, ("completion", "vision"))

    def test_inventory_derives_model_details_from_the_same_show_response(self):
        class DetailedGateway:
            def list(self):
                return {"models": [{"name": "qwen3:8b"}]}

            def show(self, model: str):
                return {
                    "capabilities": ["completion"],
                    "details": {"family": "qwen3", "parameter_size": "8.0B", "quantization_level": "Q4_K_M"},
                    "model_info": {"qwen3.context_length": 40960, "unrelated.context_length": 999},
                }

        inventory, _ = ModelService(DetailedGateway()).inventory()

        self.assertEqual(inventory[0].parameter_size, "8.0B")
        self.assertEqual(inventory[0].quantization_level, "Q4_K_M")
        self.assertEqual(inventory[0].family, "qwen3")
        self.assertEqual(inventory[0].context_length, 40960)

    def test_inventory_reads_context_length_from_a_real_ollama_show_response(self):
        """The dict above is the wire shape; the client returns a model.

        ``ollama``'s ``ShowResponse`` names the field ``modelinfo`` and keeps
        ``model_info`` only as its serialization alias, so neither attribute
        access nor that model's own ``get`` finds it under the wire name. Every
        Ollama model therefore reported no context length, and the Models panel
        showed a context window for local GGUF models but never for Ollama
        ones. Only a double built from a plain dict hid it.
        """
        from ollama._types import ShowResponse

        response = ShowResponse.model_validate(
            {
                "capabilities": ["completion"],
                "details": {
                    "family": "qwen3",
                    "parameter_size": "8.0B",
                    "quantization_level": "Q4_K_M",
                },
                "model_info": {"qwen3.context_length": 40960},
            }
        )

        class RealShapeGateway:
            def list(self):
                return {"models": [{"name": "qwen3:8b"}]}

            def show(self, model: str):
                return response

        inventory, _ = ModelService(RealShapeGateway()).inventory()

        self.assertEqual(inventory[0].context_length, 40960)
        # The neighbouring fields already worked -- their field names match the
        # wire names -- and must keep working.
        self.assertEqual(inventory[0].family, "qwen3")
        self.assertEqual(inventory[0].parameter_size, "8.0B")
        self.assertEqual(inventory[0].capabilities, ("completion",))

    def test_show_details_tolerates_a_response_missing_details_and_model_info(self):
        class MinimalGateway:
            def list(self):
                return {"models": [{"name": "qwen3:8b"}]}

            def show(self, model: str):
                return {"capabilities": ["completion"]}

        details = ModelService(MinimalGateway()).show_details("qwen3:8b")

        self.assertIsNotNone(details)
        self.assertEqual(details.capabilities, ("completion",))
        self.assertIsNone(details.parameter_size)
        self.assertIsNone(details.quantization_level)
        self.assertIsNone(details.family)
        self.assertIsNone(details.context_length)

    def test_show_details_is_none_without_a_family_prefixed_context_length(self):
        class NoContextLengthGateway:
            def list(self):
                return {"models": []}

            def show(self, model: str):
                return {"capabilities": [], "details": {"family": "qwen3"}, "model_info": {}}

        details = ModelService(NoContextLengthGateway()).show_details("qwen3:8b")

        self.assertIsNotNone(details)
        self.assertEqual(details.family, "qwen3")
        self.assertIsNone(details.context_length)

    def test_show_details_is_none_when_the_gateway_has_no_show_method(self):
        class ListOnlyGateway:
            def list(self):
                return {"models": []}

        self.assertIsNone(ModelService(ListOnlyGateway()).show_details("anything"))
        self.assertIsNone(ModelService(ListOnlyGateway()).capabilities("anything"))
        self.assertIsNone(ModelService(ListOnlyGateway()).model_supports_vision("anything"))

    def test_extracts_legacy_object_and_current_dict_model_shapes(self):
        class ModelEntry:
            model = "qwen3:8b"

        class ModelResponse:
            models = [ModelEntry()]

        self.assertEqual(
            ModelService.extract_model_tags(
                {"models": [{"model": "gemma3:4b"}]}
            ),
            {"gemma3:4b"},
        )
        self.assertEqual(
            ModelService.extract_model_tags(ModelResponse()),
            {"qwen3:8b"},
        )

    def test_pulls_only_missing_required_tags_and_reports_optional_tags(self):
        gateway = _FakeGateway(
            [
                {"models": [{"name": "qwen3:8b"}]},
                {"models": [
                    {"name": "qwen3:8b"},
                    {"name": "granite4:tiny-h"},
                ]},
            ]
        )

        result = ModelService(gateway).check(
            required_models=("qwen3:8b", "granite4:tiny-h"),
            optional_models=("translategemma:4b",),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.missing_models, ("granite4:tiny-h",))
        self.assertEqual(result.optional_missing_models, ("translategemma:4b",))
        self.assertEqual(gateway.pulled, ["granite4:tiny-h"])

    def test_model_gateway_failures_return_safe_connection_result(self):
        class BrokenGateway:
            def list(self):
                raise ConnectionError("private transport detail")

            def pull(self, model: str):
                raise AssertionError(model)

        result = ModelService(BrokenGateway()).check(required_models=("qwen3:8b",))

        self.assertFalse(result.success)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.details, "ConnectionError")
        self.assertNotIn("private transport detail", result.message)

    def test_missing_required_tags_are_reported_after_an_unsuccessful_pull(self):
        gateway = _FakeGateway(
            [
                {"models": [{"name": "qwen3:8b"}]},
                {"models": [{"name": "qwen3:8b"}]},
            ]
        )

        result = ModelService(gateway).check(
            required_models=("qwen3:8b", "granite4:tiny-h")
        )

        self.assertFalse(result.success)
        self.assertEqual(result.missing_models, ("granite4:tiny-h",))
        self.assertEqual(gateway.pulled, ["granite4:tiny-h"])


if __name__ == "__main__":
    unittest.main()


class StreamingGenerationTests(unittest.TestCase):
    """The user sees the answer as the model writes it, not after."""

    class _StreamingEngine(_FakeEngine):
        """An engine that produces its answer a piece at a time."""

        def __init__(self, pieces, **kwargs):
            super().__init__(**kwargs)
            self._pieces = pieces

        def generate(
            self,
            *,
            query,
            chat_history,
            permanent_memories,
            memories_enabled,
            user_system_instructions,
            options,
            attachments=(),
            cancellation_event=None,
            history_messages=None,
            host_observations=(),
            on_delta=None,
        ):
            del (
                query,
                chat_history,
                permanent_memories,
                memories_enabled,
                user_system_instructions,
                options,
                attachments,
                cancellation_event,
                history_messages,
                host_observations,
            )
            if on_delta is not None:
                for kind, text in self._pieces:
                    on_delta(kind, text)
            answer = "".join(text for kind, text in self._pieces if kind == "content")
            stats = GenerationStats(eval_count=3, eval_duration_ms=10.0, tokens_per_second=300.0)
            return answer, "thoughts", MemoryCommand((), False), stats

    def test_live_deltas_reach_the_sink_and_mark_the_result_streamed(self):
        recorder = _ProgressRecorder()
        engine = self._StreamingEngine(
            [("thinking", "hmm"), ("content", "Hello "), ("content", "world")]
        )
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        result = service.generate(_snapshot(), progress_sink=recorder)

        # Assert on the reassembled stream, not on delta boundaries: adjacent
        # pieces are coalesced, so where one event ends and the next begins is
        # an implementation detail the user never sees.
        def joined(phase: str) -> str:
            return "".join(
                (event.data or {}).get("delta", "")
                for event in recorder.events
                if event.phase == phase
            )

        self.assertEqual(joined("thinking_delta"), "hmm")
        self.assertEqual(joined("content_delta"), "Hello world")
        self.assertLess(
            len([e for e in recorder.events if e.phase == "content_delta"]),
            3,
            "adjacent content pieces should coalesce into fewer events",
        )
        self.assertTrue(
            result.streamed,
            "the API replays the finished answer unless the engine says it streamed",
        )

    def test_a_translated_turn_streams_the_original_then_replaces_it(self):
        """Deliberate: live feedback beats a spinner, even when it is replaced.

        Translation runs after the answer exists, so what streams is the
        original. The completed event carries the translated text and the
        client swaps it in atomically. Translation is off by default; when it
        is on, watching the model work is still better than watching nothing.
        """
        recorder = _ProgressRecorder()
        engine = self._StreamingEngine([("content", "Hello world")])
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        result = service.generate(_snapshot(), progress_sink=recorder)

        streamed_text = "".join(
            (event.data or {}).get("delta", "")
            for event in recorder.events
            if event.phase == "content_delta"
        )
        self.assertEqual(streamed_text, "Hello world")
        self.assertEqual(result.response, "translated")

    def test_a_non_streaming_engine_leaves_the_replay_to_the_api(self):
        """The deterministic double returns a whole answer, as before.

        Its turns must still be marked un-streamed, or the API would skip the
        replay and the client would receive no content at all.
        """
        recorder = _ProgressRecorder()
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: _FakeEngine(),
        )

        result = service.generate(_snapshot(), progress_sink=recorder)

        self.assertFalse(result.streamed)
        self.assertEqual(
            [e for e in recorder.events if e.phase in {"content_delta", "thinking_delta"}],
            [],
        )

    def test_an_engine_that_streams_only_whitespace_is_not_called_streamed(self):
        """An empty delta must not suppress the replay.

        streamed is what tells the API it may skip the replay, so a delta that
        carries no text has to leave it false -- otherwise a turn that emitted
        nothing would reach the client with an empty transcript.
        """
        engine = self._StreamingEngine([("content", "")])
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        result = service.generate(_snapshot(), progress_sink=_ProgressRecorder())

        self.assertFalse(result.streamed)

    def test_an_unknown_delta_kind_is_ignored_rather_than_guessed_at(self):
        engine = self._StreamingEngine([("audio", "beep"), ("content", "hi")])
        recorder = _ProgressRecorder()
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        service.generate(_snapshot(), progress_sink=recorder)

        phases = [event.phase for event in recorder.events]
        self.assertIn("content_delta", phases)
        self.assertNotIn("audio", phases)

    def test_a_fast_stream_is_coalesced_instead_of_flooding_the_event_log(self):
        """One SSE event per token would outgrow the job's retained window.

        The registry keeps a bounded number of events per job, so a long answer
        streamed token by token would push its own earlier deltas out of the
        window a reconnecting client still needs. Adjacent pieces are therefore
        merged; the text the user ends up with is unchanged.
        """
        pieces = [("content", "tok ")] * 200
        engine = self._StreamingEngine(pieces)
        recorder = _ProgressRecorder()
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        service.generate(_snapshot(), progress_sink=recorder)

        events = [e for e in recorder.events if e.phase == "content_delta"]
        self.assertLess(
            len(events),
            len(pieces) // 4,
            f"{len(events)} events for {len(pieces)} tokens is not coalescing",
        )
        self.assertEqual(
            "".join((e.data or {}).get("delta", "") for e in events),
            "".join(text for _, text in pieces),
            "coalescing must not lose or reorder a single character",
        )

    def test_buffered_text_is_flushed_even_when_the_engine_raises(self):
        """A partial answer the user already watched arrive is not swallowed."""

        class _FailsAfterStreaming(self._StreamingEngine):
            def generate(self, **kwargs):
                on_delta = kwargs.get("on_delta")
                if on_delta is not None:
                    on_delta("content", "partial")
                raise ModelOperationError("boom", operation="generation")

        recorder = _ProgressRecorder()
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: _FailsAfterStreaming([]),
        )

        with self.assertRaises(ModelOperationError):
            service.generate(_snapshot(), progress_sink=recorder)

        self.assertEqual(
            "".join(
                (e.data or {}).get("delta", "")
                for e in recorder.events
                if e.phase == "content_delta"
            ),
            "partial",
        )

    def test_an_engine_that_emits_only_unknown_kinds_is_not_called_streamed(self):
        """streamed must mean "the user saw something", not "a hook fired".

        It is what tells the API it may skip replaying the finished answer, so
        a kind the publisher drops has to leave it false -- otherwise the
        replay is skipped having shown the user nothing at all.
        """
        engine = self._StreamingEngine([("audio", "beep")])
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: engine,
        )

        result = service.generate(_snapshot(), progress_sink=_ProgressRecorder())

        self.assertFalse(result.streamed)
