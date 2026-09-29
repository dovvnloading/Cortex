"""Child-process lifecycle shared by the local worker attempts.

Every local capability -- safe computation, approval-gated code, and the fixed
image recipe -- runs its work in a short-lived child and has to be able to stop
that child without leaving it behind. These helpers are that contract, kept
apart from any one capability so all three answer cancellation and containment
the same way.

The parent side (``_contain_worker``, ``_release_worker``, ``_stop_process``)
puts the child in a job object and ends it. The child side
(``scrub_worker_environment``, ``apply_resource_limits``,
``announce_ready_and_wait``) is what each worker entry point runs before it
touches its input. ``code_execution`` keeps its own copies of the first two so
that worker still imports nothing from its siblings; a test holds the copies
to the same behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import os
from typing import Any

from cortex_backend.core.win_jobs import JobLimits, KillOnCloseJob

DEFAULT_CANCEL_GRACE_SECONDS = 0.35

_LOGGER = logging.getLogger("cortex.execution.local_process")


def _process_is_alive(process: Any) -> bool:
    try:
        return bool(process.is_alive())
    except Exception:
        return False


def _contain_worker(process: Any, limits: JobLimits | None = None) -> KillOnCloseJob | None:
    """Put a started worker in a job that ends with this process.

    Without it a worker outlives a Cortex that dies -- Task Manager, a crash --
    and keeps its resources until its own limits run out. ``limits`` adds the
    memory, process-count and user-interface ceilings the worker runs under.
    The caller closes the returned job once the worker is finished with.
    Raises ``JobObjectError`` if the worker cannot be contained, and the caller
    must not let it carry on uncontained. Windows only: elsewhere there is
    nothing to do and ``None`` is returned.

    The worker has been running since ``start()`` returned, so anything it
    spawned in that instant is outside the job. The worker entry points close
    that gap themselves: they do nothing with their input until
    ``_release_worker`` tells them the job is attached, and they spawn nothing
    before then.
    """

    if os.name != "nt":
        return None
    job = KillOnCloseJob(limits)
    try:
        job.assign(int(process.pid))
    except BaseException:
        job.close()
        raise
    return job


def _release_worker(connection: Any) -> bool:
    """Tell a worker waiting at its checkpoint that it is contained and may go.

    ``False`` means the worker is gone or its pipe is broken, and the caller
    should report a failed worker rather than wait for a result.
    """

    try:
        connection.send({"go": True})
    except (OSError, ValueError):
        return False
    return True


def announce_ready_and_wait(connection: Any) -> bool:
    """The worker's half of the checkpoint: say ready, then wait to be released.

    A worker calls this after its own bootstrap (imports, environment scrub)
    and before it reads, decodes or evaluates anything it was given. That
    input is what the job's limits exist for, and until the parent confirms
    the job is attached it would run outside them. ``False`` means the parent
    went away or refused to release the worker, and the worker must return
    without doing its work.
    """

    connection.send({"ok": True, "event": "ready"})
    try:
        go = connection.recv()
    except (EOFError, OSError):
        return False
    return isinstance(go, Mapping) and go.get("go") is True


def minimal_worker_environment() -> dict[str, str]:
    """The only environment a worker, or anything it starts, is left with.

    ``SystemRoot`` on Windows, because most Windows binaries (Python's own
    random-number start-up included) cannot run without it, and nothing
    elsewhere.
    """

    system_root = os.environ.get("SystemRoot") if os.name == "nt" else None
    return {"SystemRoot": system_root} if system_root else {}


def scrub_worker_environment() -> None:
    """Drop the inherited credentials and proxy settings from this process.

    Windows ``spawn`` hands a child its parent's whole environment block, so
    this is a clearing step inside the worker, not a launch with a clean
    environment: it has to run before the worker does anything else.
    """

    kept = minimal_worker_environment()
    os.environ.clear()
    os.environ.update(kept)


def apply_resource_limits(*, memory_bytes: int, cpu_seconds: int | None = None) -> None:
    """Apply portable best-effort limits to this worker before it does its work.

    POSIX only (address space, and CPU time when given). Windows has no
    equivalent inside the process: its memory, process-count and CPU ceilings
    are the job object the parent attaches, which is why the worker waits for
    the go-ahead.
    """

    try:
        import resource  # Unix only; unavailable on the Windows desktop build.

        # POSIX-only; the ImportError below is the Windows path.
        resource.setrlimit(  # type: ignore[attr-defined]
            resource.RLIMIT_AS,  # type: ignore[attr-defined]
            (memory_bytes, memory_bytes),
        )
        if cpu_seconds is not None:
            resource.setrlimit(  # type: ignore[attr-defined]
                resource.RLIMIT_CPU,  # type: ignore[attr-defined]
                (cpu_seconds, cpu_seconds + 1),
            )
    except (ImportError, OSError, ValueError):
        return


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
    "announce_ready_and_wait",
    "apply_resource_limits",
    "minimal_worker_environment",
    "scrub_worker_environment",
]
