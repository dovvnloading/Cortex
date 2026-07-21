"""Fail-closed verification for signed, pinned recipe bundles.

Manifest verification authenticates a bundle description and its bytes.  It does not
install, load, decode, execute, or publish a recipe provider.  Those actions remain
separate sandbox and release gates.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


MAX_MANIFEST_BYTES = 256 * 1024
MAX_MANIFEST_ENTRIES = 128
MAX_BUNDLE_ENTRY_BYTES = 128 * 1024 * 1024
_SAFE_KEY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SAFE_RECIPE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SAFE_BUNDLE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class ManifestVerificationError(ValueError):
    """Stable, non-sensitive manifest failure category."""

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            raise ValueError("invalid manifest verification code")
        self.code = code
        super().__init__("The recipe bundle failed integrity verification.")


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )


class ManifestEntry(_ManifestModel):
    recipe_id: str
    bundle_path: str
    entrypoint: Literal["image_transform", "calculation", "check"]
    version: str
    size: int = Field(strict=True, ge=1, le=MAX_BUNDLE_ENTRY_BYTES)
    sha256: str

    @field_validator("recipe_id")
    @classmethod
    def _validate_recipe_id(cls, value: str) -> str:
        if _SAFE_RECIPE_ID.fullmatch(value) is None:
            raise ValueError("recipe id is invalid")
        return value

    @field_validator("bundle_path")
    @classmethod
    def _validate_bundle_path(cls, value: str) -> str:
        parts = value.split("/")
        if (
            _SAFE_BUNDLE_PATH.fullmatch(value) is None
            or "\\" in value
            or value.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("bundle path is invalid")
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if _SEMVER.fullmatch(value) is None:
            raise ValueError("recipe version is invalid")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("bundle digest is invalid")
        return value


class SignedRecipeManifest(_ManifestModel):
    schema_version: Literal["recipe.manifest.v1"]
    key_id: str
    sequence: int = Field(strict=True, ge=1)
    bundle_version: str
    rollback_of: str | None = None
    entries: tuple[ManifestEntry, ...] = Field(
        min_length=1,
        max_length=MAX_MANIFEST_ENTRIES,
    )
    signature: str

    @field_validator("key_id")
    @classmethod
    def _validate_key_id(cls, value: str) -> str:
        if _SAFE_KEY_ID.fullmatch(value) is None:
            raise ValueError("key id is invalid")
        return value

    @field_validator("bundle_version")
    @classmethod
    def _validate_bundle_version(cls, value: str) -> str:
        if _SEMVER.fullmatch(value) is None:
            raise ValueError("bundle version is invalid")
        return value

    @field_validator("rollback_of")
    @classmethod
    def _validate_rollback_digest(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("rollback digest is invalid")
        return value

    @field_validator("signature")
    @classmethod
    def _validate_signature_text(cls, value: str) -> str:
        if not 80 <= len(value) <= 100:
            raise ValueError("signature encoding is invalid")
        return value

    @model_validator(mode="after")
    def _unique_entries(self) -> "SignedRecipeManifest":
        recipe_ids = [entry.recipe_id for entry in self.entries]
        paths = [entry.bundle_path for entry in self.entries]
        if len(recipe_ids) != len(set(recipe_ids)) or len(paths) != len(set(paths)):
            raise ValueError("manifest entries must be unique")
        return self

    def signed_payload(self) -> bytes:
        data = self.model_dump(mode="json")
        data.pop("signature", None)
        return json.dumps(
            data,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")

    def manifest_digest(self) -> str:
        return sha256(self.canonical_json().encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class ManifestState:
    sequence: int
    bundle_version: str
    digest: str

    def __post_init__(self) -> None:
        if self.sequence < 1 or _SEMVER.fullmatch(self.bundle_version) is None:
            raise ValueError("manifest state is invalid")
        if _SHA256.fullmatch(self.digest) is None:
            raise ValueError("manifest state digest is invalid")


@dataclass(frozen=True, slots=True)
class VerifiedRecipeManifest:
    manifest: SignedRecipeManifest
    digest: str
    rollback: bool

    @property
    def state(self) -> ManifestState:
        return ManifestState(
            sequence=self.manifest.sequence,
            bundle_version=self.manifest.bundle_version,
            digest=self.digest,
        )


class TrustedRecipeKeys:
    """Pinned Ed25519 public keys selected by an immutable key identifier."""

    def __init__(self, keys: Mapping[str, bytes], *, revoked: frozenset[str] = frozenset()) -> None:
        if not keys:
            raise ValueError("at least one trusted recipe key is required")
        normalized: dict[str, bytes] = {}
        for key_id, key_bytes in keys.items():
            if (
                not isinstance(key_id, str)
                or _SAFE_KEY_ID.fullmatch(key_id) is None
                or not isinstance(key_bytes, bytes)
            ):
                raise ValueError("trusted recipe key is invalid")
            if len(key_bytes) != 32:
                raise ValueError("Ed25519 public keys must be 32 bytes")
            normalized[key_id] = bytes(key_bytes)
        if not revoked.issubset(normalized):
            raise ValueError("revoked key is not in the trusted keyring")
        self._keys = normalized
        self._revoked = frozenset(revoked)

    def public_key(self, key_id: str) -> Ed25519PublicKey:
        if key_id not in self._keys or key_id in self._revoked:
            raise ManifestVerificationError("manifest_key_untrusted")
        try:
            return Ed25519PublicKey.from_public_bytes(self._keys[key_id])
        except (TypeError, ValueError, UnsupportedAlgorithm):
            raise ManifestVerificationError("manifest_key_untrusted") from None


def _parse_version(version: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(version)
    if match is None:
        raise ManifestVerificationError("manifest_invalid")
    return tuple(int(part) for part in match.groups())


def _decode_signature(encoded: str) -> bytes:
    if re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded) is None:
        raise ManifestVerificationError("manifest_signature_invalid")
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
            signature = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise ManifestVerificationError("manifest_signature_invalid") from None
    if len(signature) != 64:
        raise ManifestVerificationError("manifest_signature_invalid")
    return signature


def parse_signed_manifest(payload: Mapping[str, Any]) -> SignedRecipeManifest:
    if not isinstance(payload, Mapping):
        raise ManifestVerificationError("manifest_invalid")
    try:
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        raise ManifestVerificationError("manifest_invalid") from None
    if len(encoded.encode("ascii")) > MAX_MANIFEST_BYTES:
        raise ManifestVerificationError("manifest_too_large")
    candidate = dict(payload)
    if isinstance(candidate.get("entries"), list):
        candidate["entries"] = tuple(candidate["entries"])
    try:
        return SignedRecipeManifest.model_validate(candidate)
    except ValidationError:
        raise ManifestVerificationError("manifest_invalid") from None


def _validate_transition(
    manifest: SignedRecipeManifest,
    digest: str,
    current: ManifestState | None,
    *,
    rollback_authorized: bool,
) -> bool:
    if current is None:
        if manifest.rollback_of is not None:
            raise ManifestVerificationError("manifest_rollback_invalid")
        return False
    if manifest.sequence <= current.sequence:
        raise ManifestVerificationError("manifest_replay")
    version = _parse_version(manifest.bundle_version)
    current_version = _parse_version(current.bundle_version)
    if manifest.rollback_of is None:
        if version < current_version:
            raise ManifestVerificationError("manifest_downgrade")
        return False
    if not rollback_authorized or manifest.rollback_of != current.digest:
        raise ManifestVerificationError("manifest_rollback_not_authorized")
    if digest == current.digest:
        raise ManifestVerificationError("manifest_replay")
    return True


def verify_signed_manifest(
    payload: Mapping[str, Any],
    trusted_keys: TrustedRecipeKeys,
    *,
    current: ManifestState | None = None,
    rollback_authorized: bool = False,
) -> VerifiedRecipeManifest:
    manifest = parse_signed_manifest(payload)
    public_key = trusted_keys.public_key(manifest.key_id)
    signature = _decode_signature(manifest.signature)
    try:
        public_key.verify(signature, manifest.signed_payload())
    except (InvalidSignature, ValueError, TypeError):
        raise ManifestVerificationError("manifest_signature_invalid") from None
    digest = manifest.manifest_digest()
    rollback = _validate_transition(
        manifest,
        digest,
        current,
        rollback_authorized=rollback_authorized,
    )
    return VerifiedRecipeManifest(manifest=manifest, digest=digest, rollback=rollback)


def verify_bundle_files(manifest: SignedRecipeManifest, bundle_root: Path) -> None:
    """Verify every pinned bundle file without installing or loading it."""
    if bundle_root.is_symlink():
        raise ManifestVerificationError("bundle_root_unavailable")
    try:
        root = bundle_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ManifestVerificationError("bundle_root_unavailable") from None
    if not root.is_dir():
        raise ManifestVerificationError("bundle_root_unavailable")
    for entry in manifest.entries:
        candidate = root / entry.bundle_path
        try:
            cursor = root
            for component in Path(entry.bundle_path).parts:
                cursor = cursor / component
                if cursor.is_symlink():
                    raise ManifestVerificationError("bundle_path_invalid")
            path = candidate.resolve()
            if path.is_symlink() or not path.is_relative_to(root) or not path.is_file():
                raise ManifestVerificationError("bundle_path_invalid")
            with path.open("rb") as stream:
                content = stream.read(MAX_BUNDLE_ENTRY_BYTES + 1)
        except ManifestVerificationError:
            raise
        except (OSError, RuntimeError):
            raise ManifestVerificationError("bundle_entry_unavailable") from None
        if len(content) != entry.size:
            raise ManifestVerificationError("bundle_size_mismatch")
        if sha256(content).hexdigest() != entry.sha256:
            raise ManifestVerificationError("bundle_hash_mismatch")


__all__ = [
    "MAX_BUNDLE_ENTRY_BYTES",
    "MAX_MANIFEST_BYTES",
    "ManifestEntry",
    "ManifestState",
    "ManifestVerificationError",
    "SignedRecipeManifest",
    "TrustedRecipeKeys",
    "VerifiedRecipeManifest",
    "parse_signed_manifest",
    "verify_bundle_files",
    "verify_signed_manifest",
]
