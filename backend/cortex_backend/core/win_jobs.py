"""Windows Job Objects that keep child processes from outliving Cortex.

A job object created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` terminates
every process assigned to it when the last handle to the job closes. Cortex
holds that handle, so when it exits for any reason -- an orderly stop, Task
Manager, a crash -- Windows closes the handle with the process and reaps the
children, which nothing else on the machine would do.

The definitions here are the stable, documented Win32 surface for that, kept
in one place so the llama-server launcher, the dev-server supervisor and the
execution workers share them.

A job can also carry :class:`JobLimits`: a ceiling on committed memory (per
process and for the job as a whole), on the number of live processes, and on
CPU time, plus the user-interface restrictions that stop a contained process
reaching the clipboard, other windows' handles, the desktop or the display
settings. The memory limits are commit-charge limits, not working-set limits:
an allocation that would exceed them fails inside the process.

What this does *not* contain: a process that is assigned only after it has
started may already have spawned children of its own, and those are outside
the job (the execution workers hold their child at a checkpoint until the job
is attached for exactly that reason). A job says nothing about files or the
network: a contained process can still read and write whatever its user can,
and open sockets. It is a resource and lifetime boundary, not a sandbox.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import logging
import math
from typing import Any, Protocol
from collections.abc import Callable

logger = logging.getLogger(__name__)

JOBOBJECT_BASIC_UI_RESTRICTIONS_CLASS = 4
JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
# Every JOB_OBJECT_UILIMIT_* flag: handles, both clipboard directions, system
# parameters, display settings, global atoms, the desktop and exit-windows.
JOB_OBJECT_UILIMIT_ALL = 0x000000FF
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class JobObjectError(RuntimeError):
    """Raised when a process cannot be contained by a Job Object."""


class JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time", ctypes.c_int64),
        ("per_job_user_time", ctypes.c_int64),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class JobObjectIoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_uint64),
        ("write_operation_count", ctypes.c_uint64),
        ("other_operation_count", ctypes.c_uint64),
        ("read_transfer_count", ctypes.c_uint64),
        ("write_transfer_count", ctypes.c_uint64),
        ("other_transfer_count", ctypes.c_uint64),
    ]


class JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", JobObjectBasicLimitInformation),
        ("io_info", JobObjectIoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class JobBasicUiRestrictions(ctypes.Structure):
    _fields_ = [("ui_restrictions_class", wintypes.DWORD)]


@dataclass(frozen=True)
class JobLimits:
    """What a job enforces beyond kill-on-close. ``None`` leaves a limit off.

    ``process_memory_bytes`` caps the committed memory of each process in the
    job and ``job_memory_bytes`` the sum over the job. ``active_processes``
    caps how many processes may be alive in it at once, the assigned one
    included: creating a process past the cap fails (``ERROR_NOT_ENOUGH_QUOTA``
    from ``CreateProcess``, as the real-job tests observe). ``cpu_seconds``
    is per-process user-mode CPU time. ``restrict_ui`` applies every
    ``JOB_OBJECT_UILIMIT_*`` restriction.
    """

    process_memory_bytes: int | None = None
    job_memory_bytes: int | None = None
    active_processes: int | None = None
    cpu_seconds: float | None = None
    restrict_ui: bool = False

    def __post_init__(self) -> None:
        for name in ("process_memory_bytes", "job_memory_bytes", "active_processes"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} must be a positive integer")
        cpu_seconds = self.cpu_seconds
        if cpu_seconds is not None and (
            isinstance(cpu_seconds, bool)
            or not isinstance(cpu_seconds, (int, float))
            or not math.isfinite(cpu_seconds)
            or cpu_seconds <= 0
        ):
            raise ValueError("cpu_seconds must be a positive number")


class JobWin32(Protocol):
    """The handful of kernel32 entry points needed to assign a kill-on-close
    Job Object -- small and injectable so tests can verify the exact call
    sequence without touching real Windows APIs or spawning a real process."""

    def CreateJobObjectW(self, security_attributes: Any, name: Any) -> int: ...
    def SetInformationJobObject(self, job: int, info_class: int, info: Any, info_size: int) -> int: ...
    def OpenProcess(self, access: int, inherit_handle: int, pid: int) -> int: ...
    def AssignProcessToJobObject(self, job: int, process: int) -> int: ...
    def CloseHandle(self, handle: int) -> int: ...


def real_job_win32() -> JobWin32:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _extended_limits(limits: JobLimits) -> JobObjectExtendedLimitInformation:
    """The extended limit block for kill-on-close plus whatever ``limits`` sets."""

    info = JobObjectExtendedLimitInformation()
    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if limits.process_memory_bytes is not None:
        flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.process_memory_limit = limits.process_memory_bytes
    if limits.job_memory_bytes is not None:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        info.job_memory_limit = limits.job_memory_bytes
    if limits.active_processes is not None:
        flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.basic_limit_information.active_process_limit = limits.active_processes
    if limits.cpu_seconds is not None:
        flags |= JOB_OBJECT_LIMIT_PROCESS_TIME
        # 100-nanosecond units.
        info.basic_limit_information.per_process_user_time = int(limits.cpu_seconds * 10_000_000)
    info.basic_limit_information.limit_flags = flags
    return info


class KillOnCloseJob:
    """A job object whose processes die when it is closed or Cortex exits.

    Creating the object creates and configures the job (kill-on-close, and any
    :class:`JobLimits`); :meth:`assign` puts a running process in it;
    :meth:`close` closes the handle, which ends every process still assigned.
    The owner keeps the object for as long as its processes should live -- if
    it is never closed, the handle goes when the process does, which is the
    point.
    """

    def __init__(
        self,
        limits: JobLimits | None = None,
        *,
        win32_factory: Callable[[], JobWin32] = real_job_win32,
    ) -> None:
        self._win32: JobWin32 | None = None
        self._handle: int | None = None
        wanted = limits if limits is not None else JobLimits()
        try:
            win32 = win32_factory()
            handle = win32.CreateJobObjectW(None, None)
            if not handle:
                raise JobObjectError("could not create a process containment job")
            info = _extended_limits(wanted)
            configured = bool(
                win32.SetInformationJobObject(
                    handle,
                    JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
            )
            if configured and wanted.restrict_ui:
                restrictions = JobBasicUiRestrictions(JOB_OBJECT_UILIMIT_ALL)
                configured = bool(
                    win32.SetInformationJobObject(
                        handle,
                        JOBOBJECT_BASIC_UI_RESTRICTIONS_CLASS,
                        ctypes.byref(restrictions),
                        ctypes.sizeof(restrictions),
                    )
                )
            if not configured:
                win32.CloseHandle(handle)
                raise JobObjectError("could not configure the process containment job")
        except JobObjectError:
            raise
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise JobObjectError("could not initialize process containment") from exc
        self._win32 = win32
        self._handle = handle

    @property
    def closed(self) -> bool:
        return self._handle is None

    def assign(self, pid: int) -> None:
        """Put the running process ``pid`` in the job, or raise ``JobObjectError``."""
        win32, job = self._win32, self._handle
        if win32 is None or job is None:
            raise JobObjectError("the process containment job is closed")
        try:
            process = win32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
            if not process:
                raise JobObjectError("could not open the process for containment")
            try:
                assigned = bool(win32.AssignProcessToJobObject(job, process))
            finally:
                # Only needed to make the assignment. A failure to close it
                # leaks a handle but leaves the process contained.
                if not win32.CloseHandle(process):
                    logger.debug("Could not close a process containment handle.")
        except JobObjectError:
            raise
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise JobObjectError("could not assign the process to containment") from exc
        if not assigned:
            raise JobObjectError("could not assign the process to containment")

    def close(self) -> None:
        """Close the job, ending every process still in it. Safe to repeat."""
        win32, job = self._win32, self._handle
        self._win32 = self._handle = None
        if win32 is None or job is None:
            return
        try:
            if not win32.CloseHandle(job):
                logger.warning("Could not close a process containment job; it closes when Cortex exits.")
        except OSError:
            logger.warning("Could not close a process containment job; it closes when Cortex exits.")


__all__ = [
    "JOBOBJECT_BASIC_UI_RESTRICTIONS_CLASS",
    "JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS",
    "JOB_OBJECT_LIMIT_ACTIVE_PROCESS",
    "JOB_OBJECT_LIMIT_JOB_MEMORY",
    "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
    "JOB_OBJECT_LIMIT_PROCESS_MEMORY",
    "JOB_OBJECT_LIMIT_PROCESS_TIME",
    "JOB_OBJECT_UILIMIT_ALL",
    "PROCESS_SET_QUOTA",
    "PROCESS_TERMINATE",
    "JobBasicUiRestrictions",
    "JobLimits",
    "JobObjectBasicLimitInformation",
    "JobObjectError",
    "JobObjectExtendedLimitInformation",
    "JobObjectIoCounters",
    "JobWin32",
    "KillOnCloseJob",
    "real_job_win32",
]
