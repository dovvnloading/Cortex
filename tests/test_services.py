"""Headless tests for the Stage 1 Phase 2 service extraction."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest

from cortex_backend.core.generation import (
    CodeExecutionProposal,
    GenerationSnapshot,
    GenerationStats,
    MemoryCommand,
    ModelOperationError,
    TranslationResult,
)
from cortex_backend.services.generation import GenerationService
from cortex_backend.services.models import ModelService
from cortex_backend.services.progress import ProgressEvent


class _ProgressRecorder:
    def __init__(self):
        self.events: list[ProgressEvent] = []

    def publish(self, event: ProgressEvent) -> None:
        self.events.append(event)


class _FakeEngine:
    def __init__(self, *, translation: TranslationResult | None = None):
        self.translation = translation or TranslationResult.succeeded("translated")
        self.history_messages: list[dict] | None = None
        self.memory_inputs: list[str] | None = None
        self.options: dict | None = None
        self.title_history: str | None = None
        self.title_response: str | None = None

    def fit_memories_to_context(
        self,
        memories: list[str],
        *,
        query: str,
        user_system_instructions: str | None,
        num_ctx: int,
        code_execution_eligible: bool | None = None,
    ) -> list[str]:
        del code_execution_eligible
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
    ) -> str:
        del code_execution_eligible
        self.history_messages = messages
        return "formatted history"

    def generate(
        self,
        *,
        query: str,
        chat_history: str,
        permanent_memories: list[str],
        memories_enabled: bool,
        user_system_instructions: str | None,
        options: dict,
    ) -> tuple[str, str | None, MemoryCommand, GenerationStats | None]:
        self.options = options
        stats = GenerationStats(eval_count=10, eval_duration_ms=100.0, tokens_per_second=100.0)
        return "response", "thoughts", MemoryCommand(("remember tea",), False), stats

    def translate_text(self, text: str, target_language: str) -> TranslationResult:
        return self.translation

    def generate_chat_title(self, chat_history: str) -> str | None:
        self.title_history = chat_history
        return self.title_response


class _StatusReportingEngine(_FakeEngine):
    """An engine that reports startup progress (e.g. a llama.cpp runtime
    downloading/starting) via the optional set_status_callback hook."""

    def __init__(self):
        super().__init__()
        self._status_callback = None

    def set_status_callback(self, callback) -> None:
        self._status_callback = callback

    def generate(self, **kwargs):
        if self._status_callback is not None:
            self._status_callback("Starting the local model...")
        return super().generate(**kwargs)


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

    def test_failed_translation_is_a_safe_model_operation_error(self):
        service = GenerationService(
            history_loader=lambda thread_id: [],
            memory_loader=lambda: [],
            engine_factory=lambda snapshot: _FakeEngine(
                translation=TranslationResult.failed(
                    "Translation failed. Please try again.",
                    error_details="transport",
                )
            ),
        )

        with self.assertRaisesRegex(ModelOperationError, "Translation failed") as raised:
            service.generate(_snapshot())

        self.assertEqual(raised.exception.operation, "translation")
        self.assertEqual(raised.exception.error_details, None)

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
