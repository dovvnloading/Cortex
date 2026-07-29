# ADR-0001 Phase 2 durable recipe coordinator and artifact publication

- **Status:** Implemented and verified as an internal qualification composition
  boundary; application/API exposure remains a separate default-off decision
- **Phase:** 2 - fixed-function image recipe
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Depends on:** [typed recipe contract](0001-phase2-recipe-contract.md),
  [trusted artifact boundary](0001-phase2-artifact-boundary.md),
  [worker protocol](0001-phase2-worker-protocol.md), and
  [qualification lifecycle](0001-phase2-qualification-lifecycle.md)
- **Scope:** Durable request/attempt coordination, cancellation and recovery,
  and all-or-nothing publication for the fixed image recipe

## Decision

The qualified image recipe is coordinated by
`cortex_backend.execution.recipe_coordinator.RecipeExecutionCoordinator`.
The coordinator is an internal composition seam, not a new application route.
It can be injected by the explicit `release_profile="qualification"`
lifecycle, while the normal application remains disabled by default.

The request contract is `RecipeImageRequest`. It contains an owner, an
idempotency request identifier, an opaque `source_artifact_id`, a validated
`ImageTransformPlan`, and a bounded retention period. The plan's input artifact
identifier must match the request identifier exactly. No source path, filename,
command, model name, shell text, network target, or executable authority is
accepted or written to durable job state.

The worker seam is `RecipeWorkerAttempt`. A factory receives the durable job and
returns one already-authenticated attempt. The attempt receives immutable input
bytes and a typed plan, and returns `RecipeWorkerOutput` with byte-derived MIME,
format, dimensions, and SHA-256. The concrete `RecipeWorkerClient` speaks only
the existing authenticated broker/worker protocol; it does not open a process,
choose a provider, or invent a fallback transport.

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
4. Cancellation sets a durable `cancelling` state, signals the worker attempt,
   and waits only within the worker's bounded cancellation path. A cancelled
   attempt cannot publish a successful result.
5. A valid output is written to a private temporary staging directory under the
   artifact root. `ArtifactBoundary.collect_outputs` requires the exact single
   `output` claim, re-sniffs bytes, enforces size/link/reparse/hash limits, and
   publishes atomically. Any publication failure rolls back records and
   quarantines or reports cleanup failure through a stable category.
6. The terminal result contains only the published artifact ID, safe MIME/format,
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

The worker factory is deliberately injected. This stage does not pretend that an
unsigned package, missing broker identity binding, or absent production trust root
is a usable runtime. Those are release/qualification composition inputs owned by
the existing lifecycle and launcher gates.

## Explicit non-goals

This ADR does not add a public API route, automatic model tool selection, arbitrary
Python/WASI execution, source-path access, direct user-file mutation, network
access, application-exit persistence, production signing, or external-review
requirements. It also does not make the qualification profile the application
default.

## Verification

`tests/test_phase2_recipe_coordinator.py` covers opaque request binding, output
digest/MIME revalidation, owner-scoped publication, idempotency conflicts,
redacted worker failure, cancellation cleanup, and a real authenticated worker
runtime round trip including in-flight cancellation. The repository-wide matrix
passed with **324 passed, 1 skipped** on 2026-07-29.

## Next stage

The next implementation decision is application integration: define the explicit
UI/API request surface and wire a qualified worker-attempt factory into the
qualification lifecycle. That stage must preserve this coordinator contract and
keep the normal application default-off. No external reviewer or production key
is required to continue the open-source qualification path.
