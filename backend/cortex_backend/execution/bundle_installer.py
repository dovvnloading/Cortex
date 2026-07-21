"""Fail-closed installation and recovery for verified recipe bundles.

The installer is deliberately a storage boundary, not a provider.  It verifies a
signed manifest and every declared source byte, copies only those bytes into a
private staging directory, commits the immutable generation, and finally replaces
one small JSON pointer atomically.  It never imports, decodes, executes, or loads a
bundle.  Keyring updates are chained to the packaged bootstrap keyring, and a
previous verified generation is retained for an explicit recovery decision.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from typing import Any

from cryptography.exceptions import InvalidSignature

from .manifest import (
    MAX_MANIFEST_BYTES,
    ManifestState,
    ManifestVerificationError,
    SignedRecipeManifest,
    TrustedRecipeKeys,
    VerifiedRecipeManifest,
    verify_bundle_files,
    verify_manifest_signature,
    verify_signed_manifest,
)


MAX_INSTALL_STATE_BYTES = 512 * 1024
MAX_KEYRING_HISTORY = 128
MAX_KEYRING_KEYS = 128
MAX_COPY_CHUNK_BYTES = 1024 * 1024
INSTALL_STATE_SCHEMA = "recipe.install.v1"
KEYRING_UPDATE_SCHEMA = "recipe.keyring.update.v1"
_SAFE_KEY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SAFE_GENERATION = re.compile(r"^bundle-[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BASE64 = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


class BundleInstallError(ValueError):
    """Stable, non-sensitive bundle installation/recovery failure."""

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            raise ValueError("invalid bundle installation code")
        self.code = code
        super().__init__("The signed recipe bundle could not be installed safely.")


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError, UnicodeEncodeError):
        raise BundleInstallError("bundle_install_state_invalid") from None


def _decode_b64(value: Any, *, expected_bytes: int, code: str) -> bytes:
    if not isinstance(value, str) or _BASE64.fullmatch(value) is None:
        raise BundleInstallError(code)
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise BundleInstallError(code) from None
    if len(decoded) != expected_bytes:
        raise BundleInstallError(code)
    return decoded


def _encode_b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _keyring_payload(sequence: int, keyring: TrustedRecipeKeys) -> dict[str, Any]:
    return {
        "schema_version": "recipe.keyring.state.v1",
        "sequence": sequence,
        "keys": {key_id: _encode_b64(keyring.keys[key_id]) for key_id in sorted(keyring.keys)},
        "revoked": sorted(keyring.revoked),
    }


def _keyring_digest(sequence: int, keyring: TrustedRecipeKeys) -> str:
    return sha256(_canonical(_keyring_payload(sequence, keyring))).hexdigest()


@dataclass(frozen=True, slots=True)
class KeyringUpdate:
    """A signed replacement keyring accepted only from the current keyring."""

    sequence: int
    signer_key_id: str
    previous_digest: str
    keyring: TrustedRecipeKeys
    signature: str

    def signed_payload(self) -> bytes:
        payload = {
            "schema_version": KEYRING_UPDATE_SCHEMA,
            "sequence": self.sequence,
            "signer_key_id": self.signer_key_id,
            "previous_digest": self.previous_digest,
            "keys": {
                key_id: _encode_b64(self.keyring.keys[key_id])
                for key_id in sorted(self.keyring.keys)
            },
            "revoked": sorted(self.keyring.revoked),
        }
        return _canonical(payload)

    def as_payload(self) -> dict[str, Any]:
        payload = json.loads(self.signed_payload().decode("ascii"))
        payload["signature"] = self.signature
        return payload


def parse_keyring_update(payload: Mapping[str, Any]) -> KeyringUpdate:
    """Parse a strict signed keyring update without accepting unknown fields."""

    if not isinstance(payload, Mapping):
        raise BundleInstallError("keyring_update_invalid")
    expected_fields = {
        "schema_version",
        "sequence",
        "signer_key_id",
        "previous_digest",
        "keys",
        "revoked",
        "signature",
    }
    if set(payload) != expected_fields:
        raise BundleInstallError("keyring_update_invalid")
    if payload.get("schema_version") != KEYRING_UPDATE_SCHEMA:
        raise BundleInstallError("keyring_update_invalid")
    sequence = payload.get("sequence")
    signer = payload.get("signer_key_id")
    previous_digest = payload.get("previous_digest")
    if type(sequence) is not int or sequence < 1:
        raise BundleInstallError("keyring_update_invalid")
    if not isinstance(signer, str) or _SAFE_KEY_ID.fullmatch(signer) is None:
        raise BundleInstallError("keyring_update_invalid")
    if not isinstance(previous_digest, str) or _SHA256.fullmatch(previous_digest) is None:
        raise BundleInstallError("keyring_update_invalid")
    raw_keys = payload.get("keys")
    revoked = payload.get("revoked")
    if not isinstance(raw_keys, Mapping) or not isinstance(revoked, list):
        raise BundleInstallError("keyring_update_invalid")
    if not 1 <= len(raw_keys) <= MAX_KEYRING_KEYS or len(revoked) > len(raw_keys):
        raise BundleInstallError("keyring_update_invalid")
    keys: dict[str, bytes] = {}
    for key_id, encoded in raw_keys.items():
        if not isinstance(key_id, str) or _SAFE_KEY_ID.fullmatch(key_id) is None:
            raise BundleInstallError("keyring_update_invalid")
        if key_id in keys:
            raise BundleInstallError("keyring_update_invalid")
        keys[key_id] = _decode_b64(encoded, expected_bytes=32, code="keyring_update_invalid")
    if any(
        not isinstance(key_id, str)
        or _SAFE_KEY_ID.fullmatch(key_id) is None
        or key_id not in keys
        for key_id in revoked
    ) or len(set(revoked)) != len(revoked):
        raise BundleInstallError("keyring_update_invalid")
    signature = payload.get("signature")
    if not isinstance(signature, str) or _BASE64.fullmatch(signature) is None:
        raise BundleInstallError("keyring_update_invalid")
    try:
        keyring = TrustedRecipeKeys(keys, revoked=frozenset(revoked))
    except ValueError:
        raise BundleInstallError("keyring_update_invalid") from None
    if all(key_id in keyring.revoked for key_id in keyring.keys):
        raise BundleInstallError("keyring_no_active_keys")
    return KeyringUpdate(
        sequence=sequence,
        signer_key_id=signer,
        previous_digest=previous_digest,
        keyring=keyring,
        signature=signature,
    )


def verify_keyring_update(
    payload: Mapping[str, Any],
    trusted_keys: TrustedRecipeKeys,
    *,
    current_sequence: int,
    current_digest: str,
) -> KeyringUpdate:
    """Verify a chained keyring update against the currently trusted keys."""

    update = parse_keyring_update(payload)
    if update.sequence <= current_sequence:
        raise BundleInstallError("keyring_replay")
    if update.previous_digest != current_digest:
        raise BundleInstallError("keyring_state_mismatch")
    if update.signer_key_id not in trusted_keys.keys or update.signer_key_id in trusted_keys.revoked:
        raise BundleInstallError("keyring_signer_untrusted")
    try:
        public_key = trusted_keys.public_key(update.signer_key_id)
        signature = _decode_b64(
            update.signature,
            expected_bytes=64,
            code="keyring_signature_invalid",
        )
        public_key.verify(signature, update.signed_payload())
    except BundleInstallError:
        raise
    except (InvalidSignature, ValueError, TypeError):
        raise BundleInstallError("keyring_signature_invalid") from None
    return update


@dataclass(frozen=True, slots=True)
class BundleRecord:
    generation: str
    state: ManifestState
    key_id: str


@dataclass(frozen=True, slots=True)
class InstalledBundle:
    """A verified immutable generation; no provider is loaded by this object."""

    bundle_root: Path
    state: ManifestState
    key_id: str
    rollback: bool


@dataclass(frozen=True, slots=True)
class _KeyringSnapshot:
    sequence: int
    digest: str
    keyring: TrustedRecipeKeys
    history: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _InstallSnapshot:
    keyring: _KeyringSnapshot
    current: BundleRecord | None
    previous: BundleRecord | None


RollbackAuthorizer = Callable[[str, str], bool]


def _record_payload(record: BundleRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "generation": record.generation,
        "sequence": record.state.sequence,
        "bundle_version": record.state.bundle_version,
        "digest": record.state.digest,
        "key_id": record.key_id,
    }


def _parse_record(payload: Any) -> BundleRecord | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping) or set(payload) != {
        "generation",
        "sequence",
        "bundle_version",
        "digest",
        "key_id",
    }:
        raise BundleInstallError("bundle_install_state_invalid")
    generation = payload.get("generation")
    key_id = payload.get("key_id")
    if not isinstance(generation, str) or _SAFE_GENERATION.fullmatch(generation) is None:
        raise BundleInstallError("bundle_install_state_invalid")
    if not isinstance(key_id, str) or _SAFE_KEY_ID.fullmatch(key_id) is None:
        raise BundleInstallError("bundle_install_state_invalid")
    try:
        state = ManifestState(
            sequence=payload["sequence"],
            bundle_version=payload["bundle_version"],
            digest=payload["digest"],
        )
    except (KeyError, TypeError, ValueError):
        raise BundleInstallError("bundle_install_state_invalid") from None
    if generation != f"bundle-{state.digest}":
        raise BundleInstallError("bundle_install_state_invalid")
    return BundleRecord(generation=generation, state=state, key_id=key_id)


def _copy_stat_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        info = path.lstat()
    except OSError:
        raise BundleInstallError("bundle_entry_unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BundleInstallError("bundle_path_invalid")
    if getattr(info, "st_nlink", 1) != 1:
        raise BundleInstallError("bundle_hardlink_rejected")
    return (
        int(getattr(info, "st_dev", 0)),
        int(getattr(info, "st_ino", 0)),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", 0)),
    )


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability; file bytes are always fsynced first."""

    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise BundleInstallError("bundle_install_conflict") from None
    except OSError:
        raise BundleInstallError("bundle_install_io_failed") from None


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(12)}")
    try:
        _write_exclusive(temporary, payload)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BundleInstallError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            raise BundleInstallError("bundle_install_cleanup_pending") from None
        raise
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            raise BundleInstallError("bundle_install_cleanup_pending") from None
        raise BundleInstallError("bundle_install_io_failed") from None


