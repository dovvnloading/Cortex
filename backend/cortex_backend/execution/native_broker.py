"""Windows named-pipe adapter for the authenticated broker contract.

This module is deliberately transport-only.  It creates one protected local
named-pipe instance, binds the connected peer to an OS-reported PID/token
identity, performs a short X25519/HKDF handshake, and exposes framed message I/O.
It never launches a process, reads a file, stages an artifact, or dispatches an
operation.  A missing Windows API, ACL, identity, handshake, or authorization
step fails closed.
"""

from __future__ import annotations

from collections import deque
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import hmac
import os
import re
import secrets
import struct
import sys
from typing import Any, Callable, Literal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .broker import (
    MAX_BROKER_PAYLOAD_BYTES,
    BrokerAclPolicy,
    BrokerDirection,
    BrokerFrame,
    BrokerFrameDecoder,
    BrokerMessage,
    BrokerPeerPolicy,
    BrokerProtocolError,
    BrokerSessionKeys,
    IntegrityLevel,
    PeerIdentity,
    authorize_message,
    decode_message,
    encode_message,
)


MAX_NATIVE_PIPE_NAME_LENGTH = 256
DEFAULT_NATIVE_PIPE_BUFFER_BYTES = MAX_BROKER_PAYLOAD_BYTES + 4096
DEFAULT_NATIVE_CONNECT_TIMEOUT_MS = 5000
_PIPE_NAME = re.compile(r"^\\\\\.\\pipe\\[A-Za-z0-9._-]{1,240}$")

_HANDSHAKE_MAGIC = b"CXHS"
_HANDSHAKE_VERSION = 1
_HANDSHAKE_SERVER = 1
_HANDSHAKE_CLIENT = 2
_HANDSHAKE_HELLO = 1
_HANDSHAKE_CONFIRM = 2
_HANDSHAKE_HEADER = struct.Struct(">4sBBBBH")
_HANDSHAKE_NONCE_BYTES = 32
_HANDSHAKE_KEY_BYTES = 32
_HANDSHAKE_MAX_PAYLOAD_BYTES = 128
_HANDSHAKE_INFO = b"cortex-broker-session-v1"
_HANDSHAKE_CONFIRM_INFO = b"cortex-broker-confirm-v1"

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_PIPE_CONNECTED = 535
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_ERROR_PIPE_NOT_CONNECTED = 233
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PIPE_BUSY = 231
_ERROR_SEM_TIMEOUT = 121

_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_SECURITY_SQOS_PRESENT = 0x00100000
_SECURITY_IDENTIFICATION = 0x00010000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_TOKEN_INTEGRITY_LEVEL = 25
_TOKEN_IS_APP_CONTAINER = 29
_TOKEN_APP_CONTAINER_SID = 31
_SDDL_REVISION_1 = 1

_INTEGRITY_RIDS: tuple[tuple[int, IntegrityLevel], ...] = (
    (0x1000, "low"),
    (0x2000, "medium"),
    (0x3000, "high"),
    (0x4000, "system"),
)


class NativeBrokerError(BrokerProtocolError):
    """Stable native adapter failure without Win32/path/token disclosure."""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise NativeBrokerError("native_windows_required")


def _validate_pipe_name(pipe_name: str) -> None:
    if (
        not isinstance(pipe_name, str)
        or len(pipe_name) > MAX_NATIVE_PIPE_NAME_LENGTH
        or _PIPE_NAME.fullmatch(pipe_name) is None
    ):
        raise ValueError("native broker pipe name is invalid")


def _validate_native_peer_policy(peer_policy: BrokerPeerPolicy) -> None:
    if peer_policy.expected_process_id is None:
        raise ValueError("native broker requires an expected peer process ID")


def build_pipe_sddl(acl: BrokerAclPolicy) -> str:
    """Build a protected, deterministic DACL with no inherited/broad ACEs."""

    # BrokerAclPolicy validates SID syntax and requires both identity classes.
    sids = sorted(acl.allowed_user_sids | acl.allowed_app_container_sids)
    return "D:P" + "".join(f"(A;;GA;;;{sid})" for sid in sids)


