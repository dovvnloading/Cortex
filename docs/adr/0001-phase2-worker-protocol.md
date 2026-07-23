# ADR-0001 Phase 2 fixed recipe worker protocol and package boundary

- **Status:** Protocol, authenticated worker loop, and packaging qualification complete; signed end-to-end execution remains blocked
- **Phase:** 2 - fixed-function image provider
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Depends on:** [Signed worker provenance](0001-phase2-worker-provenance.md), [native broker adapter](0001-phase2-native-broker.md), and [native launcher](0001-phase2-native-launcher.md)
- **Scope:** Bounded worker messages, in-order byte streaming, fixed provider session state, and reproducible Windows package closure

## Decision

The worker protocol is transport-neutral and runs only inside an authenticated
native broker session. The broker remains responsible for framing, HMAC direction
keys, peer PID/token identity, installation principal, job ownership, and lifecycle
shutdown. The worker protocol validates the operation body after those checks.

One request follows this sequence:

1. `recipe.worker.prepare.v1` declares one parsed `ImageTransformPlan`, an opaque
   artifact identifier, expected byte count, expected SHA-256, and an allowlisted
   image MIME type.
2. One or more `recipe.worker.input_chunk.v1` messages append canonical base64
   chunks in strict offset order. Each chunk has its own SHA-256 and is at most
   48 KiB decoded.
3. `recipe.worker.input_complete.v1` repeats the whole-input size and digest. The
   worker decodes only after both claims match the received bytes.
4. The fixed provider runs with the worker's cancellation callback and returns
   only redacted metadata in `recipe.worker.result.v1`.
5. `recipe.worker.collect.v1` reads bounded output chunks. Each output chunk has a
   digest and final marker; the host artifact boundary remains responsible for
   quarantine, validation, hashing, and publication.

Cancellation is terminal for the request. Unknown operations, malformed bodies,
replayed offsets, identity mismatches, size/digest mismatches, provider failures,
and output offsets fail closed with stable categories. No worker message accepts a
filesystem path, shell command, executable, network target, token, or model source.

The package entrypoint now accepts exactly the native launcher's fixed argument
shape: protected pipe name, expected broker PID, installation principal, and job
ID. It creates only a `NativeBrokerClient`, authenticates the broker session, and
serves the bounded worker loop. Direct launches, malformed arguments, missing
broker identity, provider startup failure, transport failure, and message-budget
exhaustion return the safe refusal status (`78`); there is no stdio, shell, path,
or host-process fallback. The PyInstaller definition and Windows build script
qualify dependency closure and the fixed `recipe_worker.exe` path only; they do
not sign, install, or authorize the worker.

## Security invariants

- The worker never opens a path or creates a transport.
- Input and output bytes are bounded by the provider ceilings; chunks are bounded
  below the broker frame ceiling.
- Input chunks are canonical, contiguous, independently hashed, and committed only
  after whole-stream size and digest verification.
- The session state machine permits one request at a time and never resumes after
  cancellation or terminal failure.
- Output metadata is private and content-addressed; publication remains outside the
  worker session.
- A package build is not a signed bundle. The release pipeline must create the
  exact `recipe.manifest.v1` entry for `image_transform`/`recipe_worker.exe`, sign
  it with the pinned key, install it through `SignedBundleInstaller`, and re-run
  `verify_active_worker()`.

## Evidence

`tests/test_phase2_worker_protocol.py` covers successful in-order streaming and
collection, malformed operations, replay/order failure, claim mismatch,
cancellation terminality, and chunk tampering. `tests/test_phase2_worker_runtime.py`
(9 tests) covers authenticated envelope binding, redacted repairable failures,
provider failure, message budgets, terminal cleanup, watchdog expiry,
fixed-entrypoint parsing, and cancellation delivered while a transform is running.
On the controlled Windows host (2026-07-23), the
PyInstaller build produced `dist/recipe-runtime/recipe_worker.exe`; direct launch
without the exact native broker arguments returned exit code `78` as required.

## Remaining blockers

This ADR does not authorize provider execution. The remaining stage must install a
real signed generation, launch it through the reviewed suspended AppContainer/Job
Object factory, bind the live broker session to the actual worker PID and
AppContainer token, and run the hostile decoder/cancellation corpus through that
packaged process with watchdog and artifact-boundary evidence. Lifecycle/UI
enablement remains behind those gates and external security review.
