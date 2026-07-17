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
from generation_types import ConnectionResult, GenerationResult, GenerationSnapshot  # noqa: E402
from query_worker import GenerationJobController  # noqa: E402


class _ConnectionOrchestrator:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail

    def check_ollama_models_sync(self):
        if self.should_fail:
            raise ConnectionError("Ollama unavailable")
        return ConnectionResult.connected("ready")


class _GenerationOrchestrator:
    def process_query_sync(self, snapshot, status_signal=None):
        time.sleep(0.05)
        return "response", None, "thoughts"


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


if __name__ == "__main__":
    unittest.main()
