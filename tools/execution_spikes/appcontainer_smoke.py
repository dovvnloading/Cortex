"""Disposable native AppContainer containment smoke test.

This helper is deliberately kept outside Cortex's production package.  It starts
only fixed Windows executables in a freshly-created zero-capability AppContainer,
proves the process token is really an AppContainer token, and checks that the
child cannot read a parent-owned marker file or connect to a loopback listener.
No model input is accepted and no shell is used.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from uuid import uuid4


PASS = "pass"
FAIL = "fail"
BLOCKED = "blocked"

_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
_JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_TOKEN_IS_APPCONTAINER = 29
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_INFINITE = 0xFFFFFFFF
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000


class _LargeInteger(ctypes.Union):
    _fields_ = [("quad_part", ctypes.c_longlong)]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operations", ctypes.c_ulonglong),
        ("write_operations", ctypes.c_ulonglong),
        ("other_operations", ctypes.c_ulonglong),
        ("read_transfers", ctypes.c_ulonglong),
        ("write_transfers", ctypes.c_ulonglong),
        ("other_transfers", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time", _LargeInteger),
        ("per_job_user_time", _LargeInteger),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _BasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR),
        ("title", wintypes.LPWSTR),
        ("x", wintypes.DWORD),
        ("y", wintypes.DWORD),
        ("x_size", wintypes.DWORD),
        ("y_size", wintypes.DWORD),
        ("x_count_chars", wintypes.DWORD),
        ("y_count_chars", wintypes.DWORD),
        ("fill_attribute", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("show_window", wintypes.WORD),
        ("reserved2", wintypes.WORD),
        ("reserved2_ptr", ctypes.POINTER(ctypes.c_ubyte)),
        ("std_input", wintypes.HANDLE),
        ("std_output", wintypes.HANDLE),
        ("std_error", wintypes.HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [
        ("startup_info", _StartupInfo),
        ("attribute_list", wintypes.LPVOID),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("process", wintypes.HANDLE),
        ("thread", wintypes.HANDLE),
        ("process_id", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
    ]


class _SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("app_container_sid", wintypes.LPVOID),
        ("capabilities", wintypes.LPVOID),
        ("capability_count", wintypes.DWORD),
        ("reserved", wintypes.DWORD),
    ]


def _result(status: str, evidence: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "appcontainer_process_isolation_smoke",
        "status": status,
        "evidence": evidence,
    }
    if details:
        payload["details"] = details
    return payload


def _configure_apis() -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    userenv = ctypes.WinDLL("userenv.dll", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)

    userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    userenv.CreateAppContainerProfile.restype = wintypes.HRESULT
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = wintypes.HRESULT
    advapi32.FreeSid.argtypes = [wintypes.LPVOID]
    advapi32.FreeSid.restype = wintypes.LPVOID
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    kernel32.InitializeProcThreadAttributeList.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_size_t,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.LPVOID,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        ctypes.POINTER(_ProcessInformation),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.OpenProcessToken = advapi32.OpenProcessToken
    kernel32.GetTokenInformation = advapi32.GetTokenInformation
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return (
        kernel32,
        userenv,
        advapi32,
        kernel32.CreateProcessW,
        kernel32.InitializeProcThreadAttributeList,
        kernel32.UpdateProcThreadAttribute,
        kernel32.DeleteProcThreadAttributeList,
    )


def _is_appcontainer(kernel32: Any, process: wintypes.HANDLE) -> bool:
    token = wintypes.HANDLE()
    if not kernel32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        value = wintypes.DWORD()
        returned = wintypes.DWORD()
        if not kernel32.GetTokenInformation(
            token,
            _TOKEN_IS_APPCONTAINER,
            ctypes.byref(value),
            ctypes.sizeof(value),
            ctypes.byref(returned),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(value.value)
    finally:
        kernel32.CloseHandle(token)


def _run_child(
    kernel32: Any,
    userenv: Any,
    advapi32: Any,
    profile_name: str,
    profile_sid: wintypes.LPVOID,
    executable: str,
    arguments: list[str],
    timeout_ms: int = 5000,
    active_process_limit: int = 1,
    process_memory_limit_bytes: int | None = None,
    job_memory_limit_bytes: int | None = None,
    process_user_time_100ns: int | None = None,
    job_user_time_100ns: int | None = None,
) -> dict[str, Any]:
    for name, value in (
        ("process_memory_limit_bytes", process_memory_limit_bytes),
        ("job_memory_limit_bytes", job_memory_limit_bytes),
        ("process_user_time_100ns", process_user_time_100ns),
        ("job_user_time_100ns", job_user_time_100ns),
    ):
        if value is not None and (type(value) is not int or not 1 <= value <= 1024**3):
            raise ValueError(f"{name} is outside the disposable probe bounds")
    if type(active_process_limit) is not int or not 1 <= active_process_limit <= 64:
        raise ValueError("active_process_limit is outside the disposable probe bounds")
    stage = "start"
    command_line = subprocess.list2cmdline([executable, *arguments])
    command_buffer = ctypes.create_unicode_buffer(command_line)
    size = ctypes.c_size_t(0)
    stage = "attribute-size"
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    attribute_buffer = ctypes.create_string_buffer(size.value)
    attribute_list = ctypes.cast(attribute_buffer, wintypes.LPVOID)
    stage = "attribute-init"
    if not kernel32.InitializeProcThreadAttributeList(
        attribute_list, 1, 0, ctypes.byref(size)
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    capabilities = _SecurityCapabilities(
        app_container_sid=profile_sid,
        capabilities=None,
        capability_count=0,
        reserved=0,
    )
    stage = "attribute-security-capabilities"
    if not kernel32.UpdateProcThreadAttribute(
        attribute_list,
        0,
        _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
        ctypes.byref(capabilities),
        ctypes.sizeof(capabilities),
        None,
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    startup = _StartupInfoEx()
    startup.startup_info.cb = ctypes.sizeof(startup)
    startup.attribute_list = attribute_list
    process_info = _ProcessInformation()
    job = wintypes.HANDLE()
    try:
        flags = (
            _EXTENDED_STARTUPINFO_PRESENT
            | _CREATE_SUSPENDED
            | _CREATE_UNICODE_ENVIRONMENT
            | _CREATE_NO_WINDOW
        )
        stage = "create-process"
        if not kernel32.CreateProcessW(
            executable,
            command_buffer,
            None,
            None,
            False,
            flags,
            None,
            None,
            ctypes.byref(startup),
            ctypes.byref(process_info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        stage = "token-check"
        is_appcontainer = _is_appcontainer(kernel32, process_info.process)
        stage = "job-create"
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        )
        limits.basic_limit_information.active_process_limit = active_process_limit
        if process_memory_limit_bytes is not None:
            limits.basic_limit_information.limit_flags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            limits.process_memory_limit = process_memory_limit_bytes
        if job_memory_limit_bytes is not None:
            limits.basic_limit_information.limit_flags |= _JOB_OBJECT_LIMIT_JOB_MEMORY
            limits.job_memory_limit = job_memory_limit_bytes
        if process_user_time_100ns is not None:
            limits.basic_limit_information.limit_flags |= _JOB_OBJECT_LIMIT_PROCESS_TIME
            limits.basic_limit_information.per_process_user_time.quad_part = (
                process_user_time_100ns
            )
        if job_user_time_100ns is not None:
            limits.basic_limit_information.limit_flags |= _JOB_OBJECT_LIMIT_JOB_TIME
            limits.basic_limit_information.per_job_user_time.quad_part = job_user_time_100ns
        stage = "job-configure"
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        stage = "job-assign"
        if not kernel32.AssignProcessToJobObject(job, process_info.process):
            raise ctypes.WinError(ctypes.get_last_error())
        stage = "resume"
        if kernel32.ResumeThread(process_info.thread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        stage = "wait"
        wait_result = kernel32.WaitForSingleObject(process_info.process, timeout_ms)
        timed_out = wait_result == _WAIT_TIMEOUT
        job_information = _job_extended_info(kernel32, job)
        job_process_ids: list[int] = []
        all_job_processes_reaped: bool | None = None
        reap_seconds: float | None = None
        if timed_out:
            job_process_ids = _job_process_ids(kernel32, job)
            # Closing a kill-on-close Job Object is the cancellation operation.
            cancel_start = time.perf_counter()
            kernel32.CloseHandle(job)
            job = wintypes.HANDLE()
            kernel32.WaitForSingleObject(process_info.process, 3000)
            all_job_processes_reaped = _all_processes_reaped(kernel32, job_process_ids)
            reap_seconds = time.perf_counter() - cancel_start
        exit_code = wintypes.DWORD()
        stage = "exit-code"
        if not kernel32.GetExitCodeProcess(process_info.process, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            "is_appcontainer": is_appcontainer,
            "exit_code": int(exit_code.value),
            "timed_out": timed_out,
            "job_process_ids": job_process_ids,
            "all_job_processes_reaped": all_job_processes_reaped,
            "reap_seconds": reap_seconds,
            "job_information": job_information,
        }
    except Exception as exc:
        raise RuntimeError(f"{stage}: {exc}") from exc
    finally:
        if job:
            kernel32.CloseHandle(job)
        if process_info.thread:
            kernel32.CloseHandle(process_info.thread)
        if process_info.process:
            kernel32.CloseHandle(process_info.process)
        kernel32.DeleteProcThreadAttributeList(attribute_list)


def _job_process_ids(kernel32: Any, job: wintypes.HANDLE) -> list[int]:
    """Read the process IDs assigned to a Job Object before cancellation."""

    query = kernel32.QueryInformationJobObject
    query.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(8 + (ctypes.sizeof(ctypes.c_size_t) * 64))
    returned = wintypes.DWORD()
    if not query(
        job,
        _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
        buffer,
        ctypes.sizeof(buffer),
        ctypes.byref(returned),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    count = int(ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[1])
    ids = ctypes.cast(
        ctypes.byref(buffer, 8), ctypes.POINTER(ctypes.c_size_t)
    )
    return [int(ids[index]) for index in range(count)]


def _job_extended_info(kernel32: Any, job: wintypes.HANDLE) -> dict[str, int]:
    """Read bounded Job Object policy/accounting before the handle is closed."""

    query = kernel32.QueryInformationJobObject
    query.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query.restype = wintypes.BOOL
    info = _ExtendedLimitInformation()
    returned = wintypes.DWORD()
    if not query(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
        ctypes.byref(returned),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "limit_flags": int(info.basic_limit_information.limit_flags),
        "active_process_limit": int(info.basic_limit_information.active_process_limit),
        "process_memory_limit": int(info.process_memory_limit),
        "job_memory_limit": int(info.job_memory_limit),
        "process_user_time_limit_100ns": int(
            info.basic_limit_information.per_process_user_time.quad_part
        ),
        "job_user_time_limit_100ns": int(
            info.basic_limit_information.per_job_user_time.quad_part
        ),
        "peak_process_memory_used": int(info.peak_process_memory_used),
        "peak_job_memory_used": int(info.peak_job_memory_used),
    }


def _all_processes_reaped(kernel32: Any, process_ids: list[int]) -> bool:
    """Confirm every process observed in the Job is terminated after close."""

    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    all_reaped = True
    for process_id in process_ids:
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
            False,
            process_id,
        )
        if not handle:
            continue
        try:
            if kernel32.WaitForSingleObject(handle, 3000) != _WAIT_OBJECT_0:
                all_reaped = False
        finally:
            kernel32.CloseHandle(handle)
    return all_reaped


def _find_system_tool(name: str) -> str | None:
    system_root = os.environ.get("WINDIR", r"C:\Windows")
    path = Path(system_root) / "System32" / name
    return str(path) if path.is_file() else None


def run() -> dict[str, Any]:
    if sys.platform != "win32":
        return _result(BLOCKED, "The native AppContainer smoke test requires Windows.")

    findstr = _find_system_tool("findstr.exe")
    curl = _find_system_tool("curl.exe")
    if not findstr or not curl:
        return _result(
            BLOCKED,
            "Fixed native filesystem and network test tools are unavailable.",
            findstr=findstr,
            curl=curl,
        )

    kernel32, userenv, advapi32, *_ = _configure_apis()
    profile_name = f"CortexPhase0-{uuid4().hex}"
    profile_sid = wintypes.LPVOID()
    marker_path: Path | None = None
    listener: socket.socket | None = None
    stage = "profile-create"
    try:
        hr = userenv.CreateAppContainerProfile(
            profile_name,
            "Cortex Phase 0",
            "Disposable zero-capability containment smoke test",
            None,
            0,
            ctypes.byref(profile_sid),
        )
        if hr not in (0, 0x800700B7):  # S_OK or profile already exists (unique name makes latter unlikely)
            raise ctypes.WinError(hr & 0xFFFFFFFF)

        stage = "marker-create"
        marker_dir = Path(tempfile.mkdtemp(prefix="cortex-phase0-marker-"))
        marker_path = marker_dir / "private-marker.txt"
        marker_path.write_text("phase0-private-marker\n", encoding="ascii")
        stage = "filesystem-child"
        filesystem = _run_child(
            kernel32,
            userenv,
            advapi32,
            profile_name,
            profile_sid,
            findstr,
            ["/C:phase0-private-marker", str(marker_path)],
        )

        stage = "listener-create"
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.25)
        port = listener.getsockname()[1]
        stage = "network-child"
        network = _run_child(
            kernel32,
            userenv,
            advapi32,
            profile_name,
            profile_sid,
            curl,
            [
                "--silent",
                "--show-error",
                "--max-time",
                "2",
                "--connect-timeout",
                "1",
                f"http://127.0.0.1:{port}/phase0",
            ],
        )
        connected = False
        try:
            connection, _ = listener.accept()
            connected = True
            connection.close()
        except TimeoutError:
            pass

        checks = {
            "filesystem_denied": filesystem["is_appcontainer"] and filesystem["exit_code"] != 0,
            "network_denied": network["is_appcontainer"] and not connected,
            "filesystem_child_completed": not filesystem["timed_out"],
            "network_child_completed": not network["timed_out"],
        }
        status = PASS if all(checks.values()) else FAIL
        return _result(
            status,
            "A zero-capability AppContainer child proved its token and was denied parent-file and loopback access."
            if status == PASS
            else "The fixed AppContainer containment corpus did not prove token identity and both denials.",
            profile_name=profile_name,
            filesystem=filesystem,
            network=network,
            loopback_connected=connected,
            checks=checks,
        )
    except Exception as exc:
        return _result(
            FAIL,
            "The native AppContainer smoke test failed closed before containment was proven.",
            stage=stage,
            error_type=type(exc).__name__,
            error=str(exc),
        )
    finally:
        if listener is not None:
            listener.close()
        if marker_path is not None:
            try:
                marker_path.unlink(missing_ok=True)
                marker_path.parent.rmdir()
            except OSError:
                pass
        if profile_sid and profile_sid.value:
            try:
                advapi32.FreeSid(profile_sid)
            except OSError:
                pass
        try:
            userenv.DeleteAppContainerProfile(profile_name)
        except OSError:
            pass


def main() -> int:
    report = run()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
