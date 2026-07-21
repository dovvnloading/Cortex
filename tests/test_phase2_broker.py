"""Authenticated broker framing, ACL, and confused-deputy tests."""

from __future__ import annotations

import json

import pytest

from cortex_backend.execution.broker import (
    MAX_BROKER_PAYLOAD_BYTES,
    BrokerAclPolicy,
    BrokerFrameDecoder,
    BrokerMessage,
    BrokerPeerPolicy,
    BrokerProtocolError,
    BrokerSessionKeys,
    PeerIdentity,
    authorize_message,
    decode_frame,
    decode_message,
    encode_frame,
    encode_message,
)


PRINCIPAL = "a" * 64
USER_SID = "S-1-5-21-100-200-300-400"
APP_SID = "S-1-15-2-100-200-300-400"


def _keys() -> BrokerSessionKeys:
    return BrokerSessionKeys(b"b" * 32, b"e" * 32)


def _message(*, principal: str = PRINCIPAL, job_id: str = "job_1") -> BrokerMessage:
    return BrokerMessage(
        schema_version="broker.message.v1",
        direction="to_broker",
        operation="start",
        request_id="request_1",
        job_id=job_id,
        installation_principal_id=principal,
        body={"profile": "artifact.transform.v1"},
    )


def _peer(*, process_id: int = 123, user_sid: str = USER_SID, integrity_level="low"):
    return PeerIdentity(
        process_id=process_id,
        user_sid=user_sid,
        app_container_sid=APP_SID,
        integrity_level=integrity_level,
    )


def _policy(*, process_id: int | None = 123, maximum_integrity="low"):
    return BrokerPeerPolicy(
        acl=BrokerAclPolicy(
            allowed_user_sids=frozenset({USER_SID}),
            allowed_app_container_sids=frozenset({APP_SID}),
        ),
        expected_process_id=process_id,
        maximum_integrity=maximum_integrity,
    )


def test_frame_mac_length_and_sequence_are_bounded_and_replay_safe():
    keys = _keys()
    encoded = encode_frame(b"hello", sequence=1, key=keys.to_broker)
    assert decode_frame(encoded, key=keys.to_broker, expected_sequence=1).payload == b"hello"

    tampered = bytearray(encoded)
    tampered[-1] ^= 1
    with pytest.raises(BrokerProtocolError) as auth_error:
        decode_frame(bytes(tampered), key=keys.to_broker, expected_sequence=1)
    assert auth_error.value.code == "frame_authentication_failed"

    with pytest.raises(BrokerProtocolError) as replay_error:
        decode_frame(encoded, key=keys.to_broker, expected_sequence=2)
    assert replay_error.value.code == "frame_replay"

    with pytest.raises(BrokerProtocolError) as size_error:
        encode_frame(b"x" * (MAX_BROKER_PAYLOAD_BYTES + 1), sequence=1, key=keys.to_broker)
    assert size_error.value.code == "frame_payload_too_large"


def test_malformed_headers_and_transport_chunks_fail_closed():
    key = b"b" * 32
    encoded = encode_frame(b"hello", sequence=1, key=key)

    with pytest.raises(BrokerProtocolError) as truncated_error:
        decode_frame(b"", key=key)
    assert truncated_error.value.code == "frame_truncated"

    malformed = bytearray(encoded)
    malformed[4] = 2
    with pytest.raises(BrokerProtocolError) as header_error:
        decode_frame(bytes(malformed), key=key)
    assert header_error.value.code == "frame_header_invalid"

    with pytest.raises(BrokerProtocolError) as chunk_error:
        BrokerFrameDecoder(key=key).feed(b"x" * (256 * 1024 + 1))
    assert chunk_error.value.code == "frame_chunk_too_large"


def test_key_body_and_identity_constructors_are_strictly_bounded():
    with pytest.raises(ValueError):
        BrokerSessionKeys(b"same" * 8, b"same" * 8)
    with pytest.raises(BrokerProtocolError) as key_error:
        decode_frame(b"x" * 52, key=b"short")
    assert key_error.value.code == "frame_key_invalid"

    with pytest.raises(ValueError):
        BrokerMessage(
            schema_version="broker.message.v1",
            direction="to_broker",
            operation="start",
            request_id="request_1",
            job_id="job_1",
            installation_principal_id=PRINCIPAL,
            body={f"k{i}": i for i in range(33)},
        )
    with pytest.raises(ValueError):
        BrokerMessage(
            schema_version="broker.message.v1",
            direction="to_broker",
            operation="start",
            request_id="request_1",
            job_id="job_1",
            installation_principal_id=PRINCIPAL,
            body={"value": float("nan")},
        )
    with pytest.raises(ValueError):
        PeerIdentity(
            process_id=True,
            user_sid=USER_SID,
            app_container_sid=APP_SID,
            integrity_level="low",
        )
    with pytest.raises(ValueError):
        BrokerPeerPolicy(acl=_policy().acl, expected_process_id=0)


