"""Read Windows directory DACLs without changing machine/toolchain permissions.

A toolchain is usable if ALL APPLICATION PACKAGES already has inherited RX,
or if the current (possibly non-admin) token can grant the per-run SID RX.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from pathlib import Path
from collections.abc import Iterator
from typing import Any, Literal

_APP_PACKAGES = "S-1-15-2-1"
_FILE_RX = 0x001200A9
_FILE_ALL = 0x001F01FF
_WRITE_DAC = 0x00040000
_OBJECT_INHERIT = 0x01
_CONTAINER_INHERIT = 0x02
_NO_PROPAGATE = 0x04
_INHERIT_ONLY = 0x08
_INHERITED = 0x10


class _Ace(ctypes.Structure):
    _fields_ = [
        ("kind", wintypes.BYTE), ("flags", wintypes.BYTE),
        ("size", wintypes.WORD), ("mask", wintypes.DWORD),
        ("sid_start", wintypes.DWORD),
    ]


class _GenericMapping(ctypes.Structure):
    _fields_ = [
        ("read", wintypes.DWORD), ("write", wintypes.DWORD),
        ("execute", wintypes.DWORD), ("all", wintypes.DWORD),
    ]


def _function(dll: Any, name: str, args: list[Any], result: Any) -> Any:
    function = getattr(dll, name)
    function.argtypes = args
    function.restype = result
    return function


def _check_os_call(ok: int, operation: str) -> None:
    if not ok:
        raise OSError(ctypes.get_last_error(), f"{operation} failed")


def _local_free(ptr: ctypes.c_void_p) -> None:
    if ptr.value:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        _function(kernel, "LocalFree", [wintypes.HLOCAL], wintypes.HLOCAL)(ptr)


@contextmanager
def _security(path: Path) -> Iterator[tuple[Any, ctypes.c_void_p, ctypes.c_void_p]]:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    owner = ctypes.c_void_p()
    group = ctypes.c_void_p()
    get = _function(advapi, "GetNamedSecurityInfoW", [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ], wintypes.DWORD)
    # AccessCheck needs owner and group as well as the DACL.
    status = get(str(path), 1, 0x1 | 0x2 | 0x4, ctypes.byref(owner),
                 ctypes.byref(group), ctypes.byref(dacl), None,
                 ctypes.byref(descriptor))
    if status:
        raise OSError(status, f"GetNamedSecurityInfoW failed for {path}")
    try:
        yield advapi, descriptor, dacl
    finally:
        _local_free(descriptor)


@contextmanager
def _sid(advapi: Any, value: str) -> Iterator[ctypes.c_void_p]:
    pointer = ctypes.c_void_p()
    convert = _function(advapi, "ConvertStringSidToSidW", [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
    ], wintypes.BOOL)
    _check_os_call(convert(value, ctypes.byref(pointer)), "ConvertStringSidToSidW")
    try:
        yield pointer
    finally:
        _local_free(pointer)


def _aces(advapi: Any, dacl: ctypes.c_void_p) -> Iterator[tuple[_Ace, ctypes.c_void_p]]:
    if not dacl.value:
        return
    count = wintypes.WORD.from_address(dacl.value + 4).value
    get = _function(advapi, "GetAce", [
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ], wintypes.BOOL)
    for index in range(count):
        ptr = ctypes.c_void_p()
        _check_os_call(get(dacl, index, ctypes.byref(ptr)), "GetAce")
        if ptr.value is None:
            raise OSError("GetAce returned a null pointer")
        ace = ctypes.cast(ptr, ctypes.POINTER(_Ace)).contents
        # Standard access-allow/deny ACEs have their SID at offset 8.
        if ace.kind in (0, 1) and ace.size >= ctypes.sizeof(_Ace) + 4:
            yield ace, ctypes.c_void_p(ptr.value + _Ace.sid_start.offset)


def _expanded_mask(mask: int) -> int:
    if mask & 0x10000000:  # GENERIC_ALL
        mask |= _FILE_ALL
    if mask & 0x80000000:  # GENERIC_READ
        mask |= 0x00120089
    if mask & 0x20000000:  # GENERIC_EXECUTE
        mask |= 0x001200A0
    return mask


def _app_packages_have_rx(advapi: Any, dacl: ctypes.c_void_p) -> bool:
    if not dacl.value:  # NULL DACL: the directory is accessible to everyone.
        return True
    equal = _function(advapi, "EqualSid", [wintypes.LPVOID, wintypes.LPVOID], wintypes.BOOL)
    folder_allow = child_allow = folder_deny = child_deny = 0
    with _sid(advapi, _APP_PACKAGES) as sid:
        for ace, ace_sid in _aces(advapi, dacl):
            if not equal(sid, ace_sid):
                continue
            mask = _expanded_mask(int(ace.mask))
            if not ace.flags & _INHERIT_ONLY:
                if ace.kind == 0:
                    folder_allow |= mask
                else:
                    folder_deny |= mask
            # OI+CI without NO_PROPAGATE covers files and nested directories.
            if ((ace.flags & (_OBJECT_INHERIT | _CONTAINER_INHERIT))
                    == (_OBJECT_INHERIT | _CONTAINER_INHERIT)
                    and not ace.flags & _NO_PROPAGATE):
                if ace.kind == 0:
                    child_allow |= mask
                else:
                    child_deny |= mask
    return (folder_allow & _FILE_RX == _FILE_RX and child_allow & _FILE_RX == _FILE_RX
            and not (folder_deny | child_deny) & _FILE_RX)


def _can_write_dacl(advapi: Any, descriptor: ctypes.c_void_p) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_token = _function(advapi, "OpenProcessToken", [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ], wintypes.BOOL)
    duplicate = _function(advapi, "DuplicateToken", [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ], wintypes.BOOL)
    get_process = _function(kernel, "GetCurrentProcess", [], wintypes.HANDLE)
    close = _function(kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
    token = wintypes.HANDLE()
    impersonation = wintypes.HANDLE()
    try:
        _check_os_call(open_token(get_process(), 0x0002 | 0x0008, ctypes.byref(token)), "OpenProcessToken")
        _check_os_call(duplicate(token, 2, ctypes.byref(impersonation)), "DuplicateToken")
        access_check = _function(advapi, "AccessCheck", [
            wintypes.LPVOID, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(_GenericMapping),
            wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.BOOL),
        ], wintypes.BOOL)
        mapping = _GenericMapping(0x00120089, 0x00120116, 0x001200A0, _FILE_ALL)
        privileges = ctypes.create_string_buffer(512)
        size = wintypes.DWORD(len(privileges))
        granted = wintypes.DWORD()
        permitted = wintypes.BOOL()
        _check_os_call(access_check(descriptor, impersonation, _WRITE_DAC, ctypes.byref(mapping),
                                    privileges, ctypes.byref(size), ctypes.byref(granted),
                                    ctypes.byref(permitted)), "AccessCheck")
        return bool(permitted.value)
    finally:
        if impersonation:
            close(impersonation)
        if token:
            close(token)


def tool_folder_policy(path: Path) -> Literal["shared", "grantable", "unavailable"]:
    """Determine whether a tool folder needs (and can accept) a private SID ACE."""
    with _security(path) as (advapi, descriptor, dacl):
        if _app_packages_have_rx(advapi, dacl):
            return "shared"
        return "grantable" if _can_write_dacl(advapi, descriptor) else "unavailable"


def has_explicit_sid_grant(path: Path, sid_string: str) -> bool:
    """Avoid revocation on a failed /grant that never installed an ACE."""
    with _security(path) as (advapi, _descriptor, dacl), _sid(advapi, sid_string) as sid:
        equal = _function(advapi, "EqualSid", [wintypes.LPVOID, wintypes.LPVOID], wintypes.BOOL)
        return any(ace.kind == 0 and not ace.flags & _INHERITED and equal(sid, ace_sid)
                   for ace, ace_sid in _aces(advapi, dacl))
