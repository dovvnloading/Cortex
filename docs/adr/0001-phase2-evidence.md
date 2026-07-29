# ADR-0001 Phase 2 evidence log

- **Phase:** 2 — signed image recipes and calculator/check primitives
- **Status:** Typed contract, signed-manifest verification, native broker transport, authenticated worker loop, signed bundle installation, trusted artifact boundary, signed worker launch/broker qualification, and packaged transform/hostile-decoder/cancellation qualification complete; parser fuzzing, resource/watchdog accounting, artifact security review, external review, and lifecycle release gates remain open
- **Scope:** Provider-independent contracts plus a qualification-only fixed-function core
- **Source decision:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Contract ADR:** [Phase 2 typed recipe and primitive contract](0001-phase2-recipe-contract.md)
- **Worker ADR:** [Phase 2 fixed recipe worker protocol and package boundary](0001-phase2-worker-protocol.md)

## Stage checklist

| Deliverable | Status | Evidence |
| --- | --- | --- |
| Versioned image transform schema | **Complete (contract only)** | `artifact.transform.v1` allows only bounded grayscale, contrast, brightness, crop, resize, and rotate steps with PNG/JPEG/WebP output. |
| Opaque artifact binding | **Complete (validation only)** | Artifact IDs are bounded opaque identifiers; paths, source text, plugin names, and model-selected output names are rejected or absent. |
| Calculator/check primitives | **Complete (trusted pure helpers)** | Decimal-only calculator operations and explicit comparisons are deterministic, bounded, and have no I/O or code execution surface. |
| Canonical plan identity | **Complete** | Validated plans expose stable canonical JSON and SHA-256 digests for future idempotency/signature binding. |
| Signed recipe manifest | **Complete (verification only)** | Ed25519 signature verification uses a pinned key-id allowlist; every declared bundle entry is path-, size-, and SHA-256-verified; monotonic updates and explicit rollback authorization are enforced. |
| Signed bundle installation/update | **Complete (storage-only)** | Digest-named immutable generations, exclusive staging, atomic activation state, chained keyring rotation, explicit rollback authorization, and previous-generation recovery are covered by installer tests. No provider is loaded. |
| Signed worker release generation | **Complete (release-only)** | `worker_release.py` and `tools/sign_recipe_worker.py` hash every one-folder file, mark only `recipe_worker.exe` as `image_transform`, classify dependencies as inert `resource` entries, self-verify the Ed25519 signature, reject ambiguous/mutable packages, and never persist private key material. Installation still requires the pinned public trust root. |
| Signed worker provenance binding | **Complete (storage-only)** | `verify_active_worker()` rechecks the active signed generation, binds exactly one `image_transform` role to `recipe_worker.exe`, revalidates byte identity, and rejects missing/ambiguous/mismatched/tampered/reparse entries without launching. |
| Fixed worker protocol and package closure | **Complete (qualification-only)** | `worker_protocol.py` and `worker_runtime.py` enforce bounded prepare/chunk/complete/cancel/collect state, authenticated envelope identity, concurrent cancellation, redacted output/errors, and no-capability bodies. `packaging/recipe_worker/recipe_worker.spec` builds the fixed `recipe_worker.exe` (Windows build verified 2026-07-23); the entrypoint accepts only the fixed native-broker identity arguments and returns `78` on direct or failed launches. |
| Authenticated broker contract | **Complete (transport-neutral)** | Bounded versioned frames, direction-specific HMAC keys, canonical messages, peer ACL/integrity policy, and owner-scoped authorization are covered by adversarial tests. |
| Native named-pipe adapter/DACL/peer-token binding | **Complete (transport-only)** | Protected local pipe, expected PID, OS token identity, X25519/HKDF handshake, direction keys, and close-on-error lifecycle are covered by native broker tests. |
| User-artifact copy-in, output validation, and publication | **Complete (boundary only)** | Explicit owner/turn grants, bounded stable snapshots, link/reparse/hardlink/sparse/ADS rejection, byte-derived MIME policy, exact output claims, quarantine, hash/size limits, atomic repository publication, rollback, and cleanup categories are covered by `tests/test_phase2_artifact_boundary.py`. |
| Fixed-function image provider core | **Complete (qualification-only)** | `RecipeImageProvider` validates allowlisted PNG/JPEG/WebP bytes, verifies/loads one frame with Pillow bomb/resource limits, applies only parsed steps, strips metadata, revalidates encoded output, checks cancellation, and remains disabled until external sandbox health passes. |
| Windows recipe sandbox qualification harness | **Complete (signed launch/broker/hostile/cancellation)** | `recipe_worker_e2e_qualification.py` signs a disposable package with an in-memory key, installs/verifies one immutable generation, binds the live AppContainer identity to the broker, and exercises the fixed PNG transform, truncated-PNG decoder rejection, active-SVG decoder rejection, and in-flight cancellation corpus. The native broker uses bounded availability polling before reads so the cancellation reader remains live while the packaged provider transforms. |
| Suspended native launcher/resource policy | **Complete (factory + binder + ACL cleanup + qualification evidence)** | `NativeWin32ProcessFactory` grants only inherited read/execute access to the fresh AppContainer SID on the verified package root, applies and verifies Job Object policy before resume, and removes the per-launch ACE during cleanup. `NativeBrokerIdentityBinder` pins the live server to the worker PID/AppContainer SID and launcher cleanup closes it on failure. |
| OS sandbox provider and provider-produced image outputs | **Blocked / release gate** | Signed installation, provenance, AppContainer/job identity, broker handshake, `prepare`, `input_chunk`, `input_complete`, `collect_output`, hostile decoder rejection, and in-flight cancellation acknowledgement are live-qualified inside the packaged worker. The remaining gate is parser fuzzing, artifact security review, resource/watchdog accounting, external review, and production lifecycle wiring. |

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
21. The sandbox qualification harness never authorizes a provider launch from a
    missing, unsigned, or merely present worker directory; it reports `blocked` and
    never falls back to host-process decoding.
