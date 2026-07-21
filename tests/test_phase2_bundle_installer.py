"""Signed bundle staging, activation, key rotation, and recovery tests."""

from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import cortex_backend.execution.bundle_installer as installer_module
from cortex_backend.execution.bundle_installer import (
    BundleInstallError,
    KeyringUpdate,
    SignedBundleInstaller,
)
from cortex_backend.execution.manifest import (
    ManifestVerificationError,
    TrustedRecipeKeys,
    verify_manifest_signature,
)


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _public(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _keyring(private_key: Ed25519PrivateKey, *, key_id: str = "release-1") -> TrustedRecipeKeys:
    return TrustedRecipeKeys({key_id: _public(private_key)})


def _payload(
    private_key: Ed25519PrivateKey,
    *,
    key_id: str = "release-1",
    sequence: int = 1,
    bundle_version: str = "1.0.0",
    rollback_of: str | None = None,
    content: bytes = b"signed recipe bytes",
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
                "bundle_path": "recipes/image.bin",
                "entrypoint": "image_transform",
                "version": bundle_version,
                "size": len(content),
                "sha256": sha256(content).hexdigest(),
            }
        ],
    }
    signature = base64.urlsafe_b64encode(private_key.sign(_canonical(unsigned))).decode("ascii")
    return {**unsigned, "signature": signature.rstrip("=")}


def _source(root: Path, content: bytes = b"signed recipe bytes") -> None:
    target = root / "recipes" / "image.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _keyring_update(
    installer: SignedBundleInstaller,
    signer: Ed25519PrivateKey,
    new_key: Ed25519PrivateKey,
) -> dict:
    keyring = TrustedRecipeKeys(
        {"release-1": _public(signer), "release-2": _public(new_key)},
        revoked=frozenset({"release-1"}),
    )
    unsigned = KeyringUpdate(
        sequence=1,
        signer_key_id="release-1",
        previous_digest=installer._bootstrap_digest,
        keyring=keyring,
        signature="placeholder",
    )
    signature = base64.urlsafe_b64encode(signer.sign(unsigned.signed_payload())).decode("ascii")
    return {**unsigned.as_payload(), "signature": signature.rstrip("=")}


def test_install_is_verified_copied_and_restart_stable(tmp_path: Path):
    signer = Ed25519PrivateKey.generate()
    source = tmp_path / "incoming"
    _source(source)
    installer = SignedBundleInstaller(tmp_path / "store", _keyring(signer))

    installed = installer.install(_payload(signer), source)

    assert installed.bundle_root.name == f"bundle-{installed.state.digest}"
    assert (installed.bundle_root / "recipes/image.bin").read_bytes() == b"signed recipe bytes"
    assert (installed.bundle_root / "manifest.json").is_file()
    (source / "unlisted.txt").write_text("not trusted", encoding="utf-8")
    restarted = SignedBundleInstaller(tmp_path / "store", _keyring(signer))
    assert restarted.status() == installed
    assert not (installed.bundle_root / "unlisted.txt").exists()


def test_failed_state_commit_preserves_previous_active_generation(tmp_path: Path, monkeypatch):
    signer = Ed25519PrivateKey.generate()
    keys = _keyring(signer)
    source = tmp_path / "incoming"
    _source(source)
    installer = SignedBundleInstaller(tmp_path / "store", keys)
    first = installer.install(_payload(signer), source)

    updated = b"updated signed recipe bytes"
    (source / "recipes/image.bin").write_bytes(updated)
    payload = _payload(signer, sequence=2, bundle_version="1.1.0", content=updated)

    def fail_state(*_args, **_kwargs):
        raise BundleInstallError("bundle_install_io_failed")

    monkeypatch.setattr(installer_module, "_atomic_write", fail_state)
    with pytest.raises(BundleInstallError) as error:
        installer.install(payload, source)
    assert error.value.code == "bundle_install_io_failed"
    assert installer.status() == first
    assert not any(path.name.startswith(".staging-") for path in installer.bundle_root.iterdir())


