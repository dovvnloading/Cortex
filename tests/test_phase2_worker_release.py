"""Signed one-folder worker release and installer gate tests."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cortex_backend.execution.bundle_installer import SignedBundleInstaller
from cortex_backend.execution.manifest import TrustedRecipeKeys, verify_manifest_signature
from cortex_backend.execution.worker_provenance import verify_active_worker
from cortex_backend.execution.worker_release import (
    WorkerReleaseError,
    build_signed_worker_manifest,
)
from tools.sign_recipe_worker import main as sign_worker_main


def _public_key(signer: Ed25519PrivateKey) -> bytes:
    return signer.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def test_release_manifest_covers_one_folder_resources_and_installs_exactly(tmp_path: Path):
    signer = Ed25519PrivateKey.generate()
    source = tmp_path / "recipe-runtime"
    (source / "lib" / "nested").mkdir(parents=True)
    (source / "recipe_worker.exe").write_bytes(b"signed worker bytes")
    (source / "lib" / "codec.pyd").write_bytes(b"codec dependency")
    (source / "lib" / "nested" / "metadata.dat").write_bytes(b"resource")
    (source / "_internal" / "tzdata" / "GMT+0").parent.mkdir(parents=True)
    (source / "_internal" / "tzdata" / "GMT+0").write_bytes(b"timezone resource")

    release = build_signed_worker_manifest(
        source,
        private_key_bytes=signer.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        ),
        key_id="release-1",
        bundle_version="1.2.3",
        sequence=1,
    )

    verified = verify_manifest_signature(release.manifest, TrustedRecipeKeys({"release-1": _public_key(signer)}))
    assert verified.digest == release.manifest_digest
    assert release.entry_count == 4
    assert {entry["entrypoint"] for entry in release.manifest["entries"]} == {
        "image_transform",
        "resource",
    }
    installer = SignedBundleInstaller(
        tmp_path / "store",
        TrustedRecipeKeys({"release-1": _public_key(signer)}),
    )
    installed = installer.install(release.manifest, source)
    worker = verify_active_worker(installer)
    assert worker.worker_path == "recipe_worker.exe"
    assert worker.worker_size == len(b"signed worker bytes")
    assert worker.worker_sha256 == sha256(b"signed worker bytes").hexdigest()
    assert (installed.bundle_root / "lib" / "codec.pyd").read_bytes() == b"codec dependency"
    assert (installed.bundle_root / "lib" / "nested" / "metadata.dat").read_bytes() == b"resource"
    assert (installed.bundle_root / "_internal" / "tzdata" / "GMT+0").read_bytes() == b"timezone resource"


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda root: (root / "manifest.json").write_text("{}", encoding="ascii"), "release_manifest_reserved"),
        (lambda root: (root / "recipe_worker.exe").unlink(), "release_worker_missing"),
        (lambda root: (root / "empty.dat").write_bytes(b""), "release_entry_size_invalid"),
    ],
)
def test_release_manifest_rejects_incomplete_or_ambiguous_package(
    tmp_path: Path,
    mutator,
    code: str,
):
    signer = Ed25519PrivateKey.generate()
    source = tmp_path / "recipe-runtime"
    source.mkdir()
    (source / "recipe_worker.exe").write_bytes(b"worker")
    mutator(source)
    with pytest.raises(WorkerReleaseError) as error:
        build_signed_worker_manifest(
            source,
            private_key_bytes=signer.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            ),
            key_id="release-1",
            bundle_version="1.0.0",
            sequence=1,
        )
    assert error.value.code == code


def test_release_manifest_rejects_invalid_external_key_material(tmp_path: Path):
    source = tmp_path / "recipe-runtime"
    source.mkdir()
    (source / "recipe_worker.exe").write_bytes(b"worker")
    with pytest.raises(WorkerReleaseError) as error:
        build_signed_worker_manifest(
            source,
            private_key_bytes=b"not-a-private-key",
            key_id="release-1",
            bundle_version="1.0.0",
            sequence=1,
        )
    assert error.value.code == "release_signing_key_invalid"


def test_release_cli_writes_only_signed_manifest_metadata(tmp_path: Path, capsys):
    signer = Ed25519PrivateKey.generate()
    private_key = tmp_path / "release.key"
    private_key.write_bytes(
        signer.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    source = tmp_path / "recipe-runtime"
    source.mkdir()
    (source / "recipe_worker.exe").write_bytes(b"worker")
    output = tmp_path / "out" / "recipe-runtime.manifest.json"

    assert sign_worker_main(
        [
            "--source-root",
            str(source),
            "--private-key",
            str(private_key),
            "--key-id",
            "release-1",
            "--bundle-version",
            "1.0.0",
            "--sequence",
            "1",
            "--output-manifest",
            str(output),
            "--json",
        ]
    ) == 0
    result = capsys.readouterr()
    assert json.loads(result.out)["status"] == "signed"
    assert private_key.read_bytes().hex() not in result.out
    verified = verify_manifest_signature(
        json.loads(output.read_text(encoding="ascii")),
        TrustedRecipeKeys({"release-1": _public_key(signer)}),
    )
    assert verified.manifest.entries[0].bundle_path == "recipe_worker.exe"


def test_release_root_reparse_alias_is_rejected_when_supported(tmp_path: Path):
    source = tmp_path / "recipe-runtime"
    source.mkdir()
    (source / "recipe_worker.exe").write_bytes(b"worker")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(source, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this host")
    signer = Ed25519PrivateKey.generate()
    with pytest.raises(WorkerReleaseError) as error:
        build_signed_worker_manifest(
            alias,
            private_key_bytes=signer.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            ),
            key_id="release-1",
            bundle_version="1.0.0",
            sequence=1,
        )
    assert error.value.code == "release_root_invalid"
