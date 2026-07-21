# ADR-0001 Phase 2 evidence log

- **Phase:** 2 — signed image recipes and calculator/check primitives
- **Status:** Typed contract, signed-manifest verification, native broker transport, signed bundle installation, trusted artifact boundary, and qualification-only provider core complete; OS sandbox/provider and release gates remain open
- **Scope:** Provider-independent contracts plus a qualification-only fixed-function core
- **Source decision:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Contract ADR:** [Phase 2 typed recipe and primitive contract](0001-phase2-recipe-contract.md)

## Stage checklist

| Deliverable | Status | Evidence |
| --- | --- | --- |
| Versioned image transform schema | **Complete (contract only)** | `artifact.transform.v1` allows only bounded grayscale, contrast, brightness, crop, resize, and rotate steps with PNG/JPEG/WebP output. |
| Opaque artifact binding | **Complete (validation only)** | Artifact IDs are bounded opaque identifiers; paths, source text, plugin names, and model-selected output names are rejected or absent. |
| Calculator/check primitives | **Complete (trusted pure helpers)** | Decimal-only calculator operations and explicit comparisons are deterministic, bounded, and have no I/O or code execution surface. |
| Canonical plan identity | **Complete** | Validated plans expose stable canonical JSON and SHA-256 digests for future idempotency/signature binding. |
| Signed recipe manifest | **Complete (verification only)** | Ed25519 signature verification uses a pinned key-id allowlist; every declared bundle entry is path-, size-, and SHA-256-verified; monotonic updates and explicit rollback authorization are enforced. |
| Signed bundle installation/update | **Complete (storage-only)** | Digest-named immutable generations, exclusive staging, atomic activation state, chained keyring rotation, explicit rollback authorization, and previous-generation recovery are covered by installer tests. No provider is loaded. |
| Authenticated broker contract | **Complete (transport-neutral)** | Bounded versioned frames, direction-specific HMAC keys, canonical messages, peer ACL/integrity policy, and owner-scoped authorization are covered by adversarial tests. |
| Native named-pipe adapter/DACL/peer-token binding | **Complete (transport-only)** | Protected local pipe, expected PID, OS token identity, X25519/HKDF handshake, direction keys, and close-on-error lifecycle are covered by native broker tests. |
| User-artifact copy-in, output validation, and publication | **Complete (boundary only)** | Explicit owner/turn grants, bounded stable snapshots, link/reparse/hardlink/sparse/ADS rejection, byte-derived MIME policy, exact output claims, quarantine, hash/size limits, atomic repository publication, rollback, and cleanup categories are covered by `tests/test_phase2_artifact_boundary.py`. |
| Fixed-function image provider core | **Complete (qualification-only)** | `RecipeImageProvider` validates allowlisted PNG/JPEG/WebP bytes, verifies/loads one frame with Pillow bomb/resource limits, applies only parsed steps, strips metadata, revalidates encoded output, checks cancellation, and remains disabled until external sandbox health passes. |
| OS sandbox provider and provider-produced image outputs | **Blocked / next gate** | The core has no process, AppContainer/LPAC, Job Object, broker, watchdog, or lifecycle route; hostile decoder qualification and external review remain required. |

## Security invariants

1. Unknown fields and operations fail closed; no best-effort expression or command
   interpretation occurs.
2. Image plans contain no filesystem path, arbitrary filename, network target, or
   dynamic filter/plugin identifier.
3. Calculator inputs are finite bounded decimals; floats, non-finite values, division
   by zero, and result overflow/precision exhaustion fail closed.
4. Comparison semantics are explicit; tolerance exists only for `is_close` and must be
   positive.
5. Validation and evaluation errors expose stable safe categories only.
6. Canonical digests identify accepted plans but grant no capability and verify no
   signature.
7. Signed manifests verify against a pinned Ed25519 key id and canonical payload;
   unknown/revoked keys, malformed signatures, replay, downgrade, and unauthorized
   rollback fail closed.
8. Every declared bundle entry is verified by safe relative path, exact byte size, and
   SHA-256 before any future installation decision; verification does not load it.
9. The Phase 1 application lifecycle remains explicitly disabled for production
   execution; this stage cannot make a provider visible by itself.
10. Frames are bounded, authenticated with direction-specific keys, canonical, and
    strictly sequenced; replay, reflection, truncation, and malformed headers fail
    closed.
11. Peer ACL/identity and durable job ownership are checked outside the wire payload;
    a principal or job mismatch cannot be used as a confused deputy.
12. Native transport uses a protected local-only DACL, rejects remote clients, requires
    expected process binding, and closes on identity or handshake failure; it never
    falls back to a default ACL, alternate transport, or provider.
13. Bundle installation copies only verified declared bytes into an exclusive staging
    tree, rejects reparse points/hardlinks and source mutation, and activates only a
    complete digest-named generation.
14. The activation pointer is atomically replaced only after the generation is
    verified; keyring updates are signature-chained and rollback/recovery always need
    a separate trusted local decision.
15. User copy-in requires an owner-bound source grant and never mutates or overwrites
    the selected source; source identity, size, and timestamps must be stable across
    the bounded read.
16. Reparse points, hardlinks, sparse files, devices, ADS/path ambiguity, active
    content, archives, and non-finite JSON are rejected before artifact publication.
17. Provider output claims must exactly match the private staging file set; all files
    are validated before any publication, and publication failure rolls back records
    while quarantine/cleanup failures surface for supervisor recovery.
18. Artifact records are opaque IDs; repository read/delete/purge operations remain
    confined to the configured artifact root and verify the stored SHA-256.
19. The fixed-function provider accepts only immutable bytes and parsed plans, uses an
    independent format allowlist, treats decoder warnings as errors, rejects multiple
    frames, enforces hard byte/pixel/dimension/memory/step caps, and revalidates output.
20. Provider startup requires an external available sandbox health result; dependency
    or codec failure, cancellation, decoder failure, and output metadata/size failure
    leave the provider disabled and return stable categories only.

## Re-run target

```powershell
python -m pytest tests/test_phase2_recipe_contract.py -q
python -m pytest tests/test_phase2_manifest.py -q
python -m pytest tests/test_phase2_broker.py -q
python -m pytest tests/test_phase2_native_broker.py -q
python -m pytest tests/test_phase2_bundle_installer.py -q
python -m pytest tests/test_phase2_artifact_boundary.py -q
python -m pytest tests/test_phase2_recipe_provider.py -q
python -m compileall -q backend\cortex_backend\execution tests
python -m pytest -q
python tools/generate_contracts.py
npm.cmd run lint --prefix frontend
npm.cmd run typecheck --prefix frontend
npm.cmd run build --prefix frontend
npm.cmd test --prefix frontend -- --run
```

**Validation result (2026-07-21):** 16 Phase 2 contract tests, 9 signed-manifest tests,
7 broker-contract tests, 9 native-broker tests, 7 bundle-installer tests, 16
artifact-boundary tests, and 17 recipe-provider tests passed; the full Python suite
passed (204 tests total) with one
native-platform skip and one pre-existing `pytest-asyncio` deprecation warning.
Frontend lint, typecheck, production build, and all 39 frontend tests passed. Contract
generation, compileall, and `git diff --check` passed. No production execution
provider is enabled.
