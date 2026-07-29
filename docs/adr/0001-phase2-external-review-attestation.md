# ADR-0001 Phase 2 external release-review attestation

- **Status:** Verification contract implemented; production approval remains external
- **Phase:** 2 - fixed-function image provider
- **Parent:** [Phase 2 release and lifecycle health preflight](0001-phase2-release-lifecycle-gate.md)
- **Depends on:** [packaged worker release qualification](0001-phase2-worker-release-qualification.md)
- **Scope:** Parse and verify an out-of-band review decision; no provider enablement

## Decision

The external-review callback used by `RecipeRuntimeReleaseGate` now has a concrete,
independent verification contract. A `recipe.release-review.v1` attestation is a
strict Ed25519-signed approval that binds all of the following to one review:

- the immutable release commit;
- the installed signed-worker bundle/manifest digest;
- the worker signing key identifier;
- the reviewed native launcher scope; and
- the exact threat-model version.

The attestation also carries a bounded review identifier, issue and expiry times,
an explicit `approved` decision, and a separate review signer key identifier.
`TrustedReviewKeys` is intentionally distinct from the worker `TrustedRecipeKeys`
root so a worker signing key cannot silently become a security-review authority.

`ReleaseReviewProbe` adapts the verifier to the existing zero-argument
`ExternalReviewProbe` callback. It returns only a bounded `RuntimeHealth` result;
it never returns signature bytes, paths, reviewer data, or exception text.

## Verification rules

The verifier fails closed when any rule is violated:

1. The payload is ASCII-canonical, bounded to 64 KiB, schema-exact, and rejects
   unknown fields, non-canonical identifiers, malformed digests, invalid timestamps,
   overlong validity windows, and non-approval decisions.
2. The review key must be present in the independently pinned, non-revoked review
   key root. Ed25519 verification covers the canonical payload without its
   signature field.
3. The attestation must be within its issue/expiry window, with at most five
   minutes of explicit clock skew.
4. Every release target field must match the caller-supplied target using bounded
   constant-time comparisons.
5. Parse, trust, signature, freshness, and target failures map to stable redacted
   categories. No local file, process, broker, provider, or lifecycle operation is
   performed by the verifier.

## Release interpretation

This ADR makes the external-review boundary implementable and testable; it is not
an approval record. No production review payload, review private key, worker
private key, public trust root, or signed generation is committed. A production
caller must obtain the out-of-band review record, verify it against the exact
release commit/package/launcher/threat-model target, and supply the independently
pinned review key root. The provider remains absent until that review, the signed
production installation, real-worker resource enforcement, and the separate
`ExecutionLifecycle` enablement change all pass.

## Verification

`tests/test_phase2_release_attestation.py` covers valid binding, each release
identity mismatch, payload tampering, key revocation/untrust, freshness and maximum
validity, unknown fields, malformed signatures, size bounds, and probe redaction.
The tests use generated disposable keys and fixed timestamps only; no production
trust material is created or retained.
