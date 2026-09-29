"""Worker entry points that misbehave in one specific way.

The attempts start their worker by importing its entry point in a fresh child
(``spawn``), so a test double for the child has to live in an importable module
rather than inside a test function. Each function here is shaped like the real
entry point it stands in for and does one deliberate thing the real one never
does: answer before it is released, never answer, drop a marker file the moment
it is allowed to run, start late, or report what it inherited.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any

from cortex_backend.execution.local_process import announce_ready_and_wait, scrub_worker_environment


def scratch_answers_without_being_released(connection: Any, cancel_event: Any, expression: str) -> None:
    """A scratch worker that returns a result without waiting at the checkpoint."""

    connection.send({"ok": True, "value": "1"})
    connection.close()


def recipe_answers_without_being_released(
    connection: Any, cancel_event: Any, plan_payload: Any, content: bytes
) -> None:
    """A recipe worker that returns an output without waiting at the checkpoint."""

    connection.send(
        {
            "ok": True,
            "content": b"x",
            "mime_type": "image/png",
            "format": "PNG",
            "width": 1,
            "height": 1,
            "sha256": "0" * 64,
        }
    )
    connection.close()


def code_answers_without_being_released(
    connection: Any, source: str, capabilities: Any, workspace: str
) -> None:
    """A code worker that returns a result without waiting at the checkpoint."""

    connection.send({"ok": True, "result": {"schema_version": "code.result.v1"}})
    connection.close()


def scratch_never_answers(connection: Any, cancel_event: Any, expression: str) -> None:
    """A scratch worker that never gets as far as saying it is ready."""

    time.sleep(60)


def recipe_never_answers(connection: Any, cancel_event: Any, plan_payload: Any, content: bytes) -> None:
    """A recipe worker that never gets as far as saying it is ready."""

    time.sleep(60)


def scratch_marks_the_moment_it_is_released(connection: Any, cancel_event: Any, marker: str) -> None:
    """Scratch-shaped: writes ``marker`` (the expression argument) once released."""

    if announce_ready_and_wait(connection):
        Path(marker).write_text("released", encoding="utf-8")
        connection.send({"ok": True, "value": "1"})
    connection.close()


def recipe_marks_the_moment_it_is_released(
    connection: Any, cancel_event: Any, plan_payload: Any, content: bytes
) -> None:
    """Recipe-shaped: writes the path in ``plan_payload["marker"]`` once released."""

    if announce_ready_and_wait(connection):
        Path(plan_payload["marker"]).write_text("released", encoding="utf-8")
    connection.close()


def recipe_starts_late(
    connection: Any, cancel_event: Any, plan_payload: Any, content: bytes, delay: float
) -> None:
    """The real recipe worker, after a slow cold start of ``delay`` seconds."""

    from cortex_backend.execution import local_recipe_attempt

    time.sleep(delay)
    local_recipe_attempt._recipe_worker_main(connection, cancel_event, plan_payload, content)


def report_scrubbed_environment(connection: Any, secret_name: str) -> None:
    """Report whether a variable was inherited, then whether the scrub removed it."""

    inherited = secret_name in os.environ
    scrub_worker_environment()
    connection.send({"inherited": inherited, "remaining": sorted(os.environ)})
    connection.close()


def hold_until_told_to_finish(connection: Any) -> None:
    """Say it is up, then wait for the parent to say it may finish."""

    connection.send("up")
    try:
        connection.recv()
    except (EOFError, OSError):
        pass
    connection.close()