def test_rollback_requires_explicit_authorization_and_recovery_is_explicit(tmp_path: Path):
    signer = Ed25519PrivateKey.generate()
    keys = _keyring(signer)
    source = tmp_path / "incoming"
    installer = SignedBundleInstaller(tmp_path / "store", keys)
    _source(source, b"version one")
    first = installer.install(
        _payload(signer, content=b"version one"),
        source,
    )
    _source(source, b"version two")
    second = installer.install(
        _payload(signer, sequence=2, bundle_version="2.0.0", content=b"version two"),
        source,
    )
    rollback = _payload(
        signer,
        sequence=3,
        bundle_version="1.0.0",
        rollback_of=second.state.digest,
        content=b"version one",
    )
    _source(source, b"version one")
    with pytest.raises(ManifestVerificationError) as unauthorized:
        installer.install(rollback, source)
    assert getattr(unauthorized.value, "code", "") == "manifest_rollback_not_authorized"

    rolled_back = installer.install(
        rollback,
        source,
        rollback_authorizer=lambda current, candidate: current == second.state.digest
        and candidate == verify_manifest_signature(rollback, keys).digest,
    )
    assert rolled_back.rollback is True
    assert rolled_back.state.bundle_version == "1.0.0"

    # Corruption never auto-falls back; the retained previous generation needs a
    # separate recovery decision.
    (rolled_back.bundle_root / "recipes/image.bin").write_bytes(b"corrupted")
    with pytest.raises(BundleInstallError) as corrupted:
        installer.status()
    assert corrupted.value.code == "bundle_install_recovery_failed"
    with pytest.raises(BundleInstallError) as recovery_denied:
        installer.recover_previous()
    assert recovery_denied.value.code == "bundle_recovery_not_authorized"
    recovered = installer.recover_previous(
        lambda current, candidate: current == rolled_back.state.digest
        and candidate == second.state.digest
    )
    assert recovered.state.bundle_version == "2.0.0"


def test_key_rotation_is_signed_chained_and_persistent(tmp_path: Path):
    signer = Ed25519PrivateKey.generate()
    next_signer = Ed25519PrivateKey.generate()
    installer = SignedBundleInstaller(tmp_path / "store", _keyring(signer))
    update = _keyring_update(installer, signer, next_signer)

    installer.rotate_keyring(update)
    source = tmp_path / "incoming"
    _source(source, b"new key bundle")
    installed = installer.install(
        _payload(
            next_signer,
            key_id="release-2",
            content=b"new key bundle",
        ),
        source,
    )
    restarted = SignedBundleInstaller(tmp_path / "store", _keyring(signer))
    assert restarted.status() == installed

    with pytest.raises(BundleInstallError) as replay:
        restarted.rotate_keyring(update)
    assert replay.value.code == "keyring_replay"


def test_key_rotation_cannot_revoke_active_signer(tmp_path: Path):
    signer = Ed25519PrivateKey.generate()
    next_signer = Ed25519PrivateKey.generate()
    source = tmp_path / "incoming"
    _source(source)
    installer = SignedBundleInstaller(tmp_path / "store", _keyring(signer))
    installer.install(_payload(signer), source)
    update = _keyring_update(installer, signer, next_signer)
    with pytest.raises(BundleInstallError) as error:
        installer.rotate_keyring(update)
    assert error.value.code == "keyring_active_key_revoked"


def test_hardlinked_source_is_rejected_when_platform_supports_it(tmp_path: Path):
    signer = Ed25519PrivateKey.generate()
    source = tmp_path / "incoming"
    _source(source)
    external = tmp_path / "external.bin"
    external.write_bytes(b"signed recipe bytes")
    target = source / "recipes/image.bin"
    target.unlink()
    try:
        target.hardlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable on this platform")
    installer = SignedBundleInstaller(tmp_path / "store", _keyring(signer))
    with pytest.raises(BundleInstallError) as error:
        installer.install(_payload(signer), source)
    assert error.value.code == "bundle_hardlink_rejected"


def test_state_tampering_with_keyring_history_fails_closed(tmp_path: Path):
    signer = Ed25519PrivateKey.generate()
    next_signer = Ed25519PrivateKey.generate()
    installer = SignedBundleInstaller(tmp_path / "store", _keyring(signer))
    installer.rotate_keyring(_keyring_update(installer, signer, next_signer))
    source = tmp_path / "incoming"
    _source(source, b"new key bundle")
    installer.install(
        _payload(next_signer, key_id="release-2", content=b"new key bundle"),
        source,
    )
    state = installer.state_path
    payload = json.loads(state.read_text(encoding="ascii"))
    payload["keyring_history"] = []
    state.write_text(json.dumps(payload), encoding="ascii")
    with pytest.raises(BundleInstallError) as error:
        SignedBundleInstaller(tmp_path / "store", _keyring(signer)).status()
    assert error.value.code == "bundle_install_recovery_failed"
