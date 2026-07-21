"""Signed recipe manifest verification and rollback policy tests."""

from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cortex_backend.execution.manifest import (
    ManifestState,
    ManifestVerificationError,
    TrustedRecipeKeys,
    parse_signed_manifest,
    verify_bundle_files,
    verify_signed_manifest,
)


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _keyring(private_key: Ed25519PrivateKey, *, key_id: str = "release-1") -> TrustedRecipeKeys:
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return TrustedRecipeKeys({key_id: public})


def _payload(
    private_key: Ed25519PrivateKey,
    *,
    key_id: str = "release-1",
    sequence: int = 1,
    bundle_version: str = "1.0.0",
    rollback_of: str | None = None,
    content: bytes = b"signed recipe bytes",
    bundle_path: str = "recipes/image.bin",
) -> dict:
    unsigned = {
        "schema_version": "recipe.manifest.v1",
        "key_id": key_id,
        "sequence": sequence,
        "bundle_version": bundle_version,
        "rollback_of": rollback_of,
        "entries": [
            {
                "recipe_id": "image-transform",
                "bundle_path": bundle_path,
                "entrypoint": "image_transform",
                "version": bundle_version,
                "size": len(content),
                "sha256": sha256(content).hexdigest(),
            }
        ],
    }
    signature = base64.urlsafe_b64encode(private_key.sign(_canonical(unsigned))).decode("ascii")
    return {**unsigned, "signature": signature.rstrip("=")}


def test_valid_manifest_signature_and_pinned_entry_verify():
    private_key = Ed25519PrivateKey.generate()
    payload = _payload(private_key)
    verified = verify_signed_manifest(payload, _keyring(private_key))

    assert verified.rollback is False
    assert verified.manifest.bundle_version == "1.0.0"
    assert verified.state.sequence == 1
    assert len(verified.digest) == 64


def test_signature_tampering_unknown_and_revoked_keys_fail_closed():
    private_key = Ed25519PrivateKey.generate()
    payload = _payload(private_key)
    tampered = {**payload, "bundle_version": "1.0.1"}
    with pytest.raises(ManifestVerificationError) as tampered_error:
        verify_signed_manifest(tampered, _keyring(private_key))
    assert tampered_error.value.code == "manifest_signature_invalid"

    unknown_key = Ed25519PrivateKey.generate()
    with pytest.raises(ManifestVerificationError) as unknown_error:
        verify_signed_manifest(
            _payload(unknown_key, key_id="release-2"),
            _keyring(private_key),
        )
    assert unknown_error.value.code == "manifest_key_untrusted"

    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    revoked = TrustedRecipeKeys({"release-1": public}, revoked=frozenset({"release-1"}))
    with pytest.raises(ManifestVerificationError) as revoked_error:
        verify_signed_manifest(payload, revoked)
    assert revoked_error.value.code == "manifest_key_untrusted"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: {**payload, "signature": "not-a-signature"},
        lambda payload: {**payload, "sequence": 0},
        lambda payload: {**payload, "entries": payload["entries"] * 2},
        lambda payload: {**payload, "entries": [{**payload["entries"][0], "bundle_path": "../escape"}]},
    ],
)
def test_malformed_manifest_is_rejected_without_private_details(mutator):
    private_key = Ed25519PrivateKey.generate()
    with pytest.raises(ManifestVerificationError) as error:
        verify_signed_manifest(mutator(_payload(private_key)), _keyring(private_key))

    assert error.value.code in {
        "manifest_invalid",
        "manifest_signature_invalid",
    }
    assert "escape" not in str(error.value)


def test_updates_are_monotonic_and_downgrades_need_explicit_rollback_authority():
    private_key = Ed25519PrivateKey.generate()
    keys = _keyring(private_key)
    first = verify_signed_manifest(_payload(private_key), keys)
    update = verify_signed_manifest(
        _payload(private_key, sequence=2, bundle_version="1.1.0"),
        keys,
        current=first.state,
    )
    assert update.rollback is False

    with pytest.raises(ManifestVerificationError) as downgrade_error:
        verify_signed_manifest(
            _payload(private_key, sequence=3, bundle_version="1.0.1"),
            keys,
            current=update.state,
        )
    assert downgrade_error.value.code == "manifest_downgrade"

    rollback_payload = _payload(
        private_key,
        sequence=3,
        bundle_version="1.0.0",
        rollback_of=update.digest,
    )
    with pytest.raises(ManifestVerificationError) as unauthorized_error:
        verify_signed_manifest(rollback_payload, keys, current=update.state)
    assert unauthorized_error.value.code == "manifest_rollback_not_authorized"

    rollback = verify_signed_manifest(
        rollback_payload,
        keys,
        current=update.state,
        rollback_authorized=True,
    )
    assert rollback.rollback is True
    assert rollback.state.sequence == 3

    with pytest.raises(ManifestVerificationError) as replay_error:
        verify_signed_manifest(
            _payload(private_key, sequence=2, bundle_version="1.1.0"),
            keys,
            current=update.state,
        )
    assert replay_error.value.code == "manifest_replay"


def test_bundle_files_are_size_and_hash_verified_without_following_links(tmp_path: Path):
    private_key = Ed25519PrivateKey.generate()
    content = b"signed recipe bytes"
    root = tmp_path / "bundle"
    recipe_path = root / "recipes" / "image.bin"
    recipe_path.parent.mkdir(parents=True)
    recipe_path.write_bytes(content)
    manifest = parse_signed_manifest(_payload(private_key, content=content))

    verify_bundle_files(manifest, root)

    recipe_path.write_bytes(b"tampered")
    with pytest.raises(ManifestVerificationError) as error:
        verify_bundle_files(manifest, root)
    assert error.value.code == "bundle_size_mismatch"


def test_bundle_root_and_path_failures_are_safe(tmp_path: Path):
    private_key = Ed25519PrivateKey.generate()
    manifest = parse_signed_manifest(_payload(private_key))
    with pytest.raises(ManifestVerificationError) as missing_error:
        verify_bundle_files(manifest, tmp_path / "missing")
    assert missing_error.value.code == "bundle_root_unavailable"

    current = ManifestState(sequence=4, bundle_version="1.2.0", digest="a" * 64)
    with pytest.raises(ManifestVerificationError) as rollback_error:
        verify_signed_manifest(
            _payload(private_key, sequence=5, bundle_version="1.0.0", rollback_of="b" * 64),
            _keyring(private_key),
            current=current,
            rollback_authorized=True,
        )
    assert rollback_error.value.code == "manifest_rollback_not_authorized"

    with pytest.raises(ValueError):
        ManifestState(sequence=0, bundle_version="1.2.0", digest="a" * 64)
    with pytest.raises(ValueError):
        ManifestState(sequence=4, bundle_version="1.2", digest="a" * 64)
