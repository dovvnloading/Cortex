"""Headless runtime lifecycle tests for the startup and generation boundary."""

import sys
import time
import unittest
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer


PROJECT_DIR = Path(__file__).parents[1] / "Chat_LLM" / "Chat_LLM"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from Chat_LLM import ConnectionWorker  # noqa: E402
from cortex_backend.services.generation import GenerationServiceResult  # noqa: E402
from cortex_backend.services.progress import ProgressEvent  # noqa: E402
from generation_types import (  # noqa: E402
    ConnectionResult,
    GenerationResult,
    GenerationSnapshot,
    MemoryCommand,
)
from query_worker import GenerationJobController  # noqa: E402


class _ConnectionOrchestrator:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail

    def check_ollama_models_sync(self):
        if self.should_fail:
            raise ConnectionError("Ollama unavailable")
        return ConnectionResult.connected("ready")


class _GenerationOrchestrator:
    def process_query_sync(self, snapshot, progress_sink=None):
        time.sleep(0.05)
        if progress_sink:
            progress_sink.publish(
                ProgressEvent(
                    job_id=snapshot.job_id,
                    thread_id=snapshot.thread_id,
                    phase="analysis",
                    message="Analyzing the request...",
                )
            )
        return "response", None, "thoughts", MemoryCommand()


class _GenerationServiceSpy:
    def __init__(self):
        self.snapshot = None
        self.progress_sink = None

    def generate(self, snapshot, *, progress_sink=None):
        self.snapshot = snapshot
        self.progress_sink = progress_sink
        return GenerationServiceResult("service response", "service thoughts", MemoryCommand())


class _ModelServiceSpy:
    def __init__(self):
        self.required_models = None
        self.optional_models = None

    def check(self, *, required_models, optional_models):
        self.required_models = required_models
        self.optional_models = optional_models
        return ConnectionResult.connected("ready")


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_connection_worker_emits_success_result(self):
        worker = ConnectionWorker()
        worker.orchestrator = _ConnectionOrchestrator()
        results = []
        worker.finished.connect(results.append)

        worker.run()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].status, "connected")

    def test_connection_worker_emits_failure_result(self):
        worker = ConnectionWorker()
        worker.orchestrator = _ConnectionOrchestrator(should_fail=True)
        results = []
        worker.finished.connect(results.append)

        worker.run()

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        self.assertEqual(results[0].status, "error")

    def test_controller_rejects_second_job_and_stale_result(self):
        controller = GenerationJobController(_GenerationOrchestrator())
        results = []
        controller.finished.connect(results.append)
        snapshot = GenerationSnapshot(
            job_id="job-1",
            thread_id="thread-1",
            user_input="hello",
            model="qwen3:8b",
            title_model="granite4:tiny-h",
            translation_model="translategemma:4b",
            model_options={},
            memories_enabled=False,
            translation_enabled=False,
            target_language="English",
            user_system_instructions=None,
        )

        self.assertTrue(controller.start(snapshot))
        self.assertFalse(controller.start(snapshot))
        self.assertFalse(
            controller.accepts(
                GenerationResult.succeeded(
                    "stale", None, job_id="job-2", thread_id="thread-1"
                )
            )
        )

        QTimer.singleShot(500, self.app.quit)
        self.app.exec()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)

    def test_legacy_orchestrator_delegates_generation_to_headless_service(self):
        from Chat_LLM import Orchestrator

        orchestrator = object.__new__(Orchestrator)
        service = _GenerationServiceSpy()
        orchestrator.generation_service = service
        snapshot = GenerationSnapshot(
            job_id="job-delegated",
            thread_id="thread-delegated",
            user_input="hello",
            model="qwen3:8b",
            title_model="granite4:tiny-h",
            translation_model="translategemma:4b",
            model_options={},
            memories_enabled=False,
            translation_enabled=False,
            target_language="English",
            user_system_instructions=None,
        )

        result = orchestrator.process_query_sync(snapshot)

        self.assertEqual(result, ("service response", None, "service thoughts", MemoryCommand()))
        self.assertIs(service.snapshot, snapshot)
        self.assertIsNone(service.progress_sink)

    def test_legacy_orchestrator_delegates_exact_model_requirements(self):
        from Chat_LLM import Orchestrator

        orchestrator = object.__new__(Orchestrator)
        service = _ModelServiceSpy()
        orchestrator.model_service = service
        orchestrator.config = {
            "gen_model": "qwen3:8b",
            "title_model": "granite4:tiny-h",
            "translation_model": "translategemma:4b",
        }
        orchestrator.translation_enabled = True
        orchestrator.suggestions_enabled = True
        orchestrator.suggestions_model = "qwen3:4b"

        result = orchestrator.check_ollama_models_sync()

        self.assertTrue(result.success)
        self.assertEqual(service.required_models, ("qwen3:8b", "granite4:tiny-h"))
        self.assertEqual(
            service.optional_models,
            ["translategemma:4b", "qwen3:4b"],
        )


if __name__ == "__main__":
    unittest.main()
