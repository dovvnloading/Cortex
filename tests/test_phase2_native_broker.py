"""Native broker policy, handshake, and Windows adapter boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
import os
import queue
import secrets
import sys
import threading

import pytest

from cortex_backend.execution.broker import (
    BrokerAclPolicy,
    BrokerMessage,
    BrokerPeerPolicy,
    BrokerSessionKeys,
)
from cortex_backend.execution.native_broker import (
    NativeBrokerClientConfig,
    NativeBrokerConnection,
    NativeBrokerError,
    NativeBrokerServer,
    NativeBrokerServerConfig,
    _HANDSHAKE_CONFIRM,
    _HANDSHAKE_CLIENT,
    _HANDSHAKE_HEADER,
    _HANDSHAKE_HELLO,
    _HANDSHAKE_SERVER,
    _PROCESS_QUERY_LIMITED_INFORMATION,
    _close_handle,
    _client_handshake,
    _handshake_record,
    _hello_payload,
    _load_win32,
    _read_handshake_record,
    _read_process_identity,
    _server_handshake,
    build_pipe_sddl,
)


USER_SID = "S-1-5-21-100-200-300-400"
APP_SID = "S-1-15-2-100-200-300-400"


def _policy() -> BrokerPeerPolicy:
    return BrokerPeerPolicy(
        acl=BrokerAclPolicy(
            allowed_user_sids=frozenset({USER_SID}),
            allowed_app_container_sids=frozenset({APP_SID}),
        ),
        expected_process_id=222,
        maximum_integrity="low",
    )


@dataclass
class _MemoryDuplex:
    server_to_client: queue.Queue[bytes]
    client_to_server: queue.Queue[bytes]

    @classmethod
    def create(cls) -> "_MemoryDuplex":
        return cls(queue.Queue(), queue.Queue())

    def endpoint(self, role: str) -> "_MemoryEndpoint":
        if role == "server":
            return _MemoryEndpoint(self.client_to_server, self.server_to_client)
        return _MemoryEndpoint(self.server_to_client, self.client_to_server)


class _MemoryEndpoint:
    def __init__(self, incoming: queue.Queue[bytes], outgoing: queue.Queue[bytes]) -> None:
        self._incoming = incoming
        self._outgoing = outgoing

    def read(self, size: int) -> bytes:
        payload = self._incoming.get(timeout=3)
        if len(payload) > size:
            self._incoming.put(payload[size:])
            return payload[:size]
        return payload

    def write(self, payload: bytes) -> None:
        self._outgoing.put(payload)


def test_pipe_sddl_is_protected_and_contains_only_configured_sids():
    sddl = build_pipe_sddl(_policy().acl)
    assert sddl.startswith("D:P")
    assert f"(A;;GA;;;{APP_SID})" in sddl
    assert f"(A;;GA;;;{USER_SID})" in sddl
    assert "WD" not in sddl
    assert "AN" not in sddl
    assert "AU" not in sddl


def test_x25519_handshake_derives_identical_directional_keys():
    duplex = _MemoryDuplex.create()
    server_endpoint = duplex.endpoint("server")
    client_endpoint = duplex.endpoint("client")
    result: dict[str, object] = {}

    def run_server() -> None:
        try:
            result["server"] = _server_handshake(
                server_endpoint.read,
                server_endpoint.write,
                server_pid=111,
                client_pid=222,
            )
        except BaseException as exc:  # pragma: no cover - assertion reports the error
            result["server_error"] = exc

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    result["client"] = _client_handshake(
        client_endpoint.read,
        client_endpoint.write,
        expected_server_pid=111,
        client_pid=222,
    )
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert "server_error" not in result
    assert result["server"] == result["client"]
    keys = result["client"]
    assert keys.to_broker != keys.to_executor


def test_handshake_rejects_malformed_record_without_details():
    malformed = _HANDSHAKE_HEADER.pack(b"NOPE", 1, _HANDSHAKE_CONFIRM, _HANDSHAKE_SERVER, 0, 0)

    def reader(size: int) -> bytes:
        return malformed[:size]

    with pytest.raises(NativeBrokerError) as error:
        _read_handshake_record(reader)
    assert error.value.code == "native_handshake_invalid"
    assert "NOPE" not in str(error.value)


def test_client_rejects_unexpected_server_process_before_key_use():
    server_record = _handshake_record(
        _HANDSHAKE_HELLO,
        _HANDSHAKE_SERVER,
        _hello_payload(999, secrets.token_bytes(32), secrets.token_bytes(32)),
    )
    offset = 0

    def reader(size: int) -> bytes:
        nonlocal offset
        chunk = server_record[offset : offset + size]
        offset += len(chunk)
        return chunk

    with pytest.raises(NativeBrokerError) as error:
        _client_handshake(
            reader,
            lambda _payload: None,
            expected_server_pid=111,
            client_pid=222,
        )
    assert error.value.code == "native_peer_identity_mismatch"


def test_server_rejects_bad_client_confirmation_and_does_not_return_keys():
    client_record = _handshake_record(
        _HANDSHAKE_HELLO,
        _HANDSHAKE_CLIENT,
        _hello_payload(222, secrets.token_bytes(32), secrets.token_bytes(32)),
    )
    bad_confirmation = _handshake_record(
        _HANDSHAKE_CONFIRM,
        _HANDSHAKE_CLIENT,
        b"x" * 32,
    )
    stream = client_record + bad_confirmation
    offset = 0

    def reader(size: int) -> bytes:
        nonlocal offset
        chunk = stream[offset : offset + size]
        offset += len(chunk)
        return chunk

    with pytest.raises(NativeBrokerError) as error:
        _server_handshake(
            reader,
            lambda _payload: None,
            server_pid=111,
            client_pid=222,
        )
    assert error.value.code == "native_handshake_confirmation_failed"


def test_connection_closes_on_direction_violation():
    closed: list[bool] = []

    class FakePipe:
        def read(self, _size: int) -> bytes:
            raise AssertionError("direction validation should run before reading")

        def write(self, _payload: bytes) -> None:
            raise AssertionError("direction validation should run before writing")

    connection = NativeBrokerConnection(
        FakePipe(),
        peer=None,
        keys=BrokerSessionKeys(b"b" * 32, b"e" * 32),
        incoming_direction="to_broker",
        outgoing_direction="to_executor",
        authorization=lambda _message: None,
        close_pipe=lambda: closed.append(True),
    )
    message = BrokerMessage(
        schema_version="broker.message.v1",
        direction="to_broker",
        operation="start",
        request_id="request_1",
        job_id="job_1",
        installation_principal_id="a" * 64,
        body={"profile": "artifact.transform.v1"},
    )

    with pytest.raises(NativeBrokerError) as error:
        connection.send_message(message)
    assert error.value.code == "native_message_direction_invalid"
    assert closed == [True]


def test_native_configs_require_local_pipe_and_expected_process_binding():
    with pytest.raises(ValueError):
        NativeBrokerServerConfig(r"\\.\pipe\..\unsafe", _policy())
    with pytest.raises(ValueError):
        NativeBrokerServerConfig(r"\\.\pipe\cortex-test", BrokerPeerPolicy(acl=_policy().acl))
    with pytest.raises(ValueError):
        NativeBrokerClientConfig(r"\\.\pipe\cortex-test", expected_server_process_id=0)


@pytest.mark.skipif(sys.platform != "win32", reason="native named pipes require Windows")
def test_native_server_can_create_and_close_a_protected_pipe():
    config = NativeBrokerServerConfig(
        pipe_name=rf"\\.\pipe\cortex-test-{os.getpid()}",
        peer_policy=_policy(),
    )
    server = NativeBrokerServer(config)
    server.open()
    server.close()


@pytest.mark.skipif(sys.platform != "win32", reason="token inspection requires Windows")
def test_native_peer_identity_reads_os_token_without_inheriting_handles():
    win = _load_win32()
    process = win.kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        os.getpid(),
    )
    try:
        identity = _read_process_identity(win, process, os.getpid())
    finally:
        _close_handle(win, process)
    assert identity.process_id == os.getpid()
    assert identity.user_sid.startswith("S-1-")
    assert identity.integrity_level in {"low", "medium", "high", "system"}
    assert identity.app_container_sid is None


def test_non_windows_native_operations_fail_closed():
    if sys.platform == "win32":
        pytest.skip("non-Windows behavior is not applicable on Windows")
    server = NativeBrokerServer(
        NativeBrokerServerConfig(r"\\.\pipe\cortex-test", _policy())
    )
    with pytest.raises(NativeBrokerError) as error:
        server.open()
    assert error.value.code == "native_windows_required"
