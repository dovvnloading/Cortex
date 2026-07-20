# -*- coding: utf-8 -*-
"""Threaded interactive generation with explicit job ownership."""

import logging

from PySide6.QtCore import QObject, QThread, Signal

from cortex_backend.services.progress import ProgressEvent, ProgressSink
from generation_types import GenerationResult, GenerationSnapshot


class QtProgressSink:
    """Adapt typed backend progress to the legacy Qt signal boundary."""

    def __init__(self, signal: Signal):
        self._signal = signal

    def publish(self, event: ProgressEvent) -> None:
        self._signal.emit(event.message, event.job_id)


class QueryWorker(QObject):
    """Run one immutable generation snapshot outside the UI thread."""

    status_updated = Signal(str, str)  # status text, job id
    finished = Signal(object)  # GenerationResult

    def __init__(self, orchestrator, snapshot: GenerationSnapshot):
        super().__init__()
        self.orchestrator = orchestrator
        self.snapshot = snapshot

    def run(self) -> None:
        job_id = self.snapshot.job_id
        thread_id = self.snapshot.thread_id
        try:
            if QThread.currentThread().isInterruptionRequested():
                self.finished.emit(
                    GenerationResult.failed(
                        "Generation cancelled.",
                        job_id=job_id,
                        thread_id=thread_id,
                    )
                )
                return

            progress_sink: ProgressSink = QtProgressSink(self.status_updated)
            response, _, thoughts, memory_command = self.orchestrator.process_query_sync(
                self.snapshot,
                progress_sink=progress_sink,
            )
            self.finished.emit(
                GenerationResult.succeeded(
                    response,
                    thoughts,
                    job_id=job_id,
                    thread_id=thread_id,
                    memory_command=memory_command,
                )
            )
        except Exception as exc:
            logging.error(
                "Interactive generation failed for job %s (%s).",
                job_id,
                type(exc).__name__,
            )
            user_message = getattr(exc, "user_message", "Generation failed. Please try again.")
            error_details = getattr(exc, "error_details", type(exc).__name__)
            self.finished.emit(
                GenerationResult.failed(
                    user_message,
                    error_details=error_details,
                    job_id=job_id,
                    thread_id=thread_id,
                )
            )


class GenerationJobController(QObject):
    """Own at most one interactive QThread/QueryWorker pair at a time."""

    status_updated = Signal(str, str)  # status text, job id
    finished = Signal(object)  # GenerationResult
    active_changed = Signal(bool)

    def __init__(self, orchestrator):
        super().__init__()
        self.orchestrator = orchestrator
        self._thread: QThread | None = None
        self._worker: QueryWorker | None = None
        self._active_job_id: str | None = None
        self._shutting_down = False

    @property
    def active_job_id(self) -> str | None:
        return self._active_job_id

    def is_active(self) -> bool:
        return self._active_job_id is not None

    def accepts(self, result: GenerationResult) -> bool:
        """Return whether a callback still belongs to the active job."""
        return bool(
            not self._shutting_down
            and result.job_id
            and result.job_id == self._active_job_id
        )

    def start(self, snapshot: GenerationSnapshot) -> bool:
        """Start a job, rejecting a second interactive generation."""
        if self._shutting_down or self.is_active():
            logging.warning("Rejected generation request while another job is active.")
            return False

        if not snapshot.job_id:
            logging.error("Rejected generation request without a job id.")
            return False
        self._active_job_id = snapshot.job_id
        self._thread = QThread()
        self._worker = QueryWorker(self.orchestrator, snapshot)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.status_updated.connect(self.status_updated)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()
        self.active_changed.emit(True)
        return True

    def _on_worker_finished(self, result: GenerationResult) -> None:
        if not self.accepts(result):
            logging.warning("Ignoring stale generation callback for job %s.", result.job_id)
            return
        self.finished.emit(result)

    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._active_job_id = None
        self.active_changed.emit(False)

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Request worker interruption and wait for the thread to finish."""
        self._shutting_down = True
        thread = self._thread
        if thread is None:
            self._active_job_id = None
            return

        try:
            if thread.isRunning():
                thread.requestInterruption()
                thread.quit()
                if not thread.wait(timeout_ms):
                    logging.warning("Generation worker did not stop within %sms.", timeout_ms)
        except RuntimeError:
            logging.debug("Generation worker was already deleted during shutdown.")
        finally:
            self._thread = None
            self._worker = None
            self._active_job_id = None
            self.active_changed.emit(False)
