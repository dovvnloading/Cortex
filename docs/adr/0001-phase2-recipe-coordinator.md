# ADR-0001 Phase 2 durable recipe coordinator and artifact publication

- **Status:** Implemented and verified behind an explicit qualification-only
  API boundary, including trusted attachment staging and signed/native attempt
  composition and a durable packaged-worker qualification corpus; the normal
  application remains default-off
- **Phase:** 2 - fixed-function image recipe
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Depends on:** [typed recipe contract](0001-phase2-recipe-contract.md),
  [trusted artifact boundary](0001-phase2-artifact-boundary.md),
  [worker protocol](0001-phase2-worker-protocol.md), and
  [qualification lifecycle](0001-phase2-qualification-lifecycle.md)
- **Scope:** Durable attachment staging, signed/native request-attempt
  composition, cancellation and recovery, and all-or-nothing publication for
  the fixed image recipe

## Decision

The qualified image recipe is coordinated by
`cortex_backend.execution.recipe_coordinator.RecipeExecutionCoordinator`.
The coordinator is exposed through the explicit
`POST /api/v1/execution/recipe/image` route only when the app was built with a
ready `release_profile="qualification"` lifecycle. The normal application
remains disabled by default, and the deterministic `fake.v1` preview route
cannot reach the recipe coordinator.

The request contract is `RecipeImageRequest`. It contains an owner, an
idempotency request identifier, an opaque `source_artifact_id`, a validated
`ImageTransformPlan`, and a bounded retention period. The plan's input artifact
identifier must match the request identifier exactly. No source path, filename,
command, model name, shell text, network target, or executable authority is
accepted or written to durable job state.

`POST /api/v1/execution/attachments` is the qualification-only input boundary.
It accepts a bounded base64 envelope, decodes it in memory, and passes the bytes
through `ArtifactBoundary.stage_bytes`; the durable `attachment.stage.v1` record
stores only the content digest, size, derived MIME, and bounded retention. Its
response contains an opaque artifact ID and safe metadata, never a path or the
encoded payload.

The worker seam is `RecipeWorkerAttempt`. A factory receives the durable job and
returns one fresh, already-authenticated attempt. The native implementation
`NativeRecipeWorkerAttemptFactory` creates a new `NativeBrokerIdentityBinder`,
`NativeWorkerLauncher`, protected pipe binding, suspended AppContainer worker,
and `RecipeWorkerClient` per job. It requires an injected reviewed process
factory and signed installer, binds the broker PID/principal/job identity before
resume, waits only within a bounded accept window, and closes the client,
broker, and process tree on every failure or cancellation. It has no host
process or alternate transport fallback. The attempt receives immutable input
bytes and a typed plan, and returns `RecipeWorkerOutput` with byte-derived MIME,
format, dimensions, and SHA-256. The concrete `RecipeWorkerClient` speaks only
the existing authenticated broker/worker protocol; it does not choose a
provider or invent a fallback transport.

## Durable lifecycle

1. The coordinator owner-checks and reads the staged source artifact through the
   repository. The artifact's stored size, digest, and byte-derived MIME are
   rechecked before a worker is created.
2. A queued `recipe.image.v1` job stores only the versioned recipe payload,
   opaque artifact ID, canonical plan, plan digest, provider version, and bounded
   retention. SQLite uniqueness on `(owner, request_id)` makes retries
   idempotent; a different payload with the same key is a stable
   `request_conflict`.
3. A per-job lease and background thread own the attempt. Startup recovery claims
   the supervisor lease, recovers expired job leases, validates the persisted
   payload, and resumes only the known recipe profile. Invalid recovery metadata
   becomes `recovery_invalid_payload`; it is never interpreted as executable
   input.
4. Attachment staging is idempotent on `(owner, request_id)`. A duplicate
   matching payload revalidates the stored artifact bytes and returns the same
   opaque artifact; a different payload is a stable `request_conflict`.
5. Cancellation sets a durable `cancelling` state, signals the worker attempt,
   and waits only within the worker's bounded cancellation path. A cancelled
   attempt cannot publish a successful result.
6. A valid output is written to a private temporary staging directory under the
   artifact root. `ArtifactBoundary.collect_outputs` requires the exact single
   `output` claim, re-sniffs bytes, enforces size/link/reparse/hash limits, and
   publishes atomically. Any publication failure rolls back records and
   quarantines or reports cleanup failure through a stable category.