@dataclass(frozen=True, slots=True)
class NativeBrokerServerConfig:
    pipe_name: str
    peer_policy: BrokerPeerPolicy
    pipe_buffer_bytes: int = DEFAULT_NATIVE_PIPE_BUFFER_BYTES

    def __post_init__(self) -> None:
        _validate_pipe_name(self.pipe_name)
        _validate_native_peer_policy(self.peer_policy)
        if not isinstance(self.pipe_buffer_bytes, int) or not (
            MAX_BROKER_PAYLOAD_BYTES <= self.pipe_buffer_bytes <= 1024 * 1024
        ):
            raise ValueError("native broker pipe buffer is invalid")


@dataclass(frozen=True, slots=True)
class NativeBrokerClientConfig:
    pipe_name: str
    expected_server_process_id: int
    connect_timeout_ms: int = DEFAULT_NATIVE_CONNECT_TIMEOUT_MS

    def __post_init__(self) -> None:
        _validate_pipe_name(self.pipe_name)
        if type(self.expected_server_process_id) is not int or self.expected_server_process_id <= 0:
            raise ValueError("native broker server process ID is invalid")
        if not isinstance(self.connect_timeout_ms, int) or not (
            1 <= self.connect_timeout_ms <= 60_000
        ):
            raise ValueError("native broker connect timeout is invalid")


@dataclass(slots=True)
class _SecurityAttributes:
    """ctypes representation kept alive through CreateNamedPipeW."""

    nLength: int
    lpSecurityDescriptor: ctypes.c_void_p
    bInheritHandle: int


class _WinSecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _TokenUser(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]


class _TokenMandatoryLabel(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]


class _TokenAppContainerInformation(ctypes.Structure):
    _fields_ = [("app_container_sid", ctypes.c_void_p)]


@dataclass(slots=True)
class _Win32:
    kernel32: Any
    advapi32: Any


def _load_win32() -> _Win32:
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)

    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_WinSecurityAttributes),
    ]
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.ConnectNamedPipe.restype = wintypes.BOOL
    kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_WinSecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    kernel32.GetNamedPipeClientProcessId.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
    kernel32.GetNamedPipeServerProcessId.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

    return _Win32(kernel32=kernel32, advapi32=advapi32)


def _handle_value(handle: Any) -> int | None:
    if handle is None:
        return None
    value = getattr(handle, "value", handle)
    return None if value is None else int(value)


def _is_invalid_handle(handle: Any) -> bool:
    value = _handle_value(handle)
    return value in (None, 0, _INVALID_HANDLE_VALUE)


def _close_handle(win: _Win32, handle: Any) -> None:
    if not _is_invalid_handle(handle):
        win.kernel32.CloseHandle(handle)


def _pipe_error(code: int | None) -> NativeBrokerError:
    if code in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_PIPE_NOT_CONNECTED}:
        return NativeBrokerError("native_pipe_closed")
    if code in {_ERROR_FILE_NOT_FOUND, _ERROR_PIPE_BUSY}:
        return NativeBrokerError("native_pipe_unavailable")
    if code == _ERROR_SEM_TIMEOUT:
        return NativeBrokerError("native_pipe_timeout")
    return NativeBrokerError("native_pipe_io_failed")


def _security_descriptor(
    win: _Win32,
    acl: BrokerAclPolicy,
) -> tuple[ctypes.c_void_p, _WinSecurityAttributes]:
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    if not win.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        build_pipe_sddl(acl),
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise NativeBrokerError("native_acl_unavailable")
    attrs = _WinSecurityAttributes(
        nLength=ctypes.sizeof(_WinSecurityAttributes),
        lpSecurityDescriptor=descriptor,
        bInheritHandle=False,
    )
    return descriptor, attrs


def _token_information(win: _Win32, token: Any, information_class: int) -> Any:
    required = wintypes.DWORD()
    win.advapi32.GetTokenInformation(token, information_class, None, 0, ctypes.byref(required))
    if not required.value or required.value > 64 * 1024:
        raise NativeBrokerError("native_peer_token_invalid")
    buffer = ctypes.create_string_buffer(required.value)
    if not win.advapi32.GetTokenInformation(
        token,
        information_class,
        ctypes.byref(buffer),
        required,
        ctypes.byref(required),
    ):
        raise NativeBrokerError("native_peer_token_unavailable")
    # Keep the backing allocation alive while callers dereference embedded SID
    # pointers. Returning only ``bytes`` would leave those pointers dangling.
    return buffer


