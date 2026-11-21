"""Small Win32 AppContainer and extended-startup bindings (loaded only on Windows).

No Python child is allowed to execute before its caller attaches a Job Object.
The low-box token comes from PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES; a
plain CREATE_SUSPENDED process without that attribute is *not* a sandbox.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROFILE_PREFIX = "Cortex.Agent."
INTERNET_CLIENT_SID = "S-1-15-3-1"
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
CREATE_SUSPENDED = 0x00000004
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 1
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
STILL_ACTIVE = 259
SE_GROUP_ENABLED = 0x4


class AppContainerError(OSError):
    """An operating-system AppContainer operation failed."""


class ProfileCreationError(AppContainerError):
    """Only this error permits selection of the explicitly labelled fallback."""


def _dll(name: str) -> Any:
    if sys.platform != "win32":
        raise AppContainerError("AppContainer launch requires Windows")
    return ctypes.WinDLL(name, use_last_error=True)


def _fn(dll: Any, name: str, args: list[Any], result: Any) -> Any:
    fn = getattr(dll, name)
    fn.argtypes = args
    fn.restype = result
    return fn


def _failed(operation: str) -> AppContainerError:
    return AppContainerError(f"{operation} failed (Win32 error {ctypes.get_last_error()})")


def _hresult_ok(value: int) -> bool:
    return (value & 0x80000000) == 0


def _free(ptr: int | None) -> None:
    if ptr:
        _fn(_dll("kernel32"), "LocalFree", [wintypes.HLOCAL], wintypes.HLOCAL)(ptr)


def _sid_string(sid: int) -> str:
    advapi = _dll("advapi32")
    out = ctypes.c_wchar_p()
    convert = _fn(advapi, "ConvertSidToStringSidW", [wintypes.LPVOID, ctypes.POINTER(ctypes.c_wchar_p)], wintypes.BOOL)
    if not convert(sid, ctypes.byref(out)):
        raise _failed("ConvertSidToStringSidW")
    try:
        return str(out.value)
    finally:
        _free(ctypes.cast(out, ctypes.c_void_p).value)


def create_profile(name: str) -> str:
    """Create a fresh per-run profile, returning its canonical SID string."""
    if not name.startswith(PROFILE_PREFIX):
        raise ValueError("invalid agent AppContainer name")
    userenv = _dll("userenv")
    create = _fn(userenv, "CreateAppContainerProfile", [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ], ctypes.c_long)
    sid = ctypes.c_void_p()
    status = create(name, name, "Cortex coding agent run", None, 0, ctypes.byref(sid))
    if not _hresult_ok(status):
        raise ProfileCreationError(f"CreateAppContainerProfile failed (HRESULT 0x{status & 0xffffffff:08x})")
    try:
        return derive_profile_sid(name)
    except BaseException:
        delete_profile(name)
        raise
    finally:
        _free(sid.value)


def derive_profile_sid(name: str) -> str:
    userenv = _dll("userenv")
    derive = _fn(userenv, "DeriveAppContainerSidFromAppContainerName", [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
    ], ctypes.c_long)
    sid = ctypes.c_void_p()
    status = derive(name, ctypes.byref(sid))
    if not _hresult_ok(status):
        raise AppContainerError(f"DeriveAppContainerSidFromAppContainerName failed (0x{status & 0xffffffff:08x})")
    try:
        sid_value = sid.value
        if sid_value is None:
            raise AppContainerError("DeriveAppContainerSidFromAppContainerName returned a null SID")
        return _sid_string(sid_value)
    finally:
        _free(sid.value)


def delete_profile(name: str) -> None:
    if not name.startswith(PROFILE_PREFIX):
        raise ValueError("invalid agent AppContainer name")
    delete = _fn(_dll("userenv"), "DeleteAppContainerProfile", [wintypes.LPCWSTR], ctypes.c_long)
    status = delete(name)
    # ERROR_FILE_NOT_FOUND: already gone is a successful teardown.
    if not _hresult_ok(status) and (status & 0xffffffff) != 0x80070002:
        raise AppContainerError(f"DeleteAppContainerProfile failed (0x{status & 0xffffffff:08x})")


def list_profiles() -> list[str]:
    """Enumerate this user's profile monikers, not the machine's other users."""
    if sys.platform != "win32":
        return []
    import winreg

    root = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer\Mappings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root) as mapping:
            names: list[str] = []
            index = 0
            while True:
                try:
                    sid = winreg.EnumKey(mapping, index)
                except OSError as exc:
                    if getattr(exc, "winerror", None) == 259:
                        break
                    raise
                index += 1
                with winreg.OpenKey(mapping, sid) as key:
                    try:
                        name, _ = winreg.QueryValueEx(key, "Moniker")
                    except FileNotFoundError:
                        continue
                    if isinstance(name, str) and name.startswith(PROFILE_PREFIX):
                        names.append(name)
            return names
    except FileNotFoundError:
        return []


class SidAndAttributes(ctypes.Structure):
    _fields_ = [("sid", wintypes.LPVOID), ("attributes", wintypes.DWORD)]


class SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("app_container_sid", wintypes.LPVOID),
        ("capabilities", ctypes.POINTER(SidAndAttributes)),
        ("capability_count", wintypes.DWORD),
        ("reserved", wintypes.DWORD),
    ]


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("descriptor", wintypes.LPVOID), ("inherit", wintypes.BOOL)]


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
        ("x", wintypes.DWORD), ("y", wintypes.DWORD),
        ("x_size", wintypes.DWORD), ("y_size", wintypes.DWORD),
        ("x_chars", wintypes.DWORD), ("y_chars", wintypes.DWORD),
        ("fill", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("show", wintypes.WORD), ("reserved_size", wintypes.WORD),
        ("reserved_bytes", wintypes.LPBYTE),
        ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE),
    ]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [("startup", StartupInfo), ("attributes", wintypes.LPVOID)]


class ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
        ("pid", wintypes.DWORD), ("tid", wintypes.DWORD),
    ]


class NativeProcess:
    """Owned process/thread/pipe handles returned by suspended CreateProcessW."""

    def __init__(self, info: ProcessInformation, stdout_fd: int, kernel: Any):
        self._info = info
        self._kernel = kernel
        self.pid = int(info.pid)
        self.stdout = os.fdopen(stdout_fd, "rb", buffering=0)
        self._closed = False

    def resume(self) -> None:
        resume = _fn(self._kernel, "ResumeThread", [wintypes.HANDLE], wintypes.DWORD)
        if resume(self._info.thread) == WAIT_FAILED:
            raise _failed("ResumeThread")
        self._kernel.CloseHandle(self._info.thread)
        self._info.thread = None

    def wait(self, timeout: float | None = None) -> int:
        wait = _fn(self._kernel, "WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD)
        milliseconds = INFINITE if timeout is None else min(0xFFFFFFFE, max(0, int(timeout * 1000)))
        status = wait(self._info.process, milliseconds)
        if status == WAIT_TIMEOUT:
            if timeout is None:
                raise AppContainerError("WaitForSingleObject timed out during an indefinite wait")
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        if status != 0:
            raise _failed("WaitForSingleObject")
        get_code = _fn(
            self._kernel, "GetExitCodeProcess",
            [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL,
        )
        code = wintypes.DWORD()
        if not get_code(self._info.process, ctypes.byref(code)):
            raise _failed("GetExitCodeProcess")
        return int(code.value)

    def kill(self) -> None:
        terminate = _fn(self._kernel, "TerminateProcess", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL)
        terminate(self._info.process, 1)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stdout.close()
        if self._info.thread:
            self._kernel.CloseHandle(self._info.thread)
        self._kernel.CloseHandle(self._info.process)


def spawn_suspended(
    argv: list[str], cwd: Path, environment: dict[str, str], appcontainer_sid: str, *, network: bool,
) -> NativeProcess:
    """Create a low-box process *suspended*, inheriting only its two stdio handles.

    All ctypes buffers are pinned until CreateProcessW returns. A failed launch
    closes every locally owned handle and never produces a runnable process.
    """
    import msvcrt  # Windows-only CRT; loaded lazily to keep this module importable elsewhere.

    kernel = _dll("kernel32")
    advapi = _dll("advapi32")
    convert = _fn(advapi, "ConvertStringSidToSidW", [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)], wintypes.BOOL)
    app_sid = ctypes.c_void_p()
    internet_sid = ctypes.c_void_p()
    if not convert(appcontainer_sid, ctypes.byref(app_sid)):
        raise _failed("ConvertStringSidToSidW")
    pipe_read = wintypes.HANDLE()
    pipe_write = wintypes.HANDLE()
    stdin_handle = wintypes.HANDLE()
    info = ProcessInformation()
    attrs_buffer: Any = None
    attrs_initialized = False
    fd: int | None = None
    close = _fn(kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
    try:
        caps_array = (SidAndAttributes * 1)()
        caps_ptr = ctypes.POINTER(SidAndAttributes)()
        if network:
            if not convert(INTERNET_CLIENT_SID, ctypes.byref(internet_sid)):
                raise _failed("ConvertStringSidToSidW(internetClient)")
            caps_array[0] = SidAndAttributes(internet_sid, SE_GROUP_ENABLED)
            caps_ptr = ctypes.cast(caps_array, ctypes.POINTER(SidAndAttributes))
        caps = SecurityCapabilities(app_sid, caps_ptr, 1 if network else 0, 0)
        sa = SecurityAttributes(ctypes.sizeof(SecurityAttributes), None, True)
        create_pipe = _fn(kernel, "CreatePipe", [
            ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(SecurityAttributes), wintypes.DWORD,
        ], wintypes.BOOL)
        if not create_pipe(ctypes.byref(pipe_read), ctypes.byref(pipe_write), ctypes.byref(sa), 0):
            raise _failed("CreatePipe")
        read_handle = pipe_read.value
        if read_handle is None or pipe_write.value is None:
            raise AppContainerError("CreatePipe returned a null pipe handle")
        set_flags = _fn(
            kernel, "SetHandleInformation", [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD], wintypes.BOOL,
        )
        if not set_flags(pipe_read, HANDLE_FLAG_INHERIT, 0):
            raise _failed("SetHandleInformation")
        create_file = _fn(kernel, "CreateFileW", [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SecurityAttributes),
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ], wintypes.HANDLE)
        stdin_handle = create_file("NUL", 0x80000000, 3, ctypes.byref(sa), 3, 0x80, None)
        if not stdin_handle or stdin_handle == wintypes.HANDLE(-1).value:
            raise _failed("CreateFileW(NUL)")
        init = _fn(kernel, "InitializeProcThreadAttributeList", [
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t),
        ], wintypes.BOOL)
        update = _fn(kernel, "UpdateProcThreadAttribute", [
            wintypes.LPVOID, wintypes.DWORD, ctypes.c_size_t, wintypes.LPVOID,
            ctypes.c_size_t, wintypes.LPVOID, wintypes.LPVOID,
        ], wintypes.BOOL)
        size = ctypes.c_size_t()
        init(None, 2, 0, ctypes.byref(size))  # expected ERROR_INSUFFICIENT_BUFFER
        if not size.value:
            raise _failed("InitializeProcThreadAttributeList(size)")
        attrs_buffer = ctypes.create_string_buffer(size.value)
        if not init(attrs_buffer, 2, 0, ctypes.byref(size)):
            raise _failed("InitializeProcThreadAttributeList")
        attrs_initialized = True
        if not update(attrs_buffer, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                      ctypes.byref(caps), ctypes.sizeof(caps), None, None):
            raise _failed("UpdateProcThreadAttribute(security)")
        inherited = (wintypes.HANDLE * 2)(stdin_handle, pipe_write)
        if not update(attrs_buffer, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                      ctypes.byref(inherited), ctypes.sizeof(inherited), None, None):
            raise _failed("UpdateProcThreadAttribute(handles)")
        startup = StartupInfoEx()
        startup.startup.cb = ctypes.sizeof(startup)
        startup.startup.flags = STARTF_USESTDHANDLES
        startup.startup.stdin = stdin_handle
        startup.startup.stdout = pipe_write
        startup.startup.stderr = pipe_write
        startup.attributes = ctypes.cast(attrs_buffer, wintypes.LPVOID)
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        envblock = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(environment.items())) + "\0\0")
        create_process = _fn(kernel, "CreateProcessW", [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID,
            wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
            ctypes.POINTER(StartupInfoEx), ctypes.POINTER(ProcessInformation),
        ], wintypes.BOOL)
        if not create_process(argv[0], command, None, None, True,
                              CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT
                              | CREATE_UNICODE_ENVIRONMENT,
                              envblock, str(cwd), ctypes.byref(startup), ctypes.byref(info)):
            raise _failed("CreateProcessW(AppContainer)")
        fd = msvcrt.open_osfhandle(read_handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        pipe_read = wintypes.HANDLE()  # CRT owns this handle now
        child = NativeProcess(info, fd, kernel)
        fd = None
        return child
    except BaseException:
        if info.process:
            _fn(kernel, "TerminateProcess", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL)(info.process, 1)
            close(info.process)
        if info.thread:
            close(info.thread)
        raise
    finally:
        if attrs_initialized:
            _fn(kernel, "DeleteProcThreadAttributeList", [wintypes.LPVOID], None)(attrs_buffer)
        for handle in (pipe_read, pipe_write, stdin_handle):
            if handle:
                close(handle)
        if fd is not None:
            os.close(fd)
        _free(app_sid.value)
        _free(internet_sid.value)
