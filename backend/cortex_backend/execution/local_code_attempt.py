"""The local worker attempt behind ``code.exec.v1``.

Runs one validated, user-approved program in a short-lived child process under
a memory cap, an output cap, and a wall-clock limit. On Windows the child is
placed in a job object so its own descendants cannot outlive it.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import multiprocessing
import os
from threading import Event, Lock
import time
from typing import Any

from .code_execution import (
    CODE_EXECUTION_RESULT_SCHEMA,
    CodeCapabilities,
    CodeExecutionError,
    CodeExecutionResult,
    MAX_CODE_MEMORY_BYTES,
    MAX_CODE_OUTPUT_BYTES,
    MAX_CODE_TIMEOUT_SECONDS,
    MAX_CODE_VALUE_BYTES,
    _WindowsProcessJob,
    code_worker_main,
    validate_code_source,
)
from .local_process import (
    DEFAULT_CANCEL_GRACE_SECONDS,
    _stop_process,
)

DEFAULT_CODE_TIMEOUT_SECONDS = MAX_CODE_TIMEOUT_SECONDS
# Importing a frozen desktop process can take longer than evaluating a small
# program. Keep that bootstrap grace separate from the code's wall-clock limit
# so a healthy worker is not reported as a code timeout while it is starting.
DEFAULT_CODE_STARTUP_TIMEOUT_SECONDS = 15.0


class LocalCodeAttempt:
    """Run one validated code request in a short-lived child process."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_CODE_TIMEOUT_SECONDS,
        startup_timeout_seconds: float = DEFAULT_CODE_STARTUP_TIMEOUT_SECONDS,
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
    ) -> None:
        if timeout_seconds <= 0 or startup_timeout_seconds <= 0 or cancel_grace_seconds <= 0:
            raise ValueError("worker timeouts must be positive")
        self._context = multiprocessing.get_context("spawn")
        self._cancel_event = self._context.Event()
        self._timeout_seconds = float(timeout_seconds)
        self._startup_timeout_seconds = float(startup_timeout_seconds)
        self._cancel_grace_seconds = float(cancel_grace_seconds)
        self._lock = Lock()
        self._process: Any | None = None
        self._closed = False

    def evaluate(
        self,
        source: str,
        capabilities: CodeCapabilities,
        workspace: str,
        cancel_event: Event,
    ) -> CodeExecutionResult:
        if self._closed:
            raise CodeExecutionError("worker_closed")
        validate_code_source(source)
        receiver = sender = process = None
        worker_job: _WindowsProcessJob | None = None
        try:
            # Duplex, unlike the scratch/recipe workers: the child must be
            # held at its "ready" checkpoint (after environment scrubbing,
            # before running any of the source) until this end confirms the
            # Job Object is attached, so job_sent below can tell it to go.
            receiver, sender = self._context.Pipe()
            process = self._context.Process(
                target=code_worker_main,
                args=(sender, source, capabilities.as_dict(), workspace),
                name="cortex-code",
                daemon=True,
            )
            with self._lock:
                if self._closed:
                    raise CodeExecutionError("worker_closed")
                self._process = process
            process.start()
            sender.close()
            sender = None
            startup_deadline = time.monotonic() + self._startup_timeout_seconds
            deadline: float | None = None
            cancelled_at: float | None = None
            job_sent = False
            while True:
                if cancel_event.is_set() or self._cancel_event.is_set():
                    self._cancel_event.set()
                    cancelled_at = cancelled_at or time.monotonic()
                if receiver.poll(0.025):
                    try:
                        message = receiver.recv()
                    except (EOFError, OSError):
                        raise CodeExecutionError("worker_failed") from None
                    if (
                        isinstance(message, Mapping)
                        and message.get("ok") is True
                        and message.get("event") == "ready"
                    ):
                        if not job_sent:
                            job_sent = True
                            if os.name == "nt":
                                worker_job = _WindowsProcessJob(
                                    process,
                                    memory_limit=MAX_CODE_MEMORY_BYTES,
                                    active_process_limit=4,
                                    cpu_seconds=self._timeout_seconds + 1.0,
                                )
                            receiver.send({"go": True})
                        deadline = time.monotonic() + self._timeout_seconds
                        continue
                    return self._result_from_message(message)
                now = time.monotonic()
                if cancelled_at is not None and now - cancelled_at >= self._cancel_grace_seconds:
                    raise CodeExecutionError("cancelled")
                if deadline is None and now >= startup_deadline:
                    raise CodeExecutionError("worker_startup_timeout")
                if deadline is not None and now >= deadline:
                    raise CodeExecutionError("worker_timeout")
        finally:
            if sender is not None:
                try:
                    sender.close()
                except Exception:
                    pass
            if receiver is not None:
                try:
                    receiver.close()
                except Exception:
                    pass
            if worker_job is not None:
                worker_job.close()
            if process is not None:
                _stop_process(process, grace_seconds=self._cancel_grace_seconds)
            with self._lock:
                if self._process is process:
                    self._process = None

    @staticmethod
    def _result_from_message(message: object) -> CodeExecutionResult:
        if not isinstance(message, Mapping) or message.get("ok") is not True:
            code = message.get("code") if isinstance(message, Mapping) else None
            if not isinstance(code, str) or not code.isidentifier() or len(code) > 64:
                code = "worker_failed"
            raise CodeExecutionError(code)
        result = message.get("result")
        if not isinstance(result, Mapping) or result.get("schema_version") != CODE_EXECUTION_RESULT_SCHEMA:
            raise CodeExecutionError("worker_output_invalid")
        try:
            stdout = result.get("stdout")
            stderr = result.get("stderr")
            truncated = result.get("truncated")
            duration_ms = result.get("duration_ms")
            if not isinstance(stdout, str) or not isinstance(stderr, str):
                raise CodeExecutionError("worker_output_invalid")
            if len(stdout.encode("utf-8", errors="replace")) > MAX_CODE_OUTPUT_BYTES or len(stderr.encode("utf-8", errors="replace")) > MAX_CODE_OUTPUT_BYTES:
                raise CodeExecutionError("worker_output_invalid")
            if type(truncated) is not bool or type(duration_ms) is not int or not 0 <= duration_ms <= int(MAX_CODE_TIMEOUT_SECONDS * 1_000):
                raise CodeExecutionError("worker_output_invalid")
            value = result.get("value")
            if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_CODE_VALUE_BYTES:
                raise CodeExecutionError("worker_output_invalid")
            return CodeExecutionResult(
                stdout=stdout,
                stderr=stderr,
                value=value,
                truncated=truncated,
                duration_ms=duration_ms,
            )
        except (CodeExecutionError, TypeError, ValueError, OverflowError):
            raise CodeExecutionError("worker_output_invalid") from None

    def cancel(self) -> None:
        self._cancel_event.set()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process = self._process
        self._cancel_event.set()
        if process is not None:
            _stop_process(process, grace_seconds=self._cancel_grace_seconds)


__all__ = [
    "DEFAULT_CODE_STARTUP_TIMEOUT_SECONDS",
    "DEFAULT_CODE_TIMEOUT_SECONDS",
    "LocalCodeAttempt",
]
