# ADR-0001 Phase 2 packaged worker release qualification

- **Status:** CI qualification complete; production-signed packaging is optional official-release hardening
- **Phase:** 2 - fixed-function image provider
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Depends on:** [signed worker provenance](0001-phase2-worker-provenance.md), [native launcher](0001-phase2-native-launcher.md), [native broker](0001-phase2-native-broker.md), and [release/lifecycle preflight](0001-phase2-release-lifecycle-gate.md)
- **Scope:** Automated Windows qualification of the packaged worker boundary; no production trust material

## Decision

The Quality workflow now builds the fixed one-folder worker with
`packaging/build_recipe_worker.ps1 -SkipDependencyInstall` and runs
`tools/execution_spikes/recipe_worker_e2e_qualification.py --json --strict`.
The qualification harness creates an in-memory ephemeral Ed25519 key, signs the
fresh package, installs one immutable generation, re-verifies every declared byte,
launches through the suspended AppContainer/Job Object factory, binds the live
broker to the worker PID and AppContainer token, and runs the fixed transform,
hostile decoder, and in-flight cancellation corpus.

The CI job never receives a production private key, production trust root, user
file, model text, command, or network target. The generated package remains an
unsigned qualification artifact; the ephemeral signature is discarded with the
temporary store. A green run therefore proves the packaged boundary on the
runner, not production release authorization.

Quality CI follows this worker-level corpus with
`tools/execution_spikes/recipe_coordinator_e2e_qualification.py`. That separate
probe composes the same freshly installed generation through the durable
coordinator and verifies owner-scoped attachment staging, publication,
retention, cancellation, and native cleanup; it does not add a second provider
or trust root.

## Bounded execution

The package build has a 15-minute step timeout and the end-to-end qualification
has a 20-minute step timeout. The installer and provenance verifier intentionally
rehash the complete one-folder dependency closure at each trust boundary; this
may take several minutes on Windows. A slow or blocked run fails the Quality job
instead of silently passing or waiting without a release bound. The worker
protocol itself retains its per-response timeout and fail-closed cleanup.

## Required corpus

The strict run must pass all selected cases:

1. a fixed one-pixel-scale PNG transform through `prepare`, chunked input,
   `input_complete`, and `collect`;
2. truncated PNG bytes rejected inside the worker;
3. active SVG bytes rejected inside the worker; and
4. an eight-step in-flight transform that acknowledges cancellation and leaves no
   worker, broker, AppContainer profile, or staging residue.

Any missing package, provenance mismatch, broker identity failure, decoder
acceptance, cancellation timeout, cleanup failure, or qualification exception
returns a non-zero strict result. There is no host-process fallback.

## Release interpretation

This stage closes automated packaged-worker qualification evidence. It does not
create or install the signed production generation, establish the pinned release
trust root, provide the external security review, or enable the provider through
`ExecutionLifecycle`. Those remain explicit release gates in the parent ADR and
the release/lifecycle preflight.

## Verification

The local Windows package build completed successfully before this change was
published. The first green GitHub Quality run is the authoritative reproducible
CI evidence for the added workflow steps; its run URL and job result are recorded
in the Phase 2 evidence log without retaining the ephemeral key or package.
