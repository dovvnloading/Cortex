# ADR-0001 Phase 2 signed recipe manifest and rollback gate

- **Status:** Implemented and verified; the storage installer is complete and provider enablement remains blocked
- **Parent:** [Phase 2 typed recipe contract](0001-phase2-recipe-contract.md)
- **Scope:** Canonical Ed25519 manifest signatures, pinned bundle bytes, monotonic
  update policy, and explicit rollback authorization

## Decision

Recipe bundles are described by a strict `recipe.manifest.v1` document. The signed
payload contains a schema version, trusted key identifier, monotonic sequence,
semantic bundle version, optional rollback target, and a bounded list of entries. Each
entry pins an opaque recipe id, safe relative bundle path, typed entrypoint, version,
exact byte size, and lowercase SHA-256 digest.

The `resource` entrypoint is reserved for inert files in a one-folder worker
dependency closure; it is never a launch role. Exactly one `image_transform` entry at
the fixed `recipe_worker.exe` path is required by the separate worker-provenance
boundary.

The signature is Ed25519 over canonical UTF-8 JSON with sorted keys, compact separators,
and the signature field removed. Verification uses a packaged allowlist of raw 32-byte
public keys selected by a bounded key id. Unknown or revoked keys are rejected. The
implementation follows the `cryptography` Ed25519 sign/verify contract documented by
the library maintainers: verification raises on an invalid signature and public keys
are loaded from raw 32-byte bytes.

## Update and rollback policy

`ManifestState` records the accepted sequence, bundle version, and full manifest digest.
An update must use a strictly greater sequence and may not lower the semantic bundle
version. Equal or lower sequences are replayed state, never a reason to replace the
current bundle.

A rollback is still a new, strictly greater sequence. It must carry `rollback_of` equal
to the exact current manifest digest and must be accompanied by a separate trusted
local rollback authorization. A signed manifest alone cannot authorize rollback. This
prevents a compromised or stale feed from silently downgrading a healthy installation.

The verifier returns a candidate state but does not persist it. The storage boundary
in [the bundle installation ADR](0001-phase2-bundle-installation.md) verifies the
complete bundle, atomically installs an immutable digest generation, persists the
candidate state only after successful installation, and retains the previous verified
state for explicit recovery. Power loss, partial replacement, failed verification,
and failed startup leave the previously accepted state active or leave execution
unavailable; there is no fallback to an unverified directory.

## Bundle byte verification

Before installation, `verify_bundle_files` resolves only the trusted bundle root and
each manifest-declared relative path. It rejects absolute paths, traversal, backslash
forms, missing files, symlinked components, size mismatches, and SHA-256 mismatches.
It reads at most the hard per-entry ceiling and never imports, decodes, executes, or
publishes the bytes. The future provider must load only entries that were verified by
this manifest; extra files are not implicitly trusted.

## Release signing boundary

`backend/cortex_backend/execution/worker_release.py` and
`tools/sign_recipe_worker.py` form a release-only signing boundary. They enumerate
every ordinary file in the built one-folder package, reject reparse points,
hardlinks, empty/oversized files, source mutation, reserved `manifest.json`, and a
missing fixed worker, then sign the canonical manifest with an externally supplied
raw Ed25519 private key. The key is read only for the signing operation and is never
written to the repository, returned in metadata, or printed. The builder self-verifies
the resulting signature before returning, while installation still requires the
independently pinned public-key trust root and `SignedBundleInstaller`.

`packaging/build_recipe_worker.ps1` remains unsigned by default. Supplying all signing
parameters opts into the release tool; a package is not launch-authorized merely
because a manifest was generated. No private key, signed production artifact, or
trust-root update is committed by this ADR stage.

## Failure contract

Failures are reduced to stable categories: `manifest_invalid`, `manifest_too_large`,
`manifest_key_untrusted`, `manifest_signature_invalid`, `manifest_replay`,
`manifest_downgrade`, `manifest_rollback_not_authorized`, `bundle_*`, or
`bundle_root_unavailable`. No key material, path, signature, payload, or exception
detail is returned to a model or API response.

## Explicit non-goals

This manifest verifier does not provide remote update transport, SBOM validation,
Authenticode verification, image decoding, production broker IPC, OS sandboxing, or
runtime/provider enablement. Key rotation, persistent state, atomic installation,
and explicit recovery are implemented separately in
[the bundle installation ADR](0001-phase2-bundle-installation.md). The application
lifecycle remains disabled for production execution.

## Verification

`tests/test_phase2_manifest.py` covers valid signatures, tampering, unknown/revoked
keys, malformed payloads, monotonic updates, replay/downgrade rejection, explicitly
authorized rollback, bundle size/hash verification, path/root failures, and invalid
persisted state. The complete repository and frontend matrix remains required before
merge.