def test_incremental_decoder_handles_split_frames_and_rejects_direction_reflection():
    keys = _keys()
    first = encode_frame(b"one", sequence=1, key=keys.to_broker)
    second = encode_frame(b"two", sequence=2, key=keys.to_broker)
    decoder = BrokerFrameDecoder(key=keys.to_broker)

    assert decoder.feed(first[:7]) == ()
    assert decoder.feed(first[7:] + second) == (
        decode_frame(first, key=keys.to_broker, expected_sequence=1),
        decode_frame(second, key=keys.to_broker, expected_sequence=2),
    )

    reflected = encode_frame(b"one", sequence=1, key=keys.to_executor)
    with pytest.raises(BrokerProtocolError) as direction_error:
        decode_frame(reflected, key=keys.to_broker, expected_sequence=1)
    assert direction_error.value.code == "frame_authentication_failed"


def test_message_is_canonical_and_direction_bound():
    keys = _keys()
    message = _message()
    encoded = encode_message(message, keys=keys, sequence=1)
    frame = decode_frame(encoded, key=keys.to_broker, expected_sequence=1)
    assert decode_message(frame, direction="to_broker") == message

    noncanonical_payload = json.dumps(
        message.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    noncanonical_frame = encode_frame(noncanonical_payload, sequence=1, key=keys.to_broker)
    with pytest.raises(BrokerProtocolError) as canonical_error:
        decode_message(
            decode_frame(noncanonical_frame, key=keys.to_broker, expected_sequence=1),
            direction="to_broker",
        )
    assert canonical_error.value.code == "message_noncanonical"

    forbidden_payload = {
        **message.model_dump(),
        "body": {"path": "C:\\private", "profile": "artifact.transform.v1"},
    }
    forbidden_frame = encode_frame(
        json.dumps(
            forbidden_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii"),
        sequence=1,
        key=keys.to_broker,
    )
    with pytest.raises(BrokerProtocolError) as body_error:
        decode_message(
            decode_frame(forbidden_frame, key=keys.to_broker, expected_sequence=1),
            direction="to_broker",
        )
    assert body_error.value.code == "message_invalid"

    nested_payload = {
        **message.model_dump(),
        "body": {"input": {"metadata": [{"path": "C:\\private"}]}},
    }
    nested_frame = encode_frame(
        json.dumps(
            nested_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii"),
        sequence=1,
        key=keys.to_broker,
    )
    with pytest.raises(BrokerProtocolError) as nested_error:
        decode_message(
            decode_frame(nested_frame, key=keys.to_broker, expected_sequence=1),
            direction="to_broker",
        )
    assert nested_error.value.code == "message_invalid"

    mutated = _message()
    mutated.body["path"] = "C:\\private"
    with pytest.raises(BrokerProtocolError) as mutated_error:
        encode_message(mutated, keys=keys, sequence=1)
    assert mutated_error.value.code == "message_invalid"


def test_peer_acl_identity_and_job_owner_are_all_required():
    keys = _keys()
    message = _message()
    peer = _peer()
    policy = _policy()
    encoded = encode_message(message, keys=keys, sequence=1)
    verified = decode_message(
        decode_frame(encoded, key=keys.to_broker, expected_sequence=1),
        direction="to_broker",
    )

    authorize_message(
        verified,
        peer=peer,
        peer_policy=policy,
        expected_principal_id=PRINCIPAL,
        owner_for_job=lambda job_id: PRINCIPAL if job_id == "job_1" else None,
    )

    with pytest.raises(BrokerProtocolError) as principal_error:
        authorize_message(
            _message(principal="b" * 64),
            peer=peer,
            peer_policy=policy,
            expected_principal_id=PRINCIPAL,
            owner_for_job=lambda _job_id: PRINCIPAL,
        )
    assert principal_error.value.code == "broker_principal_mismatch"

    with pytest.raises(BrokerProtocolError) as owner_error:
        authorize_message(
            message,
            peer=peer,
            peer_policy=policy,
            expected_principal_id=PRINCIPAL,
            owner_for_job=lambda _job_id: "b" * 64,
        )
    assert owner_error.value.code == "broker_owner_mismatch"

    with pytest.raises(BrokerProtocolError) as acl_error:
        authorize_message(
            message,
            peer=_peer(user_sid="S-1-5-21-999-999-999-999"),
            peer_policy=policy,
            expected_principal_id=PRINCIPAL,
            owner_for_job=lambda _job_id: PRINCIPAL,
        )
    assert acl_error.value.code == "peer_acl_denied"

    with pytest.raises(BrokerProtocolError) as integrity_error:
        authorize_message(
            message,
            peer=_peer(integrity_level="medium"),
            peer_policy=policy,
            expected_principal_id=PRINCIPAL,
            owner_for_job=lambda _job_id: PRINCIPAL,
        )
    assert integrity_error.value.code == "peer_integrity_denied"


def test_peer_pid_and_owner_lookup_fail_closed_without_details():
    message = _message()
    with pytest.raises(BrokerProtocolError) as pid_error:
        authorize_message(
            message,
            peer=_peer(process_id=999),
            peer_policy=_policy(),
            expected_principal_id=PRINCIPAL,
            owner_for_job=lambda _job_id: PRINCIPAL,
        )
    assert pid_error.value.code == "peer_identity_mismatch"

    with pytest.raises(BrokerProtocolError) as lookup_error:
        authorize_message(
            message,
            peer=_peer(),
            peer_policy=_policy(),
            expected_principal_id=PRINCIPAL,
            owner_for_job=lambda _job_id: (_ for _ in ()).throw(RuntimeError("private")),
        )
    assert lookup_error.value.code == "broker_owner_lookup_failed"
    assert "private" not in str(lookup_error.value)