def _sid_to_string(win: _Win32, sid: ctypes.c_void_p) -> str:
    sid_pointer = ctypes.c_void_p(getattr(sid, "value", sid))
    if not sid_pointer.value:
        raise NativeBrokerError("native_peer_token_invalid")
    output = ctypes.c_wchar_p()
    if not win.advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(output)):
        raise NativeBrokerError("native_peer_token_invalid")
    try:
        value = output.value
    finally:
        win.kernel32.LocalFree(output)
    if not isinstance(value, str) or not value:
        raise NativeBrokerError("native_peer_token_invalid")
    return value


def _integrity_level(win: _Win32, sid: ctypes.c_void_p) -> IntegrityLevel:
    sid_text = _sid_to_string(win, sid)
    try:
        rid = int(sid_text.rsplit("-", 1)[1])
    except (ValueError, IndexError):
        raise NativeBrokerError("native_peer_token_invalid") from None
    for minimum, level in reversed(_INTEGRITY_RIDS):
        if rid >= minimum:
            return level
    raise NativeBrokerError("native_peer_token_invalid")


def _authorize_principal_and_owner(
    message: BrokerMessage,
    *,
    expected_principal_id: str,
    owner_for_job: Callable[[str], str | None],
) -> None:
    """Validate server responses without inventing an OS identity for the server."""

    if message.installation_principal_id != expected_principal_id:
        raise NativeBrokerError("native_principal_mismatch")
    try:
        owner = owner_for_job(message.job_id)
    except Exception:
        raise NativeBrokerError("native_owner_lookup_failed") from None
    if owner != expected_principal_id:
        raise NativeBrokerError("native_owner_mismatch")


def _read_process_identity(win: _Win32, process: Any, process_id: int) -> PeerIdentity:
    if type(process_id) is not int or process_id <= 0 or _is_invalid_handle(process):
        raise NativeBrokerError("native_peer_identity_unavailable")
    token = wintypes.HANDLE()
    try:
        if not win.advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
            raise NativeBrokerError("native_peer_token_unavailable")
        user_buffer = _token_information(win, token, _TOKEN_USER)
        user = _TokenUser.from_buffer_copy(user_buffer)
        integrity_buffer = _token_information(win, token, _TOKEN_INTEGRITY_LEVEL)
        integrity = _TokenMandatoryLabel.from_buffer_copy(integrity_buffer)
        app_buffer = _token_information(win, token, _TOKEN_APP_CONTAINER_SID)
        app = _TokenAppContainerInformation.from_buffer_copy(app_buffer)
        is_app_buffer = _token_information(win, token, _TOKEN_IS_APP_CONTAINER)
        is_app = wintypes.DWORD.from_buffer_copy(is_app_buffer).value != 0
        app_sid = (
            _sid_to_string(win, app.app_container_sid)
            if is_app and app.app_container_sid
            else None
        )
        if not is_app or app_sid is None:
            app_sid = None
        return PeerIdentity(
            process_id=process_id,
            user_sid=_sid_to_string(win, user.sid),
            app_container_sid=app_sid,
            integrity_level=_integrity_level(win, integrity.sid),
        )
    finally:
        _close_handle(win, token)


def _read_peer_identity(win: _Win32, pipe_handle: Any) -> PeerIdentity:
    client_pid = wintypes.DWORD()
    if not win.kernel32.GetNamedPipeClientProcessId(pipe_handle, ctypes.byref(client_pid)):
        raise NativeBrokerError("native_peer_identity_unavailable")
    process = win.kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        client_pid,
    )
    try:
        return _read_process_identity(win, process, int(client_pid.value))
    finally:
        _close_handle(win, process)


