"""Child-process lifecycle shared by the local worker attempts.

Every local capability -- safe computation, approval-gated code, and the fixed
image recipe -- runs its work in a short-lived child and has to be able to stop
that child without leaving it behind. These helpers are that contract, kept
apart from any one capability so all three answer cancellation the same way.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from cortex_backend.core.win_jobs import KillOnCloseJob

DEFAULT_CANCEL_GRACE_SECONDS = 0.35

_LOGGER = logging.getLogger("cortex.execution.local_process")


def _process_is_alive(process: Any) -> bool:
    try:
        return bool(process.is_alive())
    except Exception:
        return False


def _contain_worker(process: Any) -> KillOnCloseJob | None:
    """Put a freshly started worker in a job that ends with this process.

    Without it a worker outlives a Cortex that dies -- Task Manager, a crash --
    and keeps its resources until its own limits run out. The caller closes the
    returned job once the worker is finished with. Raises ``JobObjectError`` if
    the worker cannot be contained, and the caller must not let it carry on
    uncontained. Windows only: elsewhere there is nothing to do and ``None`` is
    returned.

    The worker has been running since ``start()`` returned, so anything it
    spawned in that instant is outside the job; the workers spawn nothing.
    """

    if os.name != "nt":
        return None
    job = KillOnCloseJob()
    try:
        job.assign(int(process.pid))
    except BaseException:
        job.close()
        raise
    return job


def _stop_process(process: Any, *, grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS) -> None:
    """Bounded clean-up for a worker that may have stopped responding.

    Every step is best-effort by design: teardown must finish even when the
    process object is in a bad state. Best-effort is not the same as silent,
    though. A step that always fails is a worker path that always leaks a
    sandboxed child, and with nothing recorded that is indistinguishable from
    a worker which simply never produced a result. One flaky teardown is
    ordinary, hence debug; a child that survives the whole ladder is not, hence
    the warning at the end.
    """

    grace = max(0.0, grace_seconds)
    try:
        process.join(timeout=grace)
    except Exception:
        _LOGGER.debug("Worker join during teardown failed.", exc_info=True)
    if not _process_is_alive(process):
        return
    try:
        process.terminate()
    except Exception:
        _LOGGER.debug("Worker terminate during teardown failed.", exc_info=True)
    try:
        process.join(timeout=grace)
    except Exception:
        _LOGGER.debug("Worker join after terminate failed.", exc_info=True)
    if not _process_is_alive(process):
        return
    # A worker that survives terminate() would otherwise be leaked while still
    # holding its sandbox resources, so escalate to an unconditional kill.
    try:
        process.kill()
    except Exception:
        _LOGGER.warning(
            "A local execution worker could not be killed; it may still hold "
            "its sandbox resources."
        )
        return
    try:
        process.join(timeout=grace)
    except Exception:
        _LOGGER.debug("Worker join after kill failed.", exc_info=True)
    if _process_is_alive(process):
        _LOGGER.warning(
            "A local execution worker survived terminate and kill; it may still "
            "hold its sandbox resources."
        )


__all__ = [
    "DEFAULT_CANCEL_GRACE_SECONDS",
]
