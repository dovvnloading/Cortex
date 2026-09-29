"""The one way a coordinator finishes a job that did not succeed.

Every profile ends an unsuccessful run the same way: it decides whether the
user asked to stop it, and writes ``cancelled`` or ``failed``. That decision is
a read followed by a write, and a Stop can commit between the two. The write
therefore has to be guarded and the decision made again when the guard trips --
otherwise the user asked for ``cancelled`` and is shown ``failed``. The code
profile had that loop and the scratch and recipe profiles did not; the loop
lives here so that no profile, and no capability added later, can drift.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from .models import TerminalExecutionStatus
from .repository import ExecutionRepository, ExecutionTransitionConflict


_LOGGER = logging.getLogger("cortex.execution.finish")

# While a decision is being made a job can move at most running -> cancelling
# -> terminal, so three reads always settle it.
_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class UnsuccessfulJobWording:
    """What one profile calls the two ways a run can end without a result."""

    cancelled_event: str
    failed_event: str
    cancelled_message: str
    failed_message: str


def finish_unsuccessful_job(
    repository: ExecutionRepository,
    job_id: str,
    *,
    failure_code: str,
    cancel_requested: Callable[[], bool],
    wording: UnsuccessfulJobWording,
) -> None:
    """Record ``cancelled`` or ``failed`` for a job, never overriding a committed Stop.

    The outcome is ``cancelled`` when ``failure_code`` says so, when the
    in-process cancel flag is set, or when the job row already reads
    ``cancelling``. The write is guarded by the status the decision was made
    against; if a concurrent actor moved the job first, the decision is made
    again from the new state. A job that is already terminal is left alone.
    Nothing here raises: this runs in a worker's error path, where the original
    outcome is what matters.
    """

    for _ in range(_ATTEMPTS):
        current = repository.get_job(job_id)
        if current is None or current.status in TerminalExecutionStatus:
            return
        cancelled = (
            failure_code == "cancelled"
            or cancel_requested()
            or current.status == "cancelling"
        )
        try:
            repository.transition(
                job_id,
                status="cancelled" if cancelled else "failed",
                event=wording.cancelled_event if cancelled else wording.failed_event,
                phase="cancelled" if cancelled else "failed",
                data={
                    "message": (
                        wording.cancelled_message if cancelled else wording.failed_message
                    )
                },
                error="cancelled" if cancelled else failure_code,
                expected_status=current.status,
            )
            return
        except ExecutionTransitionConflict:
            continue
        except Exception as exc:
            _LOGGER.warning(
                "Cortex could not record how an execution job ended (%s); "
                "it may be left non-terminal.",
                type(exc).__name__,
            )
            return
    _LOGGER.warning(
        "Cortex could not record how an execution job ended after %d attempts; "
        "it may be left non-terminal.",
        _ATTEMPTS,
    )


__all__ = ["UnsuccessfulJobWording", "finish_unsuccessful_job"]
