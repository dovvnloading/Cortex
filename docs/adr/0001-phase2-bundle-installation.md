# ADR-0001 Phase 2 signed bundle installation and recovery

- **Status:** Implemented and verified; provider, codec, and execution enablement remain blocked
- **Parent:** [Phase 2 signed recipe manifest and rollback gate](0001-phase2-signed-manifest.md)
- **Scope:** Verified bundle staging, atomic activation, durable keyring rotation, and explicit recovery

## Decision

`SignedBundleInstaller` is the only storage boundary allowed to turn a verified
recipe bundle into an installed generation. It is deliberately not a provider: it
never imports, decodes, executes, loads, or exposes a bundle to a model. The caller
provides a source directory and a signed `recipe.manifest.v1` payload; the installer
returns only an immutable, verified generation record.

The durable layout is private to the application data directory:

```text
recipe_bundles/
  state.json
  .install.lock
  bundles/
    bundle-<manifest-sha256>/
      manifest.json
      <only manifest-declared files>
```

The generation name is the full manifest digest. Source files are checked by the
existing manifest verifier and then copied into an exclusive staging directory. The
copy rejects symlinks/junctions, non-regular files, hardlinks, path escapes, size or
digest changes, and source identity changes during the copy. Only declared files are
copied; `manifest.json` is reserved for the canonical signed payload. Staged files
are flushed before the generation is renamed into the same-volume bundle directory.

One-folder worker dependencies are represented by manifest entries with the inert
`resource` entrypoint; the installer copies them for exact-tree integrity but never
selects them as executable roles. Worker-role selection remains the separate
`verify_active_worker()` provenance check.

Activation is a two-step commit. First, the complete generation is durably written
and renamed without replacing an existing digest directory. Second, the small
`state.json` pointer is written to a unique temporary file, flushed, and atomically
replaced. The pointer records the current generation, the previous verified
generation, and the keyring update chain. State is never advanced before the
generation exists and passes a full signature, exact-tree, size, and hash check.
An interrupted copy or state replacement therefore leaves the old pointer active or
leaves execution unavailable; an unverified directory is never selected.

The state transition is serialized by an application-owned lock file for both
threads and installer processes. Startup validates the keyring chain and the current
generation before reporting it active. Orphan staging and unreferenced generations
are removed only by the explicit installer-owned cleanup operation; cleanup failures
become a stable pending category rather than deleting arbitrary paths.

## Key rotation

Key rotation uses a separate strict `recipe.keyring.update.v1` payload. It contains a
monotonic sequence, the exact previous keyring digest, a signer key id, a bounded map
of raw Ed25519 public keys, revoked ids, and a signature over canonical JSON without
the signature field. The signer must be an active key in the current keyring. Every
update is replayed and verified from the packaged bootstrap keyring on startup; a
missing link, changed root digest, bad signature, unknown signer, sequence replay,
or empty active key set fails closed. The active bundle's signing key cannot be
revoked by a standalone update, preventing a rotation from bricking the installed
generation. New-key bundles are installed only after the signed rotation has been
atomically persisted.

## Rollback and recovery

Normal updates must pass the manifest sequence and non-decreasing semantic version
rules. A downgrade requires both the manifest's exact `rollback_of` digest and a
trusted local `RollbackAuthorizer(current_digest, candidate_digest)` decision. The
authorization is evaluated after signature verification and before any staging.

The previous generation is retained in durable state. If the current generation is
missing, tampered, or fails startup verification, the installer reports
`bundle_install_recovery_failed`; it does not silently fall back. An operator or
trusted lifecycle controller must call `recover_previous` with a separate rollback
authorization. Recovery verifies the retained generation again and atomically points
state to it, clearing the invalid current pointer.

## Failure contract

Stable categories include `bundle_root_*`, `bundle_path_invalid`,
`bundle_hardlink_rejected`, `bundle_source_changed`, `bundle_size_mismatch`,
`bundle_install_*`, `bundle_cleanup_*`, `bundle_recovery_*`, `keyring_*`, and the
existing manifest verification categories. No path, key material, signature, source
payload, or OS exception detail is returned across the model/API boundary.

## Explicit non-goals

This ADR does not provide remote update transport, SBOM or Authenticode validation,
image decoding, copy-in from user-owned conversation artifacts, output validation or
publication, a sandbox/provider, process launch, broker dispatch, or lifecycle
enablement. Those remain later gates.

## Verification

`tests/test_phase2_bundle_installer.py` covers restart-stable installation, staged
copy and exact generation contents, state-commit failure preservation, explicit
rollback and previous-generation recovery, signed chained key rotation and replay,
active-key revocation protection, hardlink rejection, and keyring-state tampering.
The atomic replacement model follows the Windows guidance for [ReplaceFile](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilea)
and same-volume [MoveFileEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexa)
operations; the provider remains disabled until its own sandbox and lifecycle gates
pass.
