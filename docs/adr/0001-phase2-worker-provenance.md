# ADR-0001 Phase 2 signed worker provenance binding

- **Status:** Storage-only worker-role verifier complete; native launch remains blocked
- **Phase:** 2 - fixed-function image provider
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Depends on:** [signed bundle installation](0001-phase2-bundle-installation.md), [signed recipe manifest](0001-phase2-signed-manifest.md), and [Windows sandbox qualification](0001-phase2-sandbox-qualification.md)
- **Scope:** Bind an installed signed generation to the fixed image-transform worker role without executing it.

## Decision

`backend/cortex_backend/execution/worker_provenance.py` adds a storage-only
verification boundary:

1. `verify_active_worker()` first asks `SignedBundleInstaller.status()` to
   revalidate the active signed generation and exact immutable tree;
2. the manifest is read with a bounded ASCII read and its digest must match the
   installer state;
3. every declared bundle byte is revalidated through `verify_bundle_files()`;
4. exactly one `image_transform` entry must exist and its path must be the fixed
   `recipe_worker.exe` name;
5. the worker path must be an ordinary single-link file inside the generation,
   and its size, timestamps, identity, and SHA-256 must remain stable across the
   bounded read; and
6. only immutable metadata is returned. No executable handle, import, decode,
   process creation, or provider instance is returned or performed.

The verifier is deliberately a second role-binding boundary after signature and
bundle installation. A valid signed bundle containing a different image entry,
multiple image roles, a changed worker, or a reparse/hardlink entry cannot be
treated as the worker. Stable `WorkerProvenanceError.code` values cross the
boundary; paths, bytes, OS errors, and signatures never do.

The release signer may declare the remaining one-folder files as inert `resource`
entries so the installer can enforce an exact dependency tree. Those entries are
copied and rehashed but are never eligible for worker-role selection.

## Failure categories

The verifier uses bounded categories including `worker_bundle_unavailable`,
`worker_bundle_integrity_failed`, `worker_manifest_invalid`,
`worker_manifest_mismatch`, `worker_role_missing`, `worker_role_ambiguous`,
`worker_entrypoint_mismatch`, `worker_entrypoint_invalid`,
`worker_entrypoint_changed`, `worker_entrypoint_hash_mismatch`, and
`worker_entrypoint_reparse`/`worker_manifest_reparse` where applicable.

## Explicitly not implemented here

This boundary does not launch the worker or claim any OS isolation. The following
remain required before the provider can be enabled:

- a signed packaged executable at the fixed role path;
- a native suspended launcher with private staging and exact ACLs;
- AppContainer/LPAC policy, Job Object CPU/memory/breakaway limits, accounting,
  watchdog, and full-tree cancellation;
- protected broker PID/token binding and bounded framed IPC;
- hostile decoder execution inside that worker; and
- external review and `ExecutionLifecycle` health-gated wiring.

Any missing control must leave the provider unavailable. The host process must
never decode or execute as a fallback.

## Verification

`tests/test_phase2_worker_provenance.py` proves a valid signed fixture binds to
the exact worker role and that no active generation, wrong path, ambiguous role,
or post-install tamper is accepted. Existing bundle-installer tests continue to
cover signature, byte, staging, hardlink, rollback, and recovery boundaries.

This stage is therefore **complete as a storage-only provenance substage**, not a
provider release approval.