class SignedBundleInstaller:
    """Install signed recipe bytes with an atomic pointer and explicit recovery."""

    def __init__(
        self,
        store_root: str | os.PathLike[str],
        bootstrap_keys: TrustedRecipeKeys,
        *,
        max_keyring_history: int = MAX_KEYRING_HISTORY,
    ) -> None:
        if not isinstance(bootstrap_keys, TrustedRecipeKeys):
            raise TypeError("bootstrap_keys must be TrustedRecipeKeys")
        if not 1 <= max_keyring_history <= MAX_KEYRING_HISTORY:
            raise ValueError("max_keyring_history is invalid")
        root = Path(store_root).expanduser()
        if root.exists() and _is_reparse_point(root):
            raise BundleInstallError("bundle_install_root_invalid")
        self.store_root = root.resolve(strict=False)
        self.bundle_root = self.store_root / "bundles"
        self.state_path = self.store_root / "state.json"
        self._bootstrap_keys = bootstrap_keys
        self._bootstrap_digest = _keyring_digest(0, bootstrap_keys)
        self._max_keyring_history = max_keyring_history
        try:
            self.bundle_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise BundleInstallError("bundle_install_root_unavailable") from None

    def _initial_keyring(self) -> _KeyringSnapshot:
        return _KeyringSnapshot(
            sequence=0,
            digest=self._bootstrap_digest,
            keyring=self._bootstrap_keys,
            history=(),
        )

    def _load_snapshot(self, *, validate_current: bool = True) -> _InstallSnapshot:
        if _is_reparse_point(self.state_path):
            raise BundleInstallError("bundle_install_state_invalid")
        if not self.state_path.exists():
            snapshot = _InstallSnapshot(self._initial_keyring(), None, None)
            return snapshot
        try:
            raw_bytes = self.state_path.read_bytes()
        except OSError:
            raise BundleInstallError("bundle_install_state_unavailable") from None
        if len(raw_bytes) > MAX_INSTALL_STATE_BYTES:
            raise BundleInstallError("bundle_install_state_invalid")
        try:
            raw = json.loads(raw_bytes.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BundleInstallError("bundle_install_state_invalid") from None
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "keyring_root_digest",
            "keyring_history",
            "current",
            "previous",
        } or raw.get("schema_version") != INSTALL_STATE_SCHEMA:
            raise BundleInstallError("bundle_install_state_invalid")
        if raw.get("keyring_root_digest") != self._bootstrap_digest:
            raise BundleInstallError("keyring_root_mismatch")
        history = raw.get("keyring_history")
        if not isinstance(history, list) or len(history) > self._max_keyring_history:
            raise BundleInstallError("keyring_history_invalid")
        keyring = self._initial_keyring()
        parsed_history: list[Mapping[str, Any]] = []
        for item in history:
            if not isinstance(item, Mapping):
                raise BundleInstallError("keyring_history_invalid")
            update = verify_keyring_update(
                item,
                keyring.keyring,
                current_sequence=keyring.sequence,
                current_digest=keyring.digest,
            )
            keyring = _KeyringSnapshot(
                sequence=update.sequence,
                digest=_keyring_digest(update.sequence, update.keyring),
                keyring=update.keyring,
                history=tuple(parsed_history + [dict(update.as_payload())]),
            )
            parsed_history.append(dict(update.as_payload()))
        current = _parse_record(raw.get("current"))
        previous = _parse_record(raw.get("previous"))
        snapshot = _InstallSnapshot(keyring, current, previous)
        if validate_current and current is not None:
            self._validate_record(current, keyring.keyring)
        return snapshot

    def _snapshot_payload(self, snapshot: _InstallSnapshot) -> dict[str, Any]:
        return {
            "schema_version": INSTALL_STATE_SCHEMA,
            "keyring_root_digest": self._bootstrap_digest,
            "keyring_history": [dict(item) for item in snapshot.keyring.history],
            "current": _record_payload(snapshot.current),
            "previous": _record_payload(snapshot.previous),
        }

    def _persist_snapshot(self, snapshot: _InstallSnapshot) -> None:
        encoded = _canonical(self._snapshot_payload(snapshot))
        if len(encoded) > MAX_INSTALL_STATE_BYTES:
            raise BundleInstallError("bundle_install_state_too_large")
        _atomic_write(self.state_path, encoded)

    @contextmanager
    def _store_lock(self):
        """Serialize state transitions across threads and installer processes."""

        lock_path = self.store_root / ".install.lock"
        try:
            self.store_root.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        except OSError:
            raise BundleInstallError("bundle_install_lock_unavailable") from None
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        except BundleInstallError:
            raise
        except OSError:
            raise BundleInstallError("bundle_install_lock_unavailable") from None
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)

    @staticmethod
    def _bundle_path(root: Path, record: BundleRecord) -> Path:
        return root / record.generation

    @staticmethod
    def _reject_reserved_entries(manifest: SignedRecipeManifest) -> None:
        if any(entry.bundle_path == "manifest.json" for entry in manifest.entries):
            raise BundleInstallError("bundle_install_reserved_path")

    def _validate_exact_tree(self, root: Path, manifest: SignedRecipeManifest) -> None:
        expected_files = {"manifest.json", *(entry.bundle_path for entry in manifest.entries)}
        expected_dirs = {Path(".")}
        for relative in expected_files:
            expected_dirs.update(Path(relative).parents)
        try:
            if _is_reparse_point(root) or not root.is_dir():
                raise BundleInstallError("bundle_install_recovery_failed")
            for path in root.rglob("*"):
                relative = path.relative_to(root).as_posix()
                if _is_reparse_point(path):
                    raise BundleInstallError("bundle_install_recovery_failed")
                if path.is_dir():
                    if Path(relative) not in expected_dirs:
                        raise BundleInstallError("bundle_extra_path")
                    continue
                if not path.is_file() or relative not in expected_files:
                    raise BundleInstallError("bundle_extra_path")
                if getattr(path.stat(), "st_nlink", 1) != 1:
                    raise BundleInstallError("bundle_hardlink_rejected")
        except BundleInstallError:
            raise
        except OSError:
            raise BundleInstallError("bundle_install_recovery_failed") from None

    def _validate_record_at(
        self,
        root: Path,
        record: BundleRecord,
        keyring: TrustedRecipeKeys,
    ) -> SignedRecipeManifest:
        manifest_path = root / "manifest.json"
        try:
            if _is_reparse_point(root) or not root.is_dir() or _is_reparse_point(manifest_path):
                raise BundleInstallError("bundle_install_recovery_failed")
            raw = manifest_path.read_bytes()
        except BundleInstallError:
            raise
        except OSError:
            raise BundleInstallError("bundle_install_recovery_failed") from None
        if len(raw) > MAX_MANIFEST_BYTES:
            raise BundleInstallError("bundle_install_recovery_failed")
        try:
            payload = json.loads(raw.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BundleInstallError("bundle_install_recovery_failed") from None
        try:
            verified = verify_manifest_signature(payload, keyring)
            self._reject_reserved_entries(verified.manifest)
            if (
                verified.digest != record.state.digest
                or verified.manifest.key_id != record.key_id
                or verified.state != record.state
            ):
                raise BundleInstallError("bundle_install_recovery_failed")
            self._validate_exact_tree(root, verified.manifest)
            verify_bundle_files(verified.manifest, root)
        except (ManifestVerificationError, BundleInstallError):
            raise BundleInstallError("bundle_install_recovery_failed") from None
        return verified.manifest

    def _validate_record(self, record: BundleRecord, keyring: TrustedRecipeKeys) -> SignedRecipeManifest:
        return self._validate_record_at(
            self._bundle_path(self.bundle_root, record),
            record,
            keyring,
        )

    def status(self) -> InstalledBundle | None:
        """Validate and return the active generation, or ``None`` before first install."""

        with self._store_lock():
            snapshot = self._load_snapshot()
            if snapshot.current is None:
                return None
            return InstalledBundle(
                bundle_root=self._bundle_path(self.bundle_root, snapshot.current),
                state=snapshot.current.state,
                key_id=snapshot.current.key_id,
                rollback=False,
            )

    def _source_path(self, source_root: Path, relative: str) -> Path:
        candidate = source_root / relative
        try:
            resolved = candidate.resolve(strict=True)
            root = source_root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise BundleInstallError("bundle_entry_unavailable") from None
        cursor = root
        for component in Path(relative).parts:
            cursor = cursor / component
            if _is_reparse_point(cursor):
                raise BundleInstallError("bundle_path_invalid")
        if not resolved.is_relative_to(root) or _is_reparse_point(resolved):
            raise BundleInstallError("bundle_path_invalid")
        return candidate

    def _copy_entry(self, source: Path, destination: Path, expected_size: int, expected_digest: str) -> None:
        before = _copy_stat_identity(source)
        digest = sha256()
        total = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
                while True:
                    chunk = source_stream.read(MAX_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_size or total > 128 * 1024 * 1024:
                        raise BundleInstallError("bundle_size_mismatch")
                    digest.update(chunk)
                    destination_stream.write(chunk)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
        except BundleInstallError:
            raise
        except OSError:
            raise BundleInstallError("bundle_entry_unavailable") from None
        after = _copy_stat_identity(source)
        if before != after or total != expected_size or digest.hexdigest() != expected_digest:
            raise BundleInstallError("bundle_source_changed")

    def _stage_bundle(
        self,
        source_root: Path,
        verified: VerifiedRecipeManifest,
    ) -> Path:
        source_root = Path(source_root)
        if _is_reparse_point(source_root):
            raise BundleInstallError("bundle_root_unavailable")
        try:
            source_root = source_root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise BundleInstallError("bundle_root_unavailable") from None
        if not source_root.is_dir():
            raise BundleInstallError("bundle_root_unavailable")
        verify_bundle_files(verified.manifest, source_root)
        self._reject_reserved_entries(verified.manifest)
        staging = self.bundle_root / f".staging-{secrets.token_hex(16)}"
        final = self.bundle_root / f"bundle-{verified.digest}"
        try:
            staging.mkdir()
            for entry in verified.manifest.entries:
                source = self._source_path(source_root, entry.bundle_path)
                self._copy_entry(
                    source,
                    staging / entry.bundle_path,
                    entry.size,
                    entry.sha256,
                )
            _write_exclusive(
                staging / "manifest.json",
                verified.manifest.canonical_json().encode("ascii"),
            )
            candidate = BundleRecord(
                generation=f"bundle-{verified.digest}",
                state=verified.state,
                key_id=verified.manifest.key_id,
            )
            self._validate_record_at(staging, candidate, self._load_snapshot().keyring.keyring)
            if final.exists():
                if _is_reparse_point(final) or not final.is_dir():
                    raise BundleInstallError("bundle_install_conflict")
                self._validate_record(candidate, self._load_snapshot().keyring.keyring)
                shutil.rmtree(staging)
                return final
            os.replace(staging, final)
            _fsync_directory(self.bundle_root)
            return final
        except BundleInstallError:
            try:
                if staging.exists():
                    shutil.rmtree(staging)
            except OSError:
                raise BundleInstallError("bundle_install_cleanup_pending") from None
            raise
        except OSError:
            try:
                if staging.exists():
                    shutil.rmtree(staging)
            except OSError:
                raise BundleInstallError("bundle_install_cleanup_pending") from None
            raise BundleInstallError("bundle_install_io_failed") from None

    @staticmethod
    def _authorize_rollback(
        authorizer: RollbackAuthorizer | None,
        current_digest: str,
        candidate_digest: str,
    ) -> bool:
        if authorizer is None:
            return False
        try:
            return bool(authorizer(current_digest, candidate_digest))
        except Exception:
            raise BundleInstallError("bundle_install_rollback_authorization_failed") from None

    def _install_locked(
        self,
        manifest_payload: Mapping[str, Any],
        source_root: str | os.PathLike[str],
        *,
        rollback_authorizer: RollbackAuthorizer | None = None,
    ) -> InstalledBundle:
        """Verify, stage, and atomically activate one signed bundle generation."""

        snapshot = self._load_snapshot()
        current_state = snapshot.current.state if snapshot.current is not None else None
        signature_verified = verify_manifest_signature(manifest_payload, snapshot.keyring.keyring)
        rollback_authorized = False
        if current_state is not None and signature_verified.manifest.rollback_of is not None:
            rollback_authorized = self._authorize_rollback(
                rollback_authorizer,
                current_state.digest,
                signature_verified.digest,
            )
        verified = verify_signed_manifest(
            manifest_payload,
            snapshot.keyring.keyring,
            current=current_state,
            rollback_authorized=rollback_authorized,
        )
        final = self._stage_bundle(Path(source_root), verified)
        record = BundleRecord(
            generation=f"bundle-{verified.digest}",
            state=verified.state,
            key_id=verified.manifest.key_id,
        )
        next_snapshot = _InstallSnapshot(
            keyring=snapshot.keyring,
            current=record,
            previous=snapshot.current,
        )
        self._persist_snapshot(next_snapshot)
        return InstalledBundle(
            bundle_root=final,
            state=record.state,
            key_id=record.key_id,
            rollback=verified.rollback,
        )

    def install(
        self,
        manifest_payload: Mapping[str, Any],
        source_root: str | os.PathLike[str],
        *,
        rollback_authorizer: RollbackAuthorizer | None = None,
    ) -> InstalledBundle:
        """Verify, stage, and atomically activate one signed bundle generation."""

        with self._store_lock():
            return self._install_locked(
                manifest_payload,
                source_root,
                rollback_authorizer=rollback_authorizer,
            )

    def _rotate_keyring_locked(self, payload: Mapping[str, Any]) -> None:
        """Atomically persist a chained keyring update before new-key installs."""

        snapshot = self._load_snapshot()
        update = verify_keyring_update(
            payload,
            snapshot.keyring.keyring,
            current_sequence=snapshot.keyring.sequence,
            current_digest=snapshot.keyring.digest,
        )
        if snapshot.current is not None and snapshot.current.key_id in update.keyring.revoked:
            raise BundleInstallError("keyring_active_key_revoked")
        next_keyring = _KeyringSnapshot(
            sequence=update.sequence,
            digest=_keyring_digest(update.sequence, update.keyring),
            keyring=update.keyring,
            history=snapshot.keyring.history + (update.as_payload(),),
        )
        if len(next_keyring.history) > self._max_keyring_history:
            raise BundleInstallError("keyring_history_invalid")
        if snapshot.current is not None:
            self._validate_record(snapshot.current, next_keyring.keyring)
        self._persist_snapshot(
            _InstallSnapshot(next_keyring, snapshot.current, snapshot.previous)
        )

    def rotate_keyring(self, payload: Mapping[str, Any]) -> None:
        """Atomically persist a chained keyring update before new-key installs."""

        with self._store_lock():
            self._rotate_keyring_locked(payload)

    def _recover_previous_locked(
        self,
        rollback_authorizer: RollbackAuthorizer | None = None,
    ) -> InstalledBundle:
        """Explicitly activate the retained previous generation after validation."""

        snapshot = self._load_snapshot(validate_current=False)
        if snapshot.current is None or snapshot.previous is None:
            raise BundleInstallError("bundle_recovery_unavailable")
        if not self._authorize_rollback(
            rollback_authorizer,
            snapshot.current.state.digest,
            snapshot.previous.state.digest,
        ):
            raise BundleInstallError("bundle_recovery_not_authorized")
        self._validate_record(snapshot.previous, snapshot.keyring.keyring)
        next_snapshot = _InstallSnapshot(
            keyring=snapshot.keyring,
            current=snapshot.previous,
            previous=None,
        )
        self._persist_snapshot(next_snapshot)
        return InstalledBundle(
            bundle_root=self._bundle_path(self.bundle_root, snapshot.previous),
            state=snapshot.previous.state,
            key_id=snapshot.previous.key_id,
            rollback=True,
        )

    def recover_previous(self, rollback_authorizer: RollbackAuthorizer | None = None) -> InstalledBundle:
        """Explicitly activate the retained previous generation after validation."""

        with self._store_lock():
            return self._recover_previous_locked(rollback_authorizer)

    def _cleanup_locked(self) -> None:
        """Remove only installer-owned orphan staging/generation directories."""

        snapshot = self._load_snapshot(validate_current=False)
        protected = {
            record.generation
            for record in (snapshot.current, snapshot.previous)
            if record is not None
        }
        try:
            for path in self.bundle_root.iterdir():
                if path.name.startswith(".staging-") or (
                    path.name.startswith("bundle-") and path.name not in protected
                ):
                    if _is_reparse_point(path):
                        raise BundleInstallError("bundle_cleanup_unsafe")
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        raise BundleInstallError("bundle_cleanup_unsafe")
        except BundleInstallError:
            raise
        except OSError:
            raise BundleInstallError("bundle_cleanup_pending") from None

    def cleanup(self) -> None:
        """Remove only installer-owned orphan staging/generation directories."""

        with self._store_lock():
            self._cleanup_locked()


__all__ = [
    "BundleInstallError",
    "BundleRecord",
    "InstalledBundle",
    "KeyringUpdate",
    "RollbackAuthorizer",
    "SignedBundleInstaller",
    "parse_keyring_update",
    "verify_keyring_update",
]
