"""The local worker attempt behind ``recipe.image.v1``.

Runs the fixed image provider in a short-lived child process. The child is
handed immutable bytes and an already-validated plan -- never a path, a
command, or model source -- and returns compact validated output.
"""

from __future__ import annotations

from collections.abc import Mapping
import multiprocessing
from threading import Event, Lock
import time
from typing import Any

from .lifecycle import RuntimeHealth
from .local_process import (
    DEFAULT_CANCEL_GRACE_SECONDS,
    _stop_process,
)
from .models import ExecutionJob
from .recipe_coordinator import RecipeExecutionError, RecipeWorkerOutput
from .recipe_provider import RecipeImageProvider, RecipeProviderError
from .recipes import RecipeValidationError, parse_image_transform

DEFAULT_IMAGE_TIMEOUT_SECONDS = 45.0

_RECIPE_PROCESS_ERROR = "worker_provider_failed"


def _recipe_worker_main(
    connection: Any,
    cancel_event: Any,
    plan_payload: Mapping[str, Any],
    content: bytes,
) -> None:
    """Run only the fixed provider in a child process and return bytes/metadata."""

    try:
        provider = RecipeImageProvider()
        health = provider.start(
            RuntimeHealth.ready("The local image worker dependency check passed.")
        )
        if not health.available:
            connection.send({"ok": False, "code": _RECIPE_PROCESS_ERROR})
            return
        plan = parse_image_transform(plan_payload)
        result = provider.transform(
            plan,
            content,
            cancel_check=lambda: bool(cancel_event.is_set()),
        )
        connection.send(
            {
                "ok": True,
                "content": result.content,
                "mime_type": result.mime_type,
                "format": result.format,
                "width": result.width,
                "height": result.height,
                "sha256": result.sha256,
            }
        )
    except (RecipeProviderError, RecipeValidationError):
        try:
            connection.send({"ok": False, "code": _RECIPE_PROCESS_ERROR})
        except Exception:
            pass
    except Exception:
        try:
            connection.send({"ok": False, "code": "worker_failed"})
        except Exception:
            pass
    finally:
        try:
            connection.close()
        except Exception:
            pass


class LocalRecipeWorkerAttempt:
    """A cancellable, short-lived local process wrapper for the fixed recipe."""

    def __init__(
        self,
        _job: ExecutionJob,
        *,
        timeout_seconds: float = DEFAULT_IMAGE_TIMEOUT_SECONDS,
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
    ) -> None:
        if timeout_seconds <= 0 or cancel_grace_seconds <= 0:
            raise ValueError("worker timeouts must be positive")
        self._context = multiprocessing.get_context("spawn")
        self._cancel_event = self._context.Event()
        self._timeout_seconds = float(timeout_seconds)
        self._cancel_grace_seconds = float(cancel_grace_seconds)
        self._lock = Lock()
        self._process: Any | None = None
        self._closed = False

    def transform(
        self,
        _request_id: str,
        job_id: str,
        plan: Any,
        content: bytes,
        cancel_event: Event,
    ) -> RecipeWorkerOutput:
        if self._closed:
            raise RecipeExecutionError("worker_closed")
        if cancel_event.is_set() or self._cancel_event.is_set():
            raise RecipeExecutionError("cancelled")
        try:
            plan_payload = plan.model_dump(mode="json")
        except Exception:
            raise RecipeExecutionError("worker_plan_invalid") from None
        receiver = sender = process = None
        try:
            receiver, sender = self._context.Pipe(duplex=False)
            process = self._context.Process(
                target=_recipe_worker_main,
                args=(sender, self._cancel_event, plan_payload, content),
                name=f"cortex-image-{job_id}",
                daemon=True,
            )
            with self._lock:
                if self._closed:
                    raise RecipeExecutionError("worker_closed")
                self._process = process
            process.start()
            sender.close()
            sender = None
            deadline = time.monotonic() + self._timeout_seconds
            cancelled_at: float | None = None
            while True:
                if cancel_event.is_set() or self._cancel_event.is_set():
                    self._cancel_event.set()
                    cancelled_at = cancelled_at or time.monotonic()
                if receiver.poll(0.025):
                    try:
                        message = receiver.recv()
                    except (EOFError, OSError):
                        raise RecipeExecutionError("worker_failed") from None
                    return self._output_from_message(message)
                now = time.monotonic()
                if cancelled_at is not None and now - cancelled_at >= self._cancel_grace_seconds:
                    raise RecipeExecutionError("cancelled")
                if now >= deadline:
                    raise RecipeExecutionError("worker_timeout")
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

    @staticmethod
    def _output_from_message(message: object) -> RecipeWorkerOutput:
        if not isinstance(message, Mapping):
            raise RecipeExecutionError("worker_output_invalid")
        if message.get("ok") is not True:
            code = message.get("code")
            if code == "cancelled":
                raise RecipeExecutionError("cancelled")
            raise RecipeExecutionError(_RECIPE_PROCESS_ERROR)
        try:
            return RecipeWorkerOutput(
                content=message["content"],
                mime_type=message["mime_type"],
                format=message["format"],
                width=message["width"],
                height=message["height"],
                sha256=message["sha256"],
            )
        except (KeyError, TypeError, RecipeExecutionError):
            raise RecipeExecutionError("worker_output_invalid") from None

    def cancel(self, _reason: str = "user") -> None:
        self._cancel_event.set()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process = self._process
        self._cancel_event.set()
        if process is not None:
            _stop_process(process, grace_seconds=self._cancel_grace_seconds)


__all__ = [
    "DEFAULT_IMAGE_TIMEOUT_SECONDS",
    "LocalRecipeWorkerAttempt",
]
