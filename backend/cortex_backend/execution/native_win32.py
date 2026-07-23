"""Reviewed Win32 process factory for the fixed recipe worker.

The factory is intentionally opt-in: callers must inject it into
``NativeWorkerLauncher`` together with a live broker binder.  It creates a worker
with a zero-capability AppContainer token suspended, applies and queries the Job
Object limits before resume, and kills/cleans every handle on failure.  It never
accepts a path, command, environment, or shell value from a model or user.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys
from typing import Any
from uuid import uuid4

from .native_launcher import (
    NativeLauncherError,
    NativeSuspendedWorker,
    NativeWorkerLaunchPlan,
    NativeWorkerPolicy,
)


_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
_TOKEN_IS_APPCONTAINER = 29
_TOKEN_APPCONTAINER_SID = 31
_TOKEN_QUERY = 0x0008
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_INFINITE = 0xFFFFFFFF


class NativeWin32Error(NativeLauncherError):
    """Stable native adapter failure with no OS details exposed."""


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
    _fields_ = [("startup_info", _StartupInfo), ("attribute_list", wintypes.LPVOID)]


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


@dataclass(slots=True)
class _Win32:
    kernel32: Any
    userenv: Any
    advapi32: Any


def _require_windows() -> None:
    if sys.platform != "win32":
        raise NativeWin32Error("native_windows_required")


def _invalid_handle(handle: Any) -> bool:
    return not handle or handle == ctypes.c_void_p(-1).value


def _raise(code: str) -> None:
    raise NativeWin32Error(code)


def _configure() -> _Win32:
    _require_windows()
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
    # HRESULT is a signed 32-bit value; wintypes.HRESULT is not exposed by all
    # supported CPython Windows builds (notably Python 3.11 on CI).
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
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
    advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

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
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    return _Win32(kernel32=kernel32, userenv=userenv, advapi32=advapi32)


def _close(win: _Win32, handle: Any) -> None:
    if not _invalid_handle(handle):
        win.kernel32.CloseHandle(handle)


def _app_container_sid(win: _Win32, process: Any) -> str:
    token = wintypes.HANDLE()
    if not win.advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
        _raise("native_token_open_failed")
    try:
        is_container = wintypes.DWORD()
        returned = wintypes.DWORD()
        if not win.advapi32.GetTokenInformation(
            token,
            _TOKEN_IS_APPCONTAINER,
            ctypes.byref(is_container),
            ctypes.sizeof(is_container),
            ctypes.byref(returned),
        ):
            _raise("native_token_query_failed")
        if not is_container.value:
            _raise("native_appcontainer_missing")
        required = wintypes.DWORD()
        win.advapi32.GetTokenInformation(
            token,
            _TOKEN_APPCONTAINER_SID,
            None,
            0,
            ctypes.byref(required),
        )
        if not required.value:
            _raise("native_appcontainer_sid_missing")
        buffer = ctypes.create_string_buffer(required.value)
        if not win.advapi32.GetTokenInformation(
            token,
            _TOKEN_APPCONTAINER_SID,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            _raise("native_appcontainer_sid_missing")
        sid_ptr = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        if not win.advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_text)):
            _raise("native_appcontainer_sid_invalid")
        try:
            return sid_text.value or ""
        finally:
            win.kernel32.LocalFree(sid_text)
    finally:
        _close(win, token)


class Win32SuspendedWorker:
    """Own a suspended worker and its kill-on-close Job Object."""

    def __init__(
        self,
        win: _Win32,
        *,
        profile_name: str,
        process: Any,
        thread: Any,
        process_id: int,
        app_container_sid: str,
    ) -> None:
        self._win = win
        self._profile_name = profile_name
        self._process = process
        self._thread = thread
        self._job: Any = None
        self._closed = False
        self._resumed = False
        self.process_id = process_id
        self.app_container_sid = app_container_sid

    def apply_job_policy(self, policy: NativeWorkerPolicy) -> None:
        if self._closed or self._resumed or self._job is not None:
            _raise("native_job_state_invalid")
        job = self._win.kernel32.CreateJobObjectW(None, None)
        if _invalid_handle(job):
            _raise("native_job_create_failed")
        try:
            limits = _ExtendedLimitInformation()
            limits.basic_limit_information.limit_flags = policy.required_limit_flags
            limits.basic_limit_information.active_process_limit = policy.active_process_limit
            limits.basic_limit_information.per_process_user_time.quad_part = (
                policy.process_user_time_100ns
            )
            limits.basic_limit_information.per_job_user_time.quad_part = policy.job_user_time_100ns
            limits.process_memory_limit = policy.process_memory_limit_bytes
            limits.job_memory_limit = policy.job_memory_limit_bytes
            if not self._win.kernel32.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                _raise("native_job_configure_failed")
            if not self._win.kernel32.AssignProcessToJobObject(job, self._process):
                _raise("native_job_assign_failed")
            self._verify_job_policy(job, policy)
            self._job = job
        except Exception:
            _close(self._win, job)
            raise

    def _verify_job_policy(self, job: Any, policy: NativeWorkerPolicy) -> None:
        info = _ExtendedLimitInformation()
        returned = wintypes.DWORD()
        if not self._win.kernel32.QueryInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        ):
            _raise("native_job_query_failed")
        flags = int(info.basic_limit_information.limit_flags)
        if flags != policy.required_limit_flags or flags & (
            _JOB_OBJECT_LIMIT_BREAKAWAY_OK | _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        ):
            _raise("native_job_policy_mismatch")
        if int(info.basic_limit_information.active_process_limit) != policy.active_process_limit:
            _raise("native_job_policy_mismatch")
        if int(info.process_memory_limit) != policy.process_memory_limit_bytes:
            _raise("native_job_policy_mismatch")
        if int(info.job_memory_limit) != policy.job_memory_limit_bytes:
            _raise("native_job_policy_mismatch")
        if int(info.basic_limit_information.per_process_user_time.quad_part) != policy.process_user_time_100ns:
            _raise("native_job_policy_mismatch")
        if int(info.basic_limit_information.per_job_user_time.quad_part) != policy.job_user_time_100ns:
            _raise("native_job_policy_mismatch")

    def resume(self) -> None:
        if self._closed or self._resumed or self._job is None:
            _raise("native_resume_state_invalid")
        if self._win.kernel32.ResumeThread(self._thread) == 0xFFFFFFFF:
            _raise("native_resume_failed")
        self._resumed = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._job is None and not _invalid_handle(self._process):
            self._win.kernel32.TerminateProcess(self._process, 1)
        if not _invalid_handle(self._job):
            _close(self._win, self._job)
            self._job = None
        if not _invalid_handle(self._process):
            self._win.kernel32.WaitForSingleObject(self._process, 3000)
        _close(self._win, self._thread)
        _close(self._win, self._process)
        self._thread = None
        self._process = None
        self._win.userenv.DeleteAppContainerProfile(self._profile_name)

    def __enter__(self) -> "Win32SuspendedWorker":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class NativeWin32ProcessFactory:
    """Create a fixed worker in a zero-capability AppContainer, suspended."""

    def __init__(self, *, profile_prefix: str = "CortexRecipe") -> None:
        if not isinstance(profile_prefix, str) or not profile_prefix.isidentifier():
            raise ValueError("profile prefix is invalid")
        self._profile_prefix = profile_prefix

    def create_suspended(self, plan: NativeWorkerLaunchPlan) -> NativeSuspendedWorker:
        _require_windows()
        profile_name = f"{self._profile_prefix}{uuid4().hex}"
        win = _configure()
        profile_sid = wintypes.LPVOID()
        process_info = _ProcessInformation()
        attribute_list: wintypes.LPVOID | None = None
        attribute_buffer: ctypes.Array[ctypes.c_char] | None = None
        profile_created = False
        try:
            hr = win.userenv.CreateAppContainerProfile(
                profile_name,
                "Cortex recipe worker",
                "Zero-capability fixed image recipe worker",
                None,
                0,
                ctypes.byref(profile_sid),
            )
            if hr not in (0, 0x800700B7):
                _raise("native_profile_create_failed")
            profile_created = True
            required = ctypes.c_size_t()
            win.kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(required))
            if not required.value:
                _raise("native_attribute_size_failed")
            attribute_buffer = ctypes.create_string_buffer(required.value)
            attribute_list = ctypes.cast(attribute_buffer, wintypes.LPVOID)
            if not win.kernel32.InitializeProcThreadAttributeList(
                attribute_list, 1, 0, ctypes.byref(required)
            ):
                _raise("native_attribute_init_failed")
            capabilities = _SecurityCapabilities(
                app_container_sid=profile_sid,
                capabilities=None,
                capability_count=0,
                reserved=0,
            )
            if not win.kernel32.UpdateProcThreadAttribute(
                attribute_list,
                0,
                _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                ctypes.byref(capabilities),
                ctypes.sizeof(capabilities),
                None,
                None,
            ):
                _raise("native_attribute_security_failed")
            startup = _StartupInfoEx()
            startup.startup_info.cb = ctypes.sizeof(startup)
            startup.attribute_list = attribute_list
            command_buffer = ctypes.create_unicode_buffer(plan.command_line)
            flags = (
                _EXTENDED_STARTUPINFO_PRESENT
                | _CREATE_SUSPENDED
                | _CREATE_UNICODE_ENVIRONMENT
                | _CREATE_NO_WINDOW
            )
            if not win.kernel32.CreateProcessW(
                str(plan.executable),
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
                _raise("native_process_create_failed")
            sid = _app_container_sid(win, process_info.process)
            if not sid.startswith("S-1-15-2-"):
                _raise("native_appcontainer_sid_invalid")
            if attribute_list is not None:
                win.kernel32.DeleteProcThreadAttributeList(attribute_list)
                attribute_list = None
            return Win32SuspendedWorker(
                win,
                profile_name=profile_name,
                process=process_info.process,
                thread=process_info.thread,
                process_id=int(process_info.process_id),
                app_container_sid=sid,
            )
        except Exception:
            if attribute_list is not None:
                win.kernel32.DeleteProcThreadAttributeList(attribute_list)
            if not _invalid_handle(process_info.thread):
                _close(win, process_info.thread)
            if not _invalid_handle(process_info.process):
                win.kernel32.TerminateProcess(process_info.process, 1)
                win.kernel32.WaitForSingleObject(process_info.process, 3000)
                _close(win, process_info.process)
            if profile_created:
                win.userenv.DeleteAppContainerProfile(profile_name)
            if isinstance(sys.exc_info()[1], NativeLauncherError):
                raise
            raise NativeWin32Error("native_process_create_failed") from None


__all__ = ["NativeWin32Error", "NativeWin32ProcessFactory", "Win32SuspendedWorker"]