class _PipeIO:
    def __init__(self, win: _Win32, handle: Any) -> None:
        self._win = win
        self._handle = handle

    def read(self, size: int) -> bytes:
        if size <= 0 or size > 256 * 1024:
            raise NativeBrokerError("native_pipe_read_invalid")
        buffer = ctypes.create_string_buffer(size)
        count = wintypes.DWORD()
        if not self._win.kernel32.ReadFile(
            self._handle,
            ctypes.byref(buffer),
            size,
            ctypes.byref(count),
            None,
        ):
            raise _pipe_error(ctypes.get_last_error())
        if count.value == 0:
            raise NativeBrokerError("native_pipe_closed")
        return buffer.raw[: count.value]

    def write(self, payload: bytes) -> None:
        if not isinstance(payload, bytes) or not payload or len(payload) > 256 * 1024:
            raise NativeBrokerError("native_pipe_write_invalid")
        buffer = ctypes.create_string_buffer(payload)
        offset = 0
        while offset < len(payload):
            count = wintypes.DWORD()
            if not self._win.kernel32.WriteFile(
                self._handle,
                ctypes.byref(buffer, offset),
                len(payload) - offset,
                ctypes.byref(count),
                None,
            ):
                raise _pipe_error(ctypes.get_last_error())
            if count.value == 0:
                raise NativeBrokerError("native_pipe_closed")
            offset += count.value


def _read_exact(reader: Callable[[int], bytes], size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = reader(remaining)
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > remaining:
            raise NativeBrokerError("native_handshake_io_invalid")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _handshake_record(kind: int, role: int, payload: bytes) -> bytes:
    if kind not in {_HANDSHAKE_HELLO, _HANDSHAKE_CONFIRM} or role not in {
        _HANDSHAKE_SERVER,
        _HANDSHAKE_CLIENT,
    }:
        raise NativeBrokerError("native_handshake_invalid")
    if not isinstance(payload, bytes) or len(payload) > _HANDSHAKE_MAX_PAYLOAD_BYTES:
        raise NativeBrokerError("native_handshake_invalid")
    return _HANDSHAKE_HEADER.pack(
        _HANDSHAKE_MAGIC,
        _HANDSHAKE_VERSION,
        kind,
        role,
        0,
        len(payload),
    ) + payload


def _read_handshake_record(reader: Callable[[int], bytes]) -> tuple[int, int, bytes, bytes]:
    header = _read_exact(reader, _HANDSHAKE_HEADER.size)
    magic, version, kind, role, reserved, payload_length = _HANDSHAKE_HEADER.unpack(header)
    if (
        magic != _HANDSHAKE_MAGIC
        or version != _HANDSHAKE_VERSION
        or reserved != 0
        or kind not in {_HANDSHAKE_HELLO, _HANDSHAKE_CONFIRM}
        or role not in {_HANDSHAKE_SERVER, _HANDSHAKE_CLIENT}
        or payload_length > _HANDSHAKE_MAX_PAYLOAD_BYTES
    ):
        raise NativeBrokerError("native_handshake_invalid")
    payload = _read_exact(reader, payload_length)
    return kind, role, payload, header + payload


def _hello_payload(pid: int, nonce: bytes, public_key: bytes) -> bytes:
    if type(pid) is not int or not 1 <= pid <= 0xFFFFFFFF:
        raise NativeBrokerError("native_handshake_invalid")
    if len(nonce) != _HANDSHAKE_NONCE_BYTES or len(public_key) != _HANDSHAKE_KEY_BYTES:
        raise NativeBrokerError("native_handshake_invalid")
    return struct.pack(">I", pid) + nonce + public_key


def _parse_hello(payload: bytes) -> tuple[int, bytes, bytes]:
    expected = 4 + _HANDSHAKE_NONCE_BYTES + _HANDSHAKE_KEY_BYTES
    if len(payload) != expected:
        raise NativeBrokerError("native_handshake_invalid")
    pid = struct.unpack(">I", payload[:4])[0]
    return pid, payload[4:36], payload[36:68]


def _derive_keys(
    *,
    server_record: bytes,
    client_record: bytes,
    server_private: X25519PrivateKey,
    client_public: X25519PublicKey,
) -> BrokerSessionKeys:
    try:
        shared = server_private.exchange(client_public)
    except ValueError:
        raise NativeBrokerError("native_handshake_key_failed") from None
    transcript = server_record + client_record
    salt = hashlib.sha256(transcript).digest()
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=2 * _HANDSHAKE_KEY_BYTES,
        salt=salt,
        info=_HANDSHAKE_INFO,
    ).derive(shared)
    return BrokerSessionKeys(to_broker=material[:32], to_executor=material[32:])