22. Worker provenance is storage-only: only an installer-validated immutable
    generation with one exact `image_transform`/`recipe_worker.exe` role and stable
    byte identity can proceed to a future launcher; no executable is loaded here.
23. The disposable launcher applies all required Job Object policy before resume,
    queries the configured limits/accounting, never grants breakaway, and reports
    the absent worker/broker gates as blocking.
24. Release signing reads an external raw private key only for the signing operation,
    self-verifies the canonical manifest, rejects reparse/hardlink/mutable package
    inputs, and never treats a generated manifest as launch authorization.
25. The native launcher grants only a per-profile inherited read/execute ACE on the
    verified package root, removes that ACE during worker cleanup, and never grants
    package write/delete access; a qualification timeout is always a blocked result.
26. Native broker reads poll bounded pipe availability before synchronous reads so a
    cancellation reader cannot starve the provider transform; pipe errors still fail
    closed and do not bypass framing, authentication, or sequence checks.
27. Hostile decoder bytes are rejected inside the signed worker, and cancellation is
    sent only after `input_complete` over the authenticated broker; a missing or
    ambiguous terminal response remains a blocked qualification result.

## Re-run target

```powershell
python -m pytest tests/test_phase2_recipe_contract.py -q
python -m pytest tests/test_phase2_manifest.py -q
python -m pytest tests/test_phase2_broker.py -q
python -m pytest tests/test_phase2_native_broker.py -q
python -m pytest tests/test_phase2_bundle_installer.py -q
python -m pytest tests/test_phase2_artifact_boundary.py -q
python -m pytest tests/test_phase2_recipe_provider.py -q
python -m pytest tests/test_phase2_worker_provenance.py -q
python -m pytest tests/test_phase2_worker_release.py -q
python -m pytest tests/test_native_launcher_qualification.py -q
python -m pytest tests/test_recipe_sandbox_qualification.py -q
python tools/execution_spikes/native_launcher_qualification.py
python tools/execution_spikes/recipe_sandbox_qualification.py --json --strict
python tools/execution_spikes/recipe_worker_e2e_qualification.py --json --strict
python -m compileall -q backend\cortex_backend\execution tests
python -m pytest -q
python tools/generate_contracts.py
npm.cmd run lint --prefix frontend
npm.cmd run typecheck --prefix frontend
npm.cmd run build --prefix frontend
npm.cmd test --prefix frontend -- --run
```

**Validation result (2026-07-23):** 16 Phase 2 contract tests, 9 signed-manifest tests,
7 broker-contract tests, 9 native-broker tests, 7 bundle-installer tests, 16
artifact-boundary tests, 17 recipe-provider tests, 6 worker-provenance tests, 7
worker-protocol tests, 7 worker-release tests, 16 native-launcher/factory tests,
4 native-launcher tests, and 5 sandbox-qualification tests passed; 9 worker-runtime
tests passed; the full Python suite passed (258 tests total) with one
native-platform skip and one pre-existing `pytest-asyncio` deprecation warning.
Frontend lint, typecheck, production build, and all 39 frontend tests passed. Contract
generation, compileall, and `git diff --check` passed. No production execution
provider is enabled. The sandbox qualification command passed its AppContainer,
Job Object, cancellation, and fixed decoder checks but returned the expected
fail-closed `blocked` status because the signed worker bundle is not shipped. The
Windows PyInstaller package built successfully, and an external-key smoke signed and
verified its complete 822-file closure (one `image_transform` role plus 821 inert
`resource` entries); no key or signed artifact was retained.

**Signed worker qualification result (2026-07-24):** The disposable
`recipe_worker_e2e_qualification.py --json --timeout-seconds 5 --strict` run passed
ephemeral signing/installation, active provenance verification, AppContainer/job
policy and identity binding, authenticated broker handshake, `prepare`,
`input_chunk`, `input_complete`, and `collect_output`. The transport fix polls
`PeekNamedPipe` with a 5 ms bounded wait before synchronous reads, preventing the
worker's cancellation reader from starving the provider transform. Bounded cleanup
closed the broker, binder, worker, and profile; no `recipe_worker.exe` process
remained. This closes the packaged provider-transform qualification gate but does
not close the remaining watchdog/hostile-decoder, external-review, or lifecycle
release gates.

**Packaged hostile/cancellation qualification result (2026-07-28):** The full
`recipe_worker_e2e_qualification.py --json --timeout-seconds 10 --strict` run
passed all four signed-worker cases: normal fixed PNG transform with
`collect_output`, truncated PNG rejection, active SVG rejection, and an
in-flight eight-step bounded transform that returned `cancel_ack` after
`input_complete`. Each case used a fresh authenticated broker binding and
disposable worker; the run reported no worker process or binding cleanup
residue. The optional `--case cancellation` path reproduces the cancellation
gate in isolation. These results close the packaged hostile-decoder and
cancellation qualification evidence, but do not enable the provider or close
parser fuzzing, resource/watchdog accounting, artifact security review,
external review, or production lifecycle health gates.
