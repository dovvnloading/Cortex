"""Ask the operating system, from inside a process, which job object it is in.

A test that only checks the arguments it passed to Windows proves nothing about
what Windows enforced. These probes run in the contained child itself, so what
they report is the job the child really landed in and the limits that job
really carries. The module imports nothing from Cortex on purpose: it has to
load inside a memory-limited child without adding to that child's footprint,
and it must not depend on the code under test to describe it.

Run as a script it waits for one line on stdin (so the parent can attach the
job first) and then prints the description as one JSON line.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import sys
from typing import Any

_EXTENDED_LIMIT_CLASS = 9
_BASIC_UI_RESTRICTIONS_CLASS = 4


class _BasicLimits(ctypes.Structure):
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


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("a", "b", "c", "d", "e", "f")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _UiRestrictions(ctypes.Structure):
    _fields_ = [("ui_restrictions_class", wintypes.DWORD)]


def describe_current_job() -> dict[str, Any]:
    """The limits of the job this process belongs to, as plain numbers."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL

    in_job = wintypes.BOOL()
    if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(in_job)):
        return {"in_job": None}
    if not in_job.value:
        return {"in_job": False}

    extended = _ExtendedLimits()
    ui = _UiRestrictions()
    # A NULL job handle asks about the job the calling process is in.
    if not kernel32.QueryInformationJobObject(
        None, _EXTENDED_LIMIT_CLASS, ctypes.byref(extended), ctypes.sizeof(extended), None
    ) or not kernel32.QueryInformationJobObject(
        None, _BASIC_UI_RESTRICTIONS_CLASS, ctypes.byref(ui), ctypes.sizeof(ui), None
    ):
        return {"in_job": True, "queried": False}
    return {
        "in_job": True,
        "queried": True,
        "limit_flags": int(extended.basic.limit_flags),
        "active_process_limit": int(extended.basic.active_process_limit),
        "per_process_user_time": int(extended.basic.per_process_user_time),
        "process_memory_limit": int(extended.process_memory_limit),
        "job_memory_limit": int(extended.job_memory_limit),
        "ui_restrictions": int(ui.ui_restrictions_class),
    }


def report_job(connection: Any) -> None:
    """A worker-shaped entry point: wait for the go-ahead, then report the job."""

    connection.send({"ok": True, "event": "ready"})
    try:
        go = connection.recv()
    except (EOFError, OSError):
        return
    if not (isinstance(go, dict) and go.get("go") is True):
        return
    connection.send({"ok": True, "job": describe_current_job()})
    connection.close()


if __name__ == "__main__":
    # Wait for one line on stdin so the parent can attach the job first.
    sys.stdin.readline()
    sys.stdout.write(json.dumps(describe_current_job()) + "\n")
