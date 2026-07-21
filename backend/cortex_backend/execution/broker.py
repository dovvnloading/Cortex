"""Authenticated, bounded broker contract for a future local executor.

The broker contract is transport-neutral.  It validates framed messages, direction-
specific MAC keys, OS-provided peer identity, and installation/job ownership before a
future named-pipe adapter may dispatch anything.  No socket, pipe, process, or
provider is opened by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import re
from struct import Struct, error as StructError
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


MAX_BROKER_PAYLOAD_BYTES = 64 * 1024
MAX_BROKER_CHUNK_BYTES = 256 * 1024
MAX_BROKER_BODY_KEYS = 32
_MAGIC = b"CXBF"
_VERSION = 1
_TAG_BYTES = hashlib.sha256().digest_size
_HEADER = Struct(">4sBBHQI")
_MAX_SEQUENCE = (1 << 63) - 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PRINCIPAL = re.compile(r"^[0-9a-f]{64}$")
_SID = re.compile(r"^S-1-[0-9]+(?:-[0-9]+)+$")
_FORBIDDEN_BODY_KEYS = frozenset(
    {"path", "source", "command", "shell", "executable", "network", "token"}
)

BrokerDirection = Literal["to_broker", "to_executor"]
BrokerOperation = Literal["prepare", "start", "cancel", "collect"]
IntegrityLevel = Literal["low", "medium", "high", "system"]


class BrokerProtocolError(ValueError):
    """Stable, non-sensitive protocol failure category."""

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            raise ValueError("invalid broker protocol code")
        self.code = code
        super().__init__("The local execution broker rejected the message.")


@dataclass(frozen=True, slots=True)
class BrokerSessionKeys:
    """Independent MAC keys prevent valid frames being reflected cross-direction."""

    to_broker: bytes
    to_executor: bytes

    def __post_init__(self) -> None:
        if (
            not isinstance(self.to_broker, bytes)
            or not isinstance(self.to_executor, bytes)
            or len(self.to_broker) < 32
            or len(self.to_executor) < 32
            or hmac.compare_digest(self.to_broker, self.to_executor)
        ):
            raise ValueError("broker direction keys must be distinct 32-byte values")

    def for_direction(self, direction: BrokerDirection) -> bytes:
        return self.to_broker if direction == "to_broker" else self.to_executor


@dataclass(frozen=True, slots=True)
class BrokerFrame:
    sequence: int
    payload: bytes


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise BrokerProtocolError("frame_key_invalid")


def _validate_sequence(sequence: int) -> None:
    if type(sequence) is not int or not 1 <= sequence <= _MAX_SEQUENCE:
        raise BrokerProtocolError("frame_sequence_invalid")


def encode_frame(payload: bytes, *, sequence: int, key: bytes) -> bytes:
    _validate_key(key)
    _validate_sequence(sequence)
    if not isinstance(payload, bytes) or len(payload) > MAX_BROKER_PAYLOAD_BYTES:
        raise BrokerProtocolError("frame_payload_too_large")
    header = _HEADER.pack(_MAGIC, _VERSION, 0, 0, sequence, len(payload))
    tag = hmac.new(key, header + payload, hashlib.sha256).digest()
    return header + payload + tag


def decode_frame(
    encoded: bytes,
    *,
    key: bytes,
    expected_sequence: int | None = None,
) -> BrokerFrame:
    _validate_key(key)
    if not isinstance(encoded, bytes) or len(encoded) < _HEADER.size + _TAG_BYTES:
        raise BrokerProtocolError("frame_truncated")
    if expected_sequence is not None:
        _validate_sequence(expected_sequence)
    try:
        magic, version, flags, reserved, sequence, payload_length = _HEADER.unpack_from(encoded)
    except StructError:
        raise BrokerProtocolError("frame_invalid") from None
    if magic != _MAGIC or version != _VERSION or flags != 0 or reserved != 0:
        raise BrokerProtocolError("frame_header_invalid")
    _validate_sequence(sequence)
    if payload_length > MAX_BROKER_PAYLOAD_BYTES:
        raise BrokerProtocolError("frame_payload_too_large")
    expected_length = _HEADER.size + payload_length + _TAG_BYTES
    if len(encoded) != expected_length:
        raise BrokerProtocolError("frame_length_invalid")
    if expected_sequence is not None and sequence != expected_sequence:
        raise BrokerProtocolError("frame_replay")
    payload_end = _HEADER.size + payload_length
    expected_tag = hmac.new(key, encoded[:payload_end], hashlib.sha256).digest()
    if not hmac.compare_digest(expected_tag, encoded[payload_end:]):
        raise BrokerProtocolError("frame_authentication_failed")
    return BrokerFrame(sequence=sequence, payload=encoded[_HEADER.size:payload_end])


class BrokerFrameDecoder:
    """Incremental decoder that bounds buffering and enforces strict sequencing."""

    def __init__(self, *, key: bytes, first_sequence: int = 1) -> None:
        _validate_key(key)
        _validate_sequence(first_sequence)
        self._key = key
        self._next_sequence = first_sequence
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> tuple[BrokerFrame, ...]:
        if not isinstance(chunk, bytes) or len(chunk) > MAX_BROKER_CHUNK_BYTES:
            raise BrokerProtocolError("frame_chunk_too_large")
        self._buffer.extend(chunk)
        if len(self._buffer) > MAX_BROKER_CHUNK_BYTES + _HEADER.size + _TAG_BYTES:
            raise BrokerProtocolError("frame_buffer_too_large")
        frames: list[BrokerFrame] = []
        while len(self._buffer) >= _HEADER.size:
            try:
                magic, version, flags, reserved, sequence, payload_length = _HEADER.unpack_from(
                    self._buffer
                )
            except StructError:
                raise BrokerProtocolError("frame_invalid") from None
            if magic != _MAGIC or version != _VERSION or flags != 0 or reserved != 0:
                raise BrokerProtocolError("frame_header_invalid")
            _validate_sequence(sequence)
            if payload_length > MAX_BROKER_PAYLOAD_BYTES:
                raise BrokerProtocolError("frame_payload_too_large")
            frame_length = _HEADER.size + payload_length + _TAG_BYTES
            if len(self._buffer) < frame_length:
                break
            raw = bytes(self._buffer[:frame_length])
            del self._buffer[:frame_length]
            frame = decode_frame(
                raw,
                key=self._key,
                expected_sequence=self._next_sequence,
            )
            self._next_sequence += 1
            frames.append(frame)
        return tuple(frames)


class BrokerMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["broker.message.v1"]
    direction: BrokerDirection
    operation: BrokerOperation
    request_id: str
    job_id: str
    installation_principal_id: str
    body: dict[str, Any] = Field(max_length=MAX_BROKER_BODY_KEYS)

    @field_validator("request_id", "job_id")
    @classmethod
    def _safe_ids(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("broker id is invalid")
        return value

    @field_validator("installation_principal_id")
    @classmethod
    def _principal(cls, value: str) -> str:
        if _PRINCIPAL.fullmatch(value) is None:
            raise ValueError("broker principal is invalid")
        return value

    @field_validator("body")
    @classmethod
    def _safe_body(cls, value: dict[str, Any]) -> dict[str, Any]:
        def is_invalid(value: Any, depth: int = 0, seen: set[int] | None = None) -> bool:
            if depth > 8:
                return True
            if seen is None:
                seen = set()
            if isinstance(value, Mapping):
                marker = id(value)
                if marker in seen:
                    return True
                seen.add(marker)
                try:
                    return any(
                        not isinstance(key, str)
                        or key.casefold() in _FORBIDDEN_BODY_KEYS
                        or is_invalid(item, depth + 1, seen)
                        for key, item in value.items()
                    )
                finally:
                    seen.remove(marker)
            if isinstance(value, (list, tuple)):
                marker = id(value)
                if marker in seen:
                    return True
                seen.add(marker)
                try:
                    return any(is_invalid(item, depth + 1, seen) for item in value)
                finally:
                    seen.remove(marker)
            if value is None or isinstance(value, (str, bool, int)):
                return False
            if isinstance(value, float):
                return not math.isfinite(value)
            return True

        if is_invalid(value):
            raise ValueError("broker body contains an invalid or forbidden value")
        return value

    def canonical_json(self) -> bytes:
        # Pydantic's frozen model does not deep-freeze nested dictionaries. Revalidate
        # before serialization so a caller cannot mutate the body after construction
        # and smuggle authority fields or non-JSON values into a signed frame.
        type(self).model_validate(self.model_dump(mode="python"))
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")


def encode_message(message: BrokerMessage, *, keys: BrokerSessionKeys, sequence: int) -> bytes:
    try:
        payload = message.canonical_json()
    except (TypeError, ValueError, ValidationError):
        raise BrokerProtocolError("message_invalid") from None
    return encode_frame(payload, sequence=sequence, key=keys.for_direction(message.direction))


def decode_message(
    frame: BrokerFrame,
    *,
    direction: BrokerDirection,
) -> BrokerMessage:
    try:
        payload = json.loads(frame.payload.decode("ascii"))
        message = BrokerMessage.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise BrokerProtocolError("message_invalid") from None
    try:
        canonical_payload = message.canonical_json()
    except (TypeError, ValueError, ValidationError):
        raise BrokerProtocolError("message_invalid") from None
    if message.direction != direction or canonical_payload != frame.payload:
        raise BrokerProtocolError("message_noncanonical")
    return message


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    process_id: int
    user_sid: str
    app_container_sid: str | None
    integrity_level: IntegrityLevel

    def __post_init__(self) -> None:
        if (
            type(self.process_id) is not int
            or self.process_id <= 0
            or not isinstance(self.user_sid, str)
            or _SID.fullmatch(self.user_sid) is None
        ):
            raise ValueError("peer identity is invalid")
        if self.app_container_sid is not None and (
            not isinstance(self.app_container_sid, str)
            or _SID.fullmatch(self.app_container_sid) is None
        ):
            raise ValueError("peer app-container identity is invalid")
        if not isinstance(self.integrity_level, str) or self.integrity_level not in {
            "low",
            "medium",
            "high",
            "system",
        }:
            raise ValueError("peer integrity level is invalid")


@dataclass(frozen=True, slots=True)
class BrokerAclPolicy:
    """Allowlist that a native named-pipe adapter must apply to its DACL."""

    allowed_user_sids: frozenset[str]
    allowed_app_container_sids: frozenset[str]

    def __post_init__(self) -> None:
        if not self.allowed_user_sids or not self.allowed_app_container_sids:
            raise ValueError("broker ACL must allow user and app-container SIDs")
        if any(
            not isinstance(sid, str) or _SID.fullmatch(sid) is None
            for sid in self.allowed_user_sids
        ):
            raise ValueError("broker ACL user SID is invalid")
        if any(
            not isinstance(sid, str) or _SID.fullmatch(sid) is None
            for sid in self.allowed_app_container_sids
        ):
            raise ValueError("broker ACL app-container SID is invalid")


@dataclass(frozen=True, slots=True)
class BrokerPeerPolicy:
    acl: BrokerAclPolicy
    expected_process_id: int | None = None
    maximum_integrity: IntegrityLevel = "low"

    def __post_init__(self) -> None:
        if self.expected_process_id is not None and (
            type(self.expected_process_id) is not int or self.expected_process_id <= 0
        ):
            raise ValueError("expected broker process ID is invalid")
        if not isinstance(self.maximum_integrity, str) or self.maximum_integrity not in {
            "low",
            "medium",
            "high",
            "system",
        }:
            raise ValueError("broker integrity policy is invalid")

    def validate(self, identity: PeerIdentity) -> None:
        if self.expected_process_id is not None and identity.process_id != self.expected_process_id:
            raise BrokerProtocolError("peer_identity_mismatch")
        if identity.user_sid not in self.acl.allowed_user_sids:
            raise BrokerProtocolError("peer_acl_denied")
        if (
            self.acl.allowed_app_container_sids
            and identity.app_container_sid not in self.acl.allowed_app_container_sids
        ):
            raise BrokerProtocolError("peer_acl_denied")
        levels = {"low": 0, "medium": 1, "high": 2, "system": 3}
        if self.maximum_integrity not in levels:
            raise BrokerProtocolError("peer_integrity_policy_invalid")
        if levels[identity.integrity_level] > levels[self.maximum_integrity]:
            raise BrokerProtocolError("peer_integrity_denied")


def authorize_message(
    message: BrokerMessage,
    *,
    peer: PeerIdentity,
    peer_policy: BrokerPeerPolicy,
    expected_principal_id: str,
    owner_for_job: Callable[[str], str | None],
) -> None:
    """Bind a verified frame to the trusted installation and job owner."""
    if (
        not isinstance(expected_principal_id, str)
        or _PRINCIPAL.fullmatch(expected_principal_id) is None
    ):
        raise BrokerProtocolError("broker_principal_invalid")
    peer_policy.validate(peer)
    if message.installation_principal_id != expected_principal_id:
        raise BrokerProtocolError("broker_principal_mismatch")
    try:
        owner = owner_for_job(message.job_id)
    except Exception:
        raise BrokerProtocolError("broker_owner_lookup_failed") from None
    if owner != expected_principal_id:
        raise BrokerProtocolError("broker_owner_mismatch")


__all__ = [
    "BrokerAclPolicy",
    "BrokerDirection",
    "BrokerFrame",
    "BrokerFrameDecoder",
    "BrokerMessage",
    "BrokerOperation",
    "BrokerPeerPolicy",
    "BrokerProtocolError",
    "BrokerSessionKeys",
    "IntegrityLevel",
    "MAX_BROKER_CHUNK_BYTES",
    "MAX_BROKER_PAYLOAD_BYTES",
    "PeerIdentity",
    "authorize_message",
    "decode_frame",
    "decode_message",
    "encode_frame",
    "encode_message",
]