def _confirmation(keys: BrokerSessionKeys, transcript: bytes, role: int) -> bytes:
    key = keys.to_executor if role == _HANDSHAKE_SERVER else keys.to_broker
    role_name = b"server" if role == _HANDSHAKE_SERVER else b"client"
    return hmac.new(key, _HANDSHAKE_CONFIRM_INFO + transcript + role_name, hashlib.sha256).digest()


def _server_handshake(
    reader: Callable[[int], bytes],
    writer: Callable[[bytes], None],
    *,
    server_pid: int,
    client_pid: int,
) -> BrokerSessionKeys:
    server_private = X25519PrivateKey.generate()
    server_record = _handshake_record(
        _HANDSHAKE_HELLO,
        _HANDSHAKE_SERVER,
        _hello_payload(
            server_pid,
            secrets.token_bytes(_HANDSHAKE_NONCE_BYTES),
            server_private.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            ),
        ),
    )
    writer(server_record)
    kind, role, client_payload, client_record = _read_handshake_record(reader)
    if kind != _HANDSHAKE_HELLO or role != _HANDSHAKE_CLIENT:
        raise NativeBrokerError("native_handshake_invalid")
    announced_pid, _client_nonce, client_public_bytes = _parse_hello(client_payload)
    if announced_pid != client_pid:
        raise NativeBrokerError("native_peer_identity_mismatch")
    try:
        client_public = X25519PublicKey.from_public_bytes(client_public_bytes)
    except ValueError:
        raise NativeBrokerError("native_handshake_key_failed") from None
    keys = _derive_keys(
        server_record=server_record,
        client_record=client_record,
        server_private=server_private,
        client_public=client_public,
    )
    transcript = server_record + client_record
    writer(
        _handshake_record(
            _HANDSHAKE_CONFIRM,
            _HANDSHAKE_SERVER,
            _confirmation(keys, transcript, _HANDSHAKE_SERVER),
        )
    )
    kind, role, payload, _record = _read_handshake_record(reader)
    if (
        kind != _HANDSHAKE_CONFIRM
        or role != _HANDSHAKE_CLIENT
        or not hmac.compare_digest(payload, _confirmation(keys, transcript, _HANDSHAKE_CLIENT))
    ):
        raise NativeBrokerError("native_handshake_confirmation_failed")
    return keys


def _client_handshake(
    reader: Callable[[int], bytes],
    writer: Callable[[bytes], None],
    *,
    expected_server_pid: int,
    client_pid: int,
) -> BrokerSessionKeys:
    kind, role, server_payload, server_record = _read_handshake_record(reader)
    if kind != _HANDSHAKE_HELLO or role != _HANDSHAKE_SERVER:
        raise NativeBrokerError("native_handshake_invalid")
    announced_server_pid, _server_nonce, server_public_bytes = _parse_hello(server_payload)
    if announced_server_pid != expected_server_pid:
        raise NativeBrokerError("native_peer_identity_mismatch")
    try:
        server_public = X25519PublicKey.from_public_bytes(server_public_bytes)
    except ValueError:
        raise NativeBrokerError("native_handshake_key_failed") from None
    client_private = X25519PrivateKey.generate()
    client_record = _handshake_record(
        _HANDSHAKE_HELLO,
        _HANDSHAKE_CLIENT,
        _hello_payload(
            client_pid,
            secrets.token_bytes(_HANDSHAKE_NONCE_BYTES),
            client_private.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            ),
        ),
    )
    writer(client_record)
    keys = _derive_keys(
        server_record=server_record,
        client_record=client_record,
        server_private=client_private,
        client_public=server_public,
    )
    transcript = server_record + client_record
    kind, role, payload, _record = _read_handshake_record(reader)
    if (
        kind != _HANDSHAKE_CONFIRM
        or role != _HANDSHAKE_SERVER
        or not hmac.compare_digest(payload, _confirmation(keys, transcript, _HANDSHAKE_SERVER))
    ):
        raise NativeBrokerError("native_handshake_confirmation_failed")
    writer(
        _handshake_record(
            _HANDSHAKE_CONFIRM,
            _HANDSHAKE_CLIENT,
            _confirmation(keys, transcript, _HANDSHAKE_CLIENT),
        )
    )
    return keys


