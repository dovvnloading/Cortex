"""Fail-closed verification of an external Phase 2 release review.

The release/lifecycle preflight deliberately accepts an injected health callback
instead of inferring approval from local configuration.  This module supplies
the safe callback implementation: an independently trusted Ed25519 attestation
must bind the review to the exact release commit, installed bundle digest,
worker signing key, launcher scope, and threat-model version.

The verifier performs no filesystem, process, broker, provider, or lifecycle
operations.  It only parses bounded signed data and returns stable, redacted
failure categories.  Production callers must provide the review payload and
review trust root out of band; this module does not create or persist either.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
import re
import time
from typing import Any, Callable, Literal, Mapping

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .lifecycle import RuntimeHealth


RELEASE_REVIEW_SCHEMA = "recipe.release-review.v1"
MAX_REVIEW_ATTESTATION_BYTES = 64 * 1024
MAX_REVIEW_VALIDITY_SECONDS = 366 * 24 * 60 * 60
MAX_CLOCK_SKEW_SECONDS = 5 * 60

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SAFE_SCOPE = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^[A-Za-z0-9_-]{86}$")

_PUBLIC_MESSAGES = {
    "attestation_invalid": "External security review attestation is invalid.",
    "attestation_too_large": "External security review attestation is too large.",
    "attestation_policy_invalid": "External security review policy is invalid.",
    "attestation_clock_invalid": "External security review clock is invalid.",
    "review_key_untrusted": "External security review signer is not trusted.",
    "attestation_signature_invalid": "External security review attestation signature is invalid.",
    "attestation_not_yet_valid": "External security review attestation is not yet valid.",
    "attestation_expired": "External security review attestation has expired.",
    "attestation_release_mismatch": "External review does not match this release.",
    "attestation_bundle_mismatch": "External review does not match the installed bundle.",
    "attestation_worker_key_mismatch": "External review does not match the worker signer.",
    "attestation_scope_mismatch": "External review does not match the launcher scope.",
    "attestation_threat_model_mismatch": "External review does not match the threat model.",
    "attestation_check_failed": "External security review could not be verified safely.",
}


class ReleaseReviewVerificationError(ValueError):
    """Stable, non-sensitive external-review failure category."""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("invalid release review code")
        self.code = code
        super().__init__(self.public_message)

    @property
    def public_message(self) -> str:
        return _PUBLIC_MESSAGES.get(
            self.code,
            "External security review could not be verified safely.",
        )


@dataclass(frozen=True, slots=True)
class ReleaseReviewTarget:
    """Immutable release identity that an attestation must approve."""

    release_commit: str
    bundle_digest: str
    worker_key_id: str
    launcher_scope: str
    threat_model_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.release_commit, str) or _COMMIT.fullmatch(self.release_commit) is None:
            raise ValueError("release commit is invalid")
        if not isinstance(self.bundle_digest, str) or _SHA256.fullmatch(self.bundle_digest) is None:
            raise ValueError("bundle digest is invalid")
        if not isinstance(self.worker_key_id, str) or _SAFE_ID.fullmatch(self.worker_key_id) is None:
            raise ValueError("worker key id is invalid")
        if not isinstance(self.launcher_scope, str) or _SAFE_SCOPE.fullmatch(self.launcher_scope) is None:
            raise ValueError("launcher scope is invalid")
        if not isinstance(self.threat_model_version, str) or _SEMVER.fullmatch(self.threat_model_version) is None:
            raise ValueError("threat model version is invalid")


class _ReleaseReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def canonical_json(self) -> bytes:
        try:
            return json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
        except (TypeError, ValueError, OverflowError, UnicodeEncodeError):
            raise ReleaseReviewVerificationError("attestation_invalid") from None


class ReleaseReviewAttestation(_ReleaseReviewModel):
    """Strict signed review decision; no production approval is embedded here."""

    schema_version: Literal["recipe.release-review.v1"]
    review_id: str
    review_key_id: str
    decision: Literal["approved"]
    release_commit: str
    bundle_digest: str
    worker_key_id: str
    launcher_scope: str
    threat_model_version: str
    issued_at: int = Field(strict=True, ge=1)
    expires_at: int = Field(strict=True, ge=1)
    signature: str

    @field_validator("review_id", "review_key_id", "worker_key_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("review identifier is invalid")
        return value

    @field_validator("release_commit")
    @classmethod
    def _validate_commit(cls, value: str) -> str:
        if _COMMIT.fullmatch(value) is None:
            raise ValueError("release commit is invalid")
        return value

    @field_validator("bundle_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("bundle digest is invalid")
        return value

    @field_validator("launcher_scope")
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        if _SAFE_SCOPE.fullmatch(value) is None:
            raise ValueError("launcher scope is invalid")
        return value

    @field_validator("threat_model_version")
    @classmethod
    def _validate_threat_model(cls, value: str) -> str:
        if _SEMVER.fullmatch(value) is None:
            raise ValueError("threat model version is invalid")
        return value

    @field_validator("signature")
    @classmethod
    def _validate_signature(cls, value: str) -> str:
        if _SIGNATURE.fullmatch(value) is None:
            raise ValueError("signature encoding is invalid")
        return value

    @model_validator(mode="after")
    def _validate_window(self) -> "ReleaseReviewAttestation":
        if self.expires_at <= self.issued_at:
            raise ValueError("review expiry must be after issue time")
        if self.expires_at - self.issued_at > MAX_REVIEW_VALIDITY_SECONDS:
            raise ValueError("review validity window is too long")
        return self

    def signed_payload(self) -> bytes:
        payload = self.model_dump(mode="json")
        payload.pop("signature", None)
        try:
            return json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
        except (TypeError, ValueError, OverflowError, UnicodeEncodeError):
            raise ReleaseReviewVerificationError("attestation_invalid") from None

    def digest(self) -> str:
        return sha256(self.canonical_json()).hexdigest()


class TrustedReviewKeys:
    """Pinned review public keys kept separate from the worker trust root."""

    def __init__(self, keys: Mapping[str, bytes], *, revoked: frozenset[str] = frozenset()) -> None:
        if not isinstance(keys, Mapping) or not 1 <= len(keys) <= 32:
            raise ValueError("review trust root is invalid")
        normalized: dict[str, bytes] = {}
        for key_id, key_bytes in keys.items():
            if (
                not isinstance(key_id, str)
                or _SAFE_ID.fullmatch(key_id) is None
                or not isinstance(key_bytes, bytes)
                or len(key_bytes) != 32
            ):
                raise ValueError("review trust root is invalid")
            normalized[key_id] = bytes(key_bytes)
        if not isinstance(revoked, frozenset) or not revoked.issubset(normalized):
            raise ValueError("review trust root revocation is invalid")
        self._keys = normalized
        self._revoked = frozenset(revoked)

    def public_key(self, key_id: str) -> Ed25519PublicKey:
        if key_id not in self._keys or key_id in self._revoked:
            raise ReleaseReviewVerificationError("review_key_untrusted")
        try:
            return Ed25519PublicKey.from_public_bytes(self._keys[key_id])
        except (TypeError, ValueError, UnsupportedAlgorithm):
            raise ReleaseReviewVerificationError("review_key_untrusted") from None

    @property
    def keys(self) -> Mapping[str, bytes]:
        return dict(self._keys)

    @property
    def revoked(self) -> frozenset[str]:
        return self._revoked

    def digest(self) -> str:
        payload = {
            "keys": {
                key_id: base64.urlsafe_b64encode(self._keys[key_id]).decode("ascii").rstrip("=")
                for key_id in sorted(self._keys)
            },
            "revoked": sorted(self._revoked),
        }
        return sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
                "ascii"
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedReleaseReview:
    """Verified review metadata safe to pass to diagnostics."""

    attestation: ReleaseReviewAttestation
    digest: str
    target: ReleaseReviewTarget


def _decode_signature(value: str) -> bytes:
    if _SIGNATURE.fullmatch(value) is None:
        raise ReleaseReviewVerificationError("attestation_signature_invalid")
    try:
        decoded = base64.b64decode(value + "==", altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise ReleaseReviewVerificationError("attestation_signature_invalid") from None
    if len(decoded) != 64:
        raise ReleaseReviewVerificationError("attestation_signature_invalid")
    return decoded


def parse_release_review_attestation(
    payload: Mapping[str, Any] | ReleaseReviewAttestation,
) -> ReleaseReviewAttestation:
    """Parse a bounded, strict review payload without accepting unknown fields."""

    if isinstance(payload, ReleaseReviewAttestation):
        return payload
    if not isinstance(payload, Mapping):
        raise ReleaseReviewVerificationError("attestation_invalid")
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError, UnicodeEncodeError):
        raise ReleaseReviewVerificationError("attestation_invalid") from None
    if len(encoded) > MAX_REVIEW_ATTESTATION_BYTES:
        raise ReleaseReviewVerificationError("attestation_too_large")
    try:
        return ReleaseReviewAttestation.model_validate(payload)
    except (ValidationError, TypeError, ValueError):
        raise ReleaseReviewVerificationError("attestation_invalid") from None


def verify_release_review(
    payload: Mapping[str, Any] | ReleaseReviewAttestation,
    trusted_keys: TrustedReviewKeys,
    target: ReleaseReviewTarget,
    *,
    now: int | None = None,
    clock_skew_seconds: int = MAX_CLOCK_SKEW_SECONDS,
) -> VerifiedReleaseReview:
    """Verify an approval against the exact release identity and time window."""

    if not isinstance(trusted_keys, TrustedReviewKeys) or not isinstance(
        target, ReleaseReviewTarget
    ):
        raise ReleaseReviewVerificationError("attestation_policy_invalid")
    if type(clock_skew_seconds) is not int or not 0 <= clock_skew_seconds <= MAX_CLOCK_SKEW_SECONDS:
        raise ReleaseReviewVerificationError("attestation_policy_invalid")
    if now is None:
        current_time = int(time.time())
    elif type(now) is int and now >= 0:
        current_time = now
    else:
        raise ReleaseReviewVerificationError("attestation_clock_invalid")

    attestation = parse_release_review_attestation(payload)
    public_key = trusted_keys.public_key(attestation.review_key_id)
    signature = _decode_signature(attestation.signature)
    try:
        public_key.verify(signature, attestation.signed_payload())
    except (InvalidSignature, TypeError, ValueError):
        raise ReleaseReviewVerificationError("attestation_signature_invalid") from None

    if attestation.issued_at > current_time + clock_skew_seconds:
        raise ReleaseReviewVerificationError("attestation_not_yet_valid")
    if attestation.expires_at <= current_time - clock_skew_seconds:
        raise ReleaseReviewVerificationError("attestation_expired")

    comparisons = (
        ("release_commit", "attestation_release_mismatch"),
        ("bundle_digest", "attestation_bundle_mismatch"),
        ("worker_key_id", "attestation_worker_key_mismatch"),
        ("launcher_scope", "attestation_scope_mismatch"),
        ("threat_model_version", "attestation_threat_model_mismatch"),
    )
    for field_name, code in comparisons:
        if not hmac.compare_digest(
            str(getattr(attestation, field_name)),
            str(getattr(target, field_name)),
        ):
            raise ReleaseReviewVerificationError(code)

    return VerifiedReleaseReview(
        attestation=attestation,
        digest=attestation.digest(),
        target=target,
    )


class ReleaseReviewProbe:
    """Callable ``RuntimeHealth`` adapter for ``RecipeRuntimeReleaseGate``."""

    def __init__(
        self,
        payload: Mapping[str, Any] | ReleaseReviewAttestation,
        trusted_keys: TrustedReviewKeys,
        target: ReleaseReviewTarget,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._attestation = parse_release_review_attestation(payload)
        if not isinstance(trusted_keys, TrustedReviewKeys):
            raise TypeError("trusted review keys are required")
        if not isinstance(target, ReleaseReviewTarget):
            raise TypeError("release review target is required")
        if clock is not None and not callable(clock):
            raise TypeError("review clock must be callable")
        self._trusted_keys = trusted_keys
        self._target = target
        self._clock = clock or (lambda: int(time.time()))

    def __call__(self) -> RuntimeHealth:
        try:
            verify_release_review(
                self._attestation,
                self._trusted_keys,
                self._target,
                now=self._clock(),
            )
        except ReleaseReviewVerificationError as error:
            return RuntimeHealth.blocked(error.code, error.public_message)
        except Exception:
            return RuntimeHealth.blocked(
                "attestation_check_failed",
                _PUBLIC_MESSAGES["attestation_check_failed"],
            )
        return RuntimeHealth.ready("External security review is verified for this release.")


__all__ = [
    "MAX_CLOCK_SKEW_SECONDS",
    "MAX_REVIEW_ATTESTATION_BYTES",
    "MAX_REVIEW_VALIDITY_SECONDS",
    "RELEASE_REVIEW_SCHEMA",
    "ReleaseReviewAttestation",
    "ReleaseReviewProbe",
    "ReleaseReviewTarget",
    "ReleaseReviewVerificationError",
    "TrustedReviewKeys",
    "VerifiedReleaseReview",
    "parse_release_review_attestation",
    "verify_release_review",
]
