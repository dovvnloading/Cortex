# ADR-0001 Phase 2 typed recipe and primitive contract

- **Status:** Contract and signed-manifest verification implemented and verified; provider enablement remains blocked
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Depends on:** [Phase 1 production lifecycle gate](0001-phase1-production-lifecycle.md)
- **Scope:** Typed fixed-function image plans, calculator/check primitives, canonical
  plan identity, and safe validation errors

## Decision

Phase 2 begins with a provider-independent contract layer. Model or UI proposals are
accepted only as bounded, versioned JSON plans; they are never interpreted as source,
commands, expressions, paths, or plugin names. The contract layer is deliberately
usable without a runtime provider so malformed proposals can be rejected before any
future staging, sandbox, or artifact operation is considered.

The current allowlists are:

- image steps: `grayscale`, `contrast`, `brightness`, `crop`, `resize`, and `rotate`;
- output formats: `png`, `jpeg`, and `webp`; and
- calculator operations: `add`, `subtract`, `multiply`, `divide`, `min`, and `max`.

Comparisons use a separate `check.v1` plan with explicit relational operators and an
`is_close` tolerance. Calculator operands are finite decimal values with bounded
precision; evaluation uses deterministic decimal arithmetic and a fixed result
precision ceiling. Image plans permit at most eight steps, 16,384-pixel dimensions,
opaque artifact identifiers, and metadata stripping by default. Output names and
filesystem locations are not part of the model-facing contract.

Every accepted plan has canonical JSON and a SHA-256 digest for future idempotency and
signature binding. The digest is an identity of the validated plan, not an authority
grant and not a substitute for a signed recipe bundle.

## Safety and failure contract

The parser rejects unknown fields, unknown operations, paths, non-opaque artifact IDs,
unsupported formats, oversized payloads, unsafe numeric bounds, non-finite numbers,
floating-point calculator input, division by zero, and invalid comparison tolerances.
Errors expose only stable categories such as `invalid_image_recipe`,
`invalid_calculation`, `invalid_check`, `payload_too_large`, or
`result_out_of_bounds`; raw payloads, paths, source text, and parser details do not
cross the API boundary.

`evaluate_calculator` and `evaluate_check` are pure trusted arithmetic helpers. They do
not read or write files, access the network, import generated code, decode images, or
publish artifacts. A future coordinator may call them only after policy, staging, and
sandbox gates have succeeded.

## Explicitly out of scope

This ADR does not authorize:

- installation or loading of a signed recipe/runtime bundle; manifest signature and
  byte verification are implemented separately in
  [the signed-manifest ADR](0001-phase2-signed-manifest.md);
- image codecs, thumbnails, or decompression handling;
- production broker named-pipe ACL, peer identity, framing, or IPC;
- artifact copy-in, output validation, atomic publication, or source ownership binding;
- Wasmtime/WASI, AppContainer/LPAC, Job Object, host process, or any other provider;
- model prompt/tool exposure, automatic execution, or application lifecycle enablement.

Those gates require their own implementation evidence and security review. The
packaged application remains on the explicitly disabled lifecycle from Phase 1.

## Required next gates

1. Implement the production broker contract with ACL, peer identity, framing, message
   limits, and confused-deputy tests.
2. Implement trusted copy-in/output validation and artifact publication tests,
   including parser fuzzing and source non-overwrite proofs.
3. Qualify the fixed-function provider inside the OS sandbox and wire it only through
   a passing lifecycle health check after external review.

## Verification

`tests/test_phase2_recipe_contract.py` covers canonical identity, malformed and
oversized plans, path/operation rejection, decimal determinism, division and result
limits, explicit comparisons, and redacted failures. The full repository and
frontend matrix remains required before this stage is merged.