Authorization = Callable[[BrokerMessage], None]


class NativeBrokerConnection:
    """Authenticated framed connection; close on every protocol failure."""

    def __init__(
        self,
        pipe: _PipeIO,
        *,
        peer: PeerIdentity | None,
        keys: BrokerSessionKeys,
        incoming_direction: BrokerDirection,
        outgoing_direction: BrokerDirection,
        authorization: Authorization,
        close_pipe: Callable[[], None],
    ) -> None:
        self.peer = peer
        self._pipe = pipe
        self._keys = keys
        self._incoming_direction = incoming_direction
        self._outgoing_direction = outgoing_direction
        self._authorization = authorization
        self._close_pipe = close_pipe
        self._decoder = BrokerFrameDecoder(key=keys.for_direction(incoming_direction))
        self._pending: deque[BrokerFrame] = deque()
        self._next_sequence = 1
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise NativeBrokerError("native_connection_closed")

    def send_message(self, message: BrokerMessage) -> None:
        self._ensure_open()
        if message.direction != self._outgoing_direction:
            self.close()
            raise NativeBrokerError("native_message_direction_invalid")
        try:
            encoded = encode_message(message, keys=self._keys, sequence=self._next_sequence)
            self._pipe.write(encoded)
            self._next_sequence += 1
        except (BrokerProtocolError, NativeBrokerError):
            self.close()
            raise
        except Exception:
            self.close()
            raise NativeBrokerError("native_pipe_write_failed") from None

    def receive_message(self) -> BrokerMessage:
        self._ensure_open()
        try:
            while not self._pending:
                self._pending.extend(self._decoder.feed(self._pipe.read(64 * 1024)))
            message = decode_message(self._pending.popleft(), direction=self._incoming_direction)
            self._authorization(message)
            return message
        except (BrokerProtocolError, NativeBrokerError):
            self.close()
            raise
        except Exception:
            self.close()
            raise NativeBrokerError("native_message_rejected") from None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._close_pipe()

    def __enter__(self) -> "NativeBrokerConnection":
        self._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class NativeBrokerServer:
    """Single-instance local named-pipe server with mandatory peer binding."""

    def __init__(self, config: NativeBrokerServerConfig) -> None:
        self.config = config
        self._win: _Win32 | None = None
        self._handle: Any = None
        self._closed = False

    def open(self) -> None:
        if self._handle is not None:
            raise NativeBrokerError("native_server_already_open")
        win = _load_win32()
        descriptor, attrs = _security_descriptor(win, self.config.peer_policy.acl)
        handle = win.kernel32.CreateNamedPipeW(
            self.config.pipe_name,
            _PIPE_ACCESS_DUPLEX | _FILE_FLAG_FIRST_PIPE_INSTANCE,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
            1,
            self.config.pipe_buffer_bytes,
            self.config.pipe_buffer_bytes,
            0,
            ctypes.byref(attrs),
        )
        win.kernel32.LocalFree(descriptor)
        if _is_invalid_handle(handle):
            raise NativeBrokerError("native_pipe_create_failed")
        self._win = win
        self._handle = handle
        self._closed = False

    def accept(
        self,
        *,
        expected_principal_id: str,
        owner_for_job: Callable[[str], str | None],
    ) -> NativeBrokerConnection:
        if self._handle is None or self._win is None or self._closed:
            raise NativeBrokerError("native_server_not_open")
        handle = self._handle
        win = self._win
        self._handle = None
        try:
            connected = win.kernel32.ConnectNamedPipe(handle, None)
            if not connected and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                raise NativeBrokerError("native_pipe_connect_failed")
            peer = _read_peer_identity(win, handle)
            self.config.peer_policy.validate(peer)
            keys = _server_handshake(
                _PipeIO(win, handle).read,
                _PipeIO(win, handle).write,
                server_pid=os.getpid(),
                client_pid=peer.process_id,
            )
            authorization = lambda message: authorize_message(
                message,
                peer=peer,
                peer_policy=self.config.peer_policy,
                expected_principal_id=expected_principal_id,
                owner_for_job=owner_for_job,
            )
            pipe = _PipeIO(win, handle)
            return NativeBrokerConnection(
                pipe,
                peer=peer,
                keys=keys,
                incoming_direction="to_broker",
                outgoing_direction="to_executor",
                authorization=authorization,
                close_pipe=lambda: self._close_connected(win, handle),
            )
        except (BrokerProtocolError, NativeBrokerError):
            self._close_connected(win, handle)
            raise
        except Exception:
            self._close_connected(win, handle)
            raise NativeBrokerError("native_accept_failed") from None

    @staticmethod
    def _close_connected(win: _Win32, handle: Any) -> None:
        win.kernel32.DisconnectNamedPipe(handle)
        _close_handle(win, handle)

    def close(self) -> None:
        if self._handle is not None and self._win is not None:
            self._close_connected(self._win, self._handle)
            self._handle = None
        self._closed = True

    def __enter__(self) -> "NativeBrokerServer":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class NativeBrokerClient:
    """Named-pipe client used only for the reviewed broker handshake and framing."""

    def __init__(self, config: NativeBrokerClientConfig) -> None:
        self.config = config

    def connect(
        self,
        *,
        expected_principal_id: str,
        owner_for_job: Callable[[str], str | None],
    ) -> NativeBrokerConnection:
        win = _load_win32()
        if not win.kernel32.WaitNamedPipeW(self.config.pipe_name, self.config.connect_timeout_ms):
            raise _pipe_error(ctypes.get_last_error())
        handle = win.kernel32.CreateFileW(
            self.config.pipe_name,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            _SECURITY_SQOS_PRESENT | _SECURITY_IDENTIFICATION,
            None,
        )
        if _is_invalid_handle(handle):
            raise _pipe_error(ctypes.get_last_error())
        try:
            server_pid = wintypes.DWORD()
            if not win.kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(server_pid)):
                raise NativeBrokerError("native_peer_identity_unavailable")
            if server_pid.value != self.config.expected_server_process_id:
                raise NativeBrokerError("native_peer_identity_mismatch")
            pipe = _PipeIO(win, handle)
            keys = _client_handshake(
                pipe.read,
                pipe.write,
                expected_server_pid=self.config.expected_server_process_id,
                client_pid=os.getpid(),
            )
            # The client cannot safely invent a server token identity. The pipe
            # DACL and expected server PID bind the transport; responses are then
            # bound to the trusted installation/job without a fake PeerIdentity.
            authorization = lambda message: _authorize_principal_and_owner(
                message,
                expected_principal_id=expected_principal_id,
                owner_for_job=owner_for_job,
            )
            return NativeBrokerConnection(
                pipe,
                peer=None,
                keys=keys,
                incoming_direction="to_executor",
                outgoing_direction="to_broker",
                authorization=authorization,
                close_pipe=lambda: _close_handle(win, handle),
            )
        except (BrokerProtocolError, NativeBrokerError):
            _close_handle(win, handle)
            raise
        except Exception:
            _close_handle(win, handle)
            raise NativeBrokerError("native_connect_failed") from None


__all__ = [
    "DEFAULT_NATIVE_CONNECT_TIMEOUT_MS",
    "DEFAULT_NATIVE_PIPE_BUFFER_BYTES",
    "MAX_NATIVE_PIPE_NAME_LENGTH",
    "NativeBrokerClient",
    "NativeBrokerClientConfig",
    "NativeBrokerConnection",
    "NativeBrokerError",
    "NativeBrokerServer",
    "NativeBrokerServerConfig",
    "build_pipe_sddl",
]
