"""The local worker attempt behind ``recipe.image.v1``.

Runs the fixed image provider in a short-lived child process. The child is
handed immutable bytes and an already-validated plan -- never a path, a
command, or model source -- and returns compact validated output.

The child imports the imaging stack, then holds at a checkpoint until the parent
has put it in a job object with memory and process-count limits. Only then does
it look at the untrusted bytes. The startup wait and the transform budget are
separate, so a slow cold start is not reported as a slow image.
"""

from __future__ import annotations

from collections.abc import Mapping
import multiprocessing
from threading import Event, Lock
import time
from typing import Any

from cortex_backend.core.win_jobs import JobLimits, JobObjectError, KillOnCloseJob

from .local_process import (
    DEFAULT_CANCEL_GRACE_SECONDS,
    _contain_worker,
    _release_worker,
    _stop_process,
    announce_ready_and_wait,
    apply_resource_limits,
    scrub_worker_environment,
)
from .models import ExecutionJob
from .recipe_coordinator import RecipeExecutionError, RecipeWorkerOutput
from .recipe_provider import (
    RecipeImageProvider,
    RecipeProviderError,
    pin_plugin_registry,
)
from .recipes import RecipeValidationError, parse_image_transform

DEFAULT_IMAGE_TIMEOUT_SECONDS = 45.0
# Importing Pillow and its codecs in a frozen desktop process can take longer
# than transforming a small image, so the child gets its own start-up budget
# before the transform clock starts.
DEFAULT_IMAGE_STARTUP_TIMEOUT_SECONDS = 15.0
# The ceiling is sized from what the real worker was measured to need at the
# provider's own limits, not derived from them. The provider's 256 MiB decode
# budget describes one decoded image; what an operation or an encoder holds on
# top of that (a premultiplied copy, a resampling buffer, encoder tables) is
# not something arithmetic on the limits gets right. The first ceiling here
# came from contrast alone (about 800 MiB at 64 megapixels) and refused
# legitimate 60-64 megapixel resizes and WebP output.
#
# Measured: the real worker, spawned and put in a real job whose limit was
# raised to 12 GiB, reading the job's own peak committed-memory counter after
# the transform. Inputs are synthetic 64-megapixel RGBA images (the provider's
# ceiling) unless noted; Pillow 12.3.0, Python 3.14, Windows 11.
#   contrast or brightness x8, PNG or JPEG out                   about 800 MiB
#   grayscale, crop, rotate or same-size resize, PNG out         about 540 MiB
#   JPEG / lossless WebP input, contrast, PNG out               814 / 1569 MiB
#   LANCZOS resize 8192x8192 -> 16384x4096                            1311 MiB
#   LANCZOS resize 4096x16384 -> 16384x4096                           1825 MiB
#     (source, premultiplied copy, 1 GiB dst_w x src_h buffer, result)
#   the same two resizes from a 98 MiB input                   1410 / 1923 MiB
#   lossless WebP out (method 6), smooth image                 1888 - 1952 MiB
#   lossless WebP out, from a 98 MiB PNG (largest success)     2243 - 2308 MiB
#   lossless WebP out, from full-entropy noise                 2885 - 2942 MiB
# The last row is refused whatever the ceiling (the provider reports
# output_too_large: the result is over the 128 MiB output limit), but it is
# where memory goes highest, so it sets the size: 4 GiB is 39 percent above
# that 2942 MiB, the largest peak of any kind, and 77 percent above 2308 MiB,
# the largest that succeeds. The job still stops a decoder that runs away; this
# caps committed memory and reserves nothing. POSIX applies the same figure as
# an address-space limit, which is best effort and was not measured.
RECIPE_WORKER_MEMORY_BYTES = 4 * 1024 * 1024 * 1024
# The worker decodes untrusted bytes and starts nothing, so the job allows it
# exactly one process.
RECIPE_WORKER_JOB_LIMITS = JobLimits(
    process_memory_bytes=RECIPE_WORKER_MEMORY_BYTES,
    job_memory_bytes=RECIPE_WORKER_MEMORY_BYTES,
    active_processes=1,
    restrict_ui=True,
)

_RECIPE_PROCESS_ERROR = "worker_provider_failed"


def _recipe_worker_main(
    connection: Any,
    cancel_event: Any,
    plan_payload: Mapping[str, Any],
    content: bytes,
) -> None:
    """Run only the fixed provider in a child process and return bytes/metadata."""

    try:
        scrub_worker_environment()
        apply_resource_limits(memory_bytes=RECIPE_WORKER_MEMORY_BYTES)
        provider = RecipeImageProvider()
        health = provider.start()
        if not health.available:
            connection.send({"ok": False, "code": _RECIPE_PROCESS_ERROR})
            return
        # This child handles nothing but the three fixed formats, so freeze
        # the plugin table now that they are loaded. Safe here and only here:
        # the flag is process-global, and the backend process needs the wider
        # set for chat attachments.
        pin_plugin_registry()
        # Everything above is this worker's own bootstrap. Hold here until the
        # parent confirms the job carrying the memory and process-count limits
        # is attached: the plan and the image bytes are read only after that.
        if not announce_ready_and_wait(connection):
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
        startup_timeout_seconds: float = DEFAULT_IMAGE_STARTUP_TIMEOUT_SECONDS,
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
        worker_job: KillOnCloseJob | None = None
        try:
            # Duplex: the worker waits at its "ready" checkpoint for this end's
            # go-ahead, which is only sent once the job is attached.
            receiver, sender = self._context.Pipe()
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
            startup_deadline = time.monotonic() + self._startup_timeout_seconds
            deadline: float | None = None
            cancelled_at: float | None = None
            released = False
            while True:
                if cancel_event.is_set() or self._cancel_event.is_set():
                    self._cancel_event.set()
                    cancelled_at = cancelled_at or time.monotonic()
                if receiver.poll(0.025):
                    try:
                        message = receiver.recv()
                    except (EOFError, OSError):
                        raise RecipeExecutionError("worker_failed") from None
                    if (
                        isinstance(message, Mapping)
                        and message.get("ok") is True
                        and message.get("event") == "ready"
                    ):
                        if not released:
                            released = True
                            try:
                                worker_job = _contain_worker(process, RECIPE_WORKER_JOB_LIMITS)
                            except JobObjectError:
                                raise RecipeExecutionError("process_isolation_unavailable") from None
                            if not _release_worker(receiver):
                                raise RecipeExecutionError("worker_failed")
                            deadline = time.monotonic() + self._timeout_seconds
                        continue
                    if not released and isinstance(message, Mapping) and message.get("ok") is True:
                        # A result from a worker that was never released is not
                        # one this attempt asked for.
                        raise RecipeExecutionError("worker_output_invalid")
                    return self._output_from_message(message)
                now = time.monotonic()
                if cancelled_at is not None and now - cancelled_at >= self._cancel_grace_seconds:
                    raise RecipeExecutionError("cancelled")
                if deadline is None and now >= startup_deadline:
                    raise RecipeExecutionError("worker_startup_timeout")
                if deadline is not None and now >= deadline:
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
            if worker_job is not None:
                worker_job.close()
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
    "DEFAULT_IMAGE_STARTUP_TIMEOUT_SECONDS",
    "DEFAULT_IMAGE_TIMEOUT_SECONDS",
    "LocalRecipeWorkerAttempt",
    "RECIPE_WORKER_JOB_LIMITS",
    "RECIPE_WORKER_MEMORY_BYTES",
]