7. The terminal result contains only the published artifact ID, safe MIME/format,
   size, digest, dimensions, and plan digest. It never returns the staging or
   repository path. A cancellation race after publication deletes the unpublished
   records before recording `cancelled`.

## Worker protocol client

`RecipeWorkerClient` uses a reader thread around the existing blocking
authenticated connection so cancellation can be sent while the provider is
transforming. It sends `prepare`, bounded independently hashed input chunks,
`input_complete`, and `collect` messages. Every response is checked for direction,
principal, request/job identity, operation, schema, chunk offset, chunk digest,
output size, output digest, MIME, and format. A worker error is reduced to its
stable code; transport, timeout, malformed-message, and cancellation categories
are distinct. A late result after cancellation is never accepted.

## Failure and recovery policy

The coordinator exposes stable categories such as `input_artifact_unavailable`,
`worker_transport_failed`, `worker_timeout`, `worker_output_invalid`,
`artifact_publication_failed`, `artifact_cleanup_pending`, `cancelled`, and
`coordinator_failed`. Raw paths, decoder errors, broker payloads, process IDs,
tokens, and stack traces do not enter job events or results. Terminal repository
state is immutable, so late worker callbacks cannot overwrite a validated result.

The worker factory is deliberately injected. The qualification helper
`build_recipe_coordinator_factory` remains the generic seam, while
`build_native_recipe_coordinator_factory` composes the signed/native attempt
factory when a caller supplies the installer, allowed user SIDs, and reviewed
process-factory factory. Configuration creates no process. This stage does not
pretend that an unsigned package, missing broker identity binding, or absent
production trust root is a usable runtime; those remain release/qualification
composition inputs owned by the existing lifecycle and launcher gates.

## Explicit non-goals

This ADR does not add automatic model tool selection, arbitrary Python/WASI
execution, source-path access, direct user-file mutation, network access,
application-exit persistence, production signing, or external-review
requirements. It also does not make the qualification profile the application
default or claim that the normal app discovers a worker package implicitly.

## Verification

`tests/test_phase2_recipe_coordinator.py` covers opaque request binding, output
digest/MIME revalidation, owner-scoped publication, idempotency conflicts,
redacted worker failure, cancellation cleanup, and a real authenticated worker
runtime round trip including in-flight cancellation. `tests/test_phase2_recipe_api.py`
covers default-off/blocked exposure, typed JSON plan parsing, owner-scoped
artifacts, idempotency/conflict handling, and the shared status/task surface.
`tests/test_phase2_qualification_lifecycle.py` covers the explicit coordinator
factory seam. `tests/test_phase2_attachment_staging.py` covers byte limits,
active/archive rejection, owner/idempotency binding, result revalidation, and
retention. `tests/test_phase2_native_recipe_attempt.py` covers fresh per-job
identity/resource scopes, principal validation, explicit process-factory
composition, and bounded cleanup. The generated OpenAPI and TypeScript
contracts include both typed envelopes, and `frontend/src/api/client.ts`
exposes the qualification routes without enabling them in the UI by default.

The qualification-only packaged-worker gate is
`tools/execution_spikes/recipe_coordinator_e2e_qualification.py`. It signs the
already-built one-folder worker with an in-memory ephemeral key, installs one
disposable immutable generation, stages a PNG through `AttachmentStagingService`,
and drives the real `RecipeExecutionCoordinator` through the native
AppContainer/broker/client attempt. Its strict corpus verifies successful
publication and digest/MIME/size binding, foreign-owner rejection, retention
expiry and purge, in-flight cancellation with no result artifact, absence of
temporary publication directories, and complete native process cleanup. The
probe is Windows-only, bounded, redacted, and has no host-process fallback.

## Next stage

The coordinator implementation and hosted Quality CI gates are complete, and PR
#64 is merged. The next product stage is a normal user-facing recipe flow; this
API/coordinator contract remains qualification-only until that flow is reviewed.
Any follow-on scratch-compute profile needs focused capability and safety tests,
but it does not need an external reviewer or production key for the open-source
qualification path. Production trust remains optional release hardening.
