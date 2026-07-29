"""Adversarial tests for the external Phase 2 release-review contract."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cortex_backend.execution.lifecycle import RuntimeHealth
from cortex_backend.execution.release_attestation import (
    ReleaseReviewProbe,
    ReleaseReviewTarget,
    ReleaseReviewVerificationError,
    TrustedReviewKeys,
    verify_release_review,
)


NOW = 2_000_000
TARGET = ReleaseReviewTarget(
    release_commit="a" * 40,
    bundle_digest="b" * 64,
    worker_key_id="release-1",
    launcher_scope="native.recipe.launcher.v1",
    threat_model_version="2026.7.1",
)


def _public_key(signer: Ed25519PrivateKey) -> bytes:
    return signer.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _signed_payload(
    signer: Ed25519PrivateKey,
    *,
    issued_at: int = NOW - 60,
    expires_at: int = NOW + 3600,
    **overrides: object,
) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "schema_version": "recipe.release-review.v1",
        "review_id": "review-20260729-1",
        "review_key_id": "review-1",
        "decision": "approved",
        "release_commit": TARGET.release_commit,
        "bundle_digest": TARGET.bundle_digest,
        "worker_key_id": TARGET.worker_key_id,
        "launcher_scope": TARGET.launcher_scope,
        "threat_model_version": TARGET.threat_model_version,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    unsigned.update(overrides)
    encoded = json.dumps(
        unsigned,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    signature = base64.urlsafe_b64encode(signer.sign(encoded)).decode("ascii").rstrip("=")
    return {**unsigned, "signature": signature}


def _keys(signer: Ed25519PrivateKey) -> TrustedReviewKeys:
    return TrustedReviewKeys({"review-1": _public_key(signer)})


def test_valid_attestation_binds_all_release_identity_fields():
    signer = Ed25519PrivateKey.generate()
    payload = _signed_payload(signer)

    verified = verify_release_review(payload, _keys(signer), TARGET, now=NOW)

    assert verified.target == TARGET
    assert verified.attestation.review_id == "review-20260729-1"
    assert len(verified.digest) == 64
    health = ReleaseReviewProbe(payload, _keys(signer), TARGET, clock=lambda: NOW)()
    assert health == RuntimeHealth.ready(
        "External security review is verified for this release."
    )


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("release_commit", "attestation_release_mismatch"),
        ("bundle_digest", "attestation_bundle_mismatch"),
        ("worker_key_id", "attestation_worker_key_mismatch"),
        ("launcher_scope", "attestation_scope_mismatch"),
        ("threat_model_version", "attestation_threat_model_mismatch"),
    ],
)
def test_signed_but_wrong_release_identity_is_rejected(field: str, code: str):
    signer = Ed25519PrivateKey.generate()
    replacement = {
        "release_commit": "c" * 40,
        "bundle_digest": "d" * 64,
        "worker_key_id": "release-2",
        "launcher_scope": "native.recipe.launcher.v2",
        "threat_model_version": "2026.8.1",
    }[field]
    payload = _signed_payload(signer, **{field: replacement})

    with pytest.raises(ReleaseReviewVerificationError) as error:
        verify_release_review(payload, _keys(signer), TARGET, now=NOW)

    assert error.value.code == code
    assert str(error.value) == error.value.public_message


def test_tampered_payload_fails_signature_before_target_binding():
    signer = Ed25519PrivateKey.generate()
    payload = _signed_payload(signer)
    payload["bundle_digest"] = "d" * 64

    with pytest.raises(ReleaseReviewVerificationError) as error:
        verify_release_review(payload, _keys(signer), TARGET, now=NOW)

    assert error.value.code == "attestation_signature_invalid"


@pytest.mark.parametrize(
    ("issued_at", "expires_at", "code"),
    [
        (NOW + 600, NOW + 3600, "attestation_not_yet_valid"),
        (NOW - 3600, NOW - 60, "attestation_expired"),
        (NOW - 1, NOW + 366 * 24 * 60 * 60 + 1, "attestation_invalid"),
    ],
)
def test_attestation_time_window_is_bounded_and_fresh(issued_at: int, expires_at: int, code: str):
    signer = Ed25519PrivateKey.generate()
    payload = _signed_payload(signer, issued_at=issued_at, expires_at=expires_at)

    with pytest.raises(ReleaseReviewVerificationError) as error:
        verify_release_review(payload, _keys(signer), TARGET, now=NOW, clock_skew_seconds=0)

    assert error.value.code == code


def test_unknown_revoked_and_wrong_review_keys_fail_closed():
    signer = Ed25519PrivateKey.generate()
    payload = _signed_payload(signer)

    revoked = TrustedReviewKeys({"review-1": _public_key(signer)}, revoked=frozenset({"review-1"}))
    with pytest.raises(ReleaseReviewVerificationError) as revoked_error:
        verify_release_review(payload, revoked, TARGET, now=NOW)
    assert revoked_error.value.code == "review_key_untrusted"

    payload = _signed_payload(signer, review_key_id="review-unknown")
    with pytest.raises(ReleaseReviewVerificationError) as unknown_error:
        verify_release_review(payload, _keys(signer), TARGET, now=NOW)
    assert unknown_error.value.code == "review_key_untrusted"


def test_unknown_fields_invalid_signature_and_oversized_payload_are_redacted():
    signer = Ed25519PrivateKey.generate()
    payload = _signed_payload(signer)
    payload["unexpected"] = "do not accept"
    with pytest.raises(ReleaseReviewVerificationError) as unknown_error:
        verify_release_review(payload, _keys(signer), TARGET, now=NOW)
    assert unknown_error.value.code == "attestation_invalid"

    malformed = _signed_payload(signer)
    malformed["signature"] = "not-a-signature"
    with pytest.raises(ReleaseReviewVerificationError) as signature_error:
        verify_release_review(malformed, _keys(signer), TARGET, now=NOW)
    assert signature_error.value.code == "attestation_invalid"

    oversized = _signed_payload(signer)
    oversized["review_id"] = "a" * 64
    oversized["launcher_scope"] = "a" * 128
    oversized["threat_model_version"] = "1.2.3"
    oversized["extra"] = "x" * 70_000
    with pytest.raises(ReleaseReviewVerificationError) as size_error:
        verify_release_review(oversized, _keys(signer), TARGET, now=NOW)
    assert size_error.value.code == "attestation_too_large"


def test_probe_redacts_clock_and_policy_failures():
    signer = Ed25519PrivateKey.generate()
    payload = _signed_payload(signer)
    probe = ReleaseReviewProbe(
        payload,
        _keys(signer),
        TARGET,
        clock=lambda: (_ for _ in ()).throw(RuntimeError("secret clock path")),
    )

    health = probe()

    assert health.available is False
    assert health.code == "attestation_check_failed"
    assert "secret" not in health.message.lower()
