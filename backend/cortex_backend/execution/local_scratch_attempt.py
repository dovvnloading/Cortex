"""The local worker attempt behind ``scratch.auto.v1``.

Evaluates one safe decimal expression in a short-lived child under a hard
wall-clock deadline. The child receives an already-validated expression and
nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping
import multiprocessing
from threading import Event, Lock
import time
from typing import Any

from .local_process import (
    DEFAULT_CANCEL_GRACE_SECONDS,
    _stop_process,
)
from .scratch_compute import (
    ScratchComputeError,
    ScratchComputeResult,
    scratch_worker_main,
)

DEFAULT_SCRATCH_TIMEOUT_SECONDS = 3.0
DEFAULT_SCRATCH_STARTUP_TIMEOUT_SECONDS = 15.0


class LocalScratchAttempt:
    """Spawn the limited expression evaluator with a hard wall-clock deadline."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_SCRATCH_TIMEOUT_SECONDS,
        startup_timeout_seconds: float = DEFAULT_SCRATCH_STARTUP_TIMEOUT_SECONDS,
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

    def evaluate(self, expression: str, cancel_event: Event) -> ScratchComputeResult:
        if self._closed:
            raise ScratchComputeError("worker_closed")
        receiver = sender = process = None
        try:
            receiver, sender = self._context.Pipe(duplex=False)
            process = self._context.Process(
                target=scratch_worker_main,
                args=(sender, self._cancel_event, expression),
                name="cortex-scratch",
                daemon=True,
            )
            with self._lock:
                if self._closed:
                    raise ScratchComputeError("worker_closed")
                self._process = process
            process.start()
            sender.close()
            sender = None
            startup_deadline = time.monotonic() + self._startup_timeout_seconds
            deadline: float | None = None
            cancelled_at: float | None = None
            while True:
                if cancel_event.is_set() or self._cancel_event.is_set():
                    self._cancel_event.set()
                    cancelled_at = cancelled_at or time.monotonic()
                if receiver.poll(0.025):
                    try:
                        message = receiver.recv()
                    except (EOFError, OSError):
                        raise ScratchComputeError("worker_failed") from None
                    if not isinstance(message, Mapping):
                        raise ScratchComputeError("worker_output_invalid")
                    if message.get("ok") is True and message.get("event") == "ready":
                        deadline = time.monotonic() + self._timeout_seconds
                        continue
                    if message.get("ok") is not True:
                        code = message.get("code")
                        raise ScratchComputeError(
                            "cancelled" if code == "cancelled" else "worker_failed"
                        )
                    try:
                        return ScratchComputeResult(value=message["value"])
                    except (KeyError, TypeError, ValueError):
                        raise ScratchComputeError("worker_output_invalid") from None
                now = time.monotonic()
                if cancelled_at is not None and now - cancelled_at >= self._cancel_grace_seconds:
                    raise ScratchComputeError("cancelled")
                if deadline is None and now >= startup_deadline:
                    raise ScratchComputeError("worker_startup_timeout")
                if deadline is not None and now >= deadline:
                    raise ScratchComputeError("worker_timeout")
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
            if process is not None:
                _stop_process(process, grace_seconds=self._cancel_grace_seconds)
            with self._lock:
                if self._process is process:
                    self._process = None

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
    "DEFAULT_SCRATCH_STARTUP_TIMEOUT_SECONDS",
    "DEFAULT_SCRATCH_TIMEOUT_SECONDS",
    "LocalScratchAttempt",
]
