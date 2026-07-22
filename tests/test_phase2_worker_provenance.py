"""Signed image-worker role binding remains storage-only and fail-closed."""

from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cortex_backend.execution.bundle_installer import SignedBundleInstaller
from cortex_backend.execution.manifest import TrustedRecipeKeys
from cortex_backend.execution.worker_provenance import (
    WorkerProvenanceError,
    verify_active_worker,
)


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _keyring(private_key: Ed25519PrivateKey) -> TrustedRecipeKeys:
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return TrustedRecipeKeys({"release-1": public})


def _payload(
    private_key: Ed25519PrivateKey,
    entries: list[dict],
) -> dict:
    unsigned = {
        "schema_version": "recipe.manifest.v1",
        "key_id": "release-1",
        "sequence": 1,
        "bundle_version": "1.0.0",
        "rollback_of": None,
        "entries": entries,
    }
    signature = base64.urlsafe_b64encode(private_key.sign(_canonical(unsigned))).decode("ascii")
    return {**unsigned, "signature": signature.rstrip("=")}


def _entry(path: str, content: bytes, *, recipe_id: str = "image-transform") -> dict:
    return {
        "recipe_id": recipe_id,
        "bundle_path": path,
        "entrypoint": "image_transform",
        "version": "1.0.0",
        "size": len(content),
        "sha256": sha256(content).hexdigest(),
    }


def _install(
    tmp_path: Path,
    entries: list[tuple[str, bytes, str]],
):
    signer = Ed25519PrivateKey.generate()
    source = tmp_path / "incoming"
    for path, content, _recipe_id in entries:
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    manifest_entries = [
        _entry(path, content, recipe_id=recipe_id)
        for path, content, recipe_id in entries
    ]
    installer = SignedBundleInstaller(tmp_path / "store", _keyring(signer))
    installer.install(_payload(signer, manifest_entries), source)
    return installer


def test_active_signed_generation_binds_exact_worker_role(tmp_path: Path):
    content = b"fixed recipe worker fixture"
    installer = _install(tmp_path, [("recipe_worker.exe", content, "image-transform")])

    worker = verify_active_worker(installer)

    assert worker.worker_path == "recipe_worker.exe"
    assert worker.worker_size == len(content)
    assert worker.worker_sha256 == sha256(content).hexdigest()
    assert worker.recipe_version == "1.0.0"


def test_no_active_generation_is_unavailable(tmp_path: Path):
    signer = Ed25519PrivateKey.generate()
    installer = SignedBundleInstaller(tmp_path / "store", _keyring(signer))

    with pytest.raises(WorkerProvenanceError) as error:
        verify_active_worker(installer)

    assert error.value.code == "worker_bundle_unavailable"


def test_worker_role_must_use_fixed_entrypoint_path(tmp_path: Path):
    installer = _install(tmp_path, [("recipes/image.bin", b"worker", "image-transform")])

    with pytest.raises(WorkerProvenanceError) as error:
        verify_active_worker(installer)

    assert error.value.code == "worker_entrypoint_mismatch"


def test_multiple_image_roles_are_ambiguous(tmp_path: Path):
    installer = _install(
        tmp_path,
        [
            ("recipe_worker.exe", b"worker-one", "image-transform"),
            ("recipe_worker_two.exe", b"worker-two", "image-transform-two"),
        ],
    )

    with pytest.raises(WorkerProvenanceError) as error:
        verify_active_worker(installer)

    assert error.value.code == "worker_role_ambiguous"


def test_tampered_installed_worker_fails_before_role_binding(tmp_path: Path):
    installer = _install(tmp_path, [("recipe_worker.exe", b"worker", "image-transform")])
    installed = installer.status()
    assert installed is not None
    (installed.bundle_root / "recipe_worker.exe").write_bytes(b"tampered")

    with pytest.raises(WorkerProvenanceError) as error:
        verify_active_worker(installer)

    assert error.value.code == "worker_bundle_integrity_failed"


def test_reparse_worker_fails_closed_before_provenance_binding(tmp_path: Path):
    installer = _install(tmp_path, [("recipe_worker.exe", b"worker", "image-transform")])
    installed = installer.status()
    assert installed is not None
    target = installed.bundle_root / "recipe_worker.exe"
    external = tmp_path / "external-worker.exe"
    external.write_bytes(b"outside")
    target.unlink()
    try:
        target.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are unavailable on this Windows host")

    with pytest.raises(WorkerProvenanceError) as error:
        verify_active_worker(installer)

    assert error.value.code == "worker_bundle_integrity_failed"
