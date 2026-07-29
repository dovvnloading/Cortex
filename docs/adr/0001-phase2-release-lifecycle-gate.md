# ADR-0001 Phase 2 release and lifecycle health preflight

- **Status:** Preflight implemented and verified; provider release remains blocked
- **Phase:** 2 - fixed-function image provider
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Depends on:** [signed worker provenance](0001-phase2-worker-provenance.md), [native launcher](0001-phase2-native-launcher.md), [native broker](0001-phase2-native-broker.md), and [Phase 1 lifecycle](0001-phase1-production-lifecycle.md)
- **Scope:** Read-only composition of mandatory release controls for a future lifecycle health callback

## Decision

`RecipeRuntimeReleaseGate` is the single preflight boundary that a future
`ExecutionLifecycle` integration may use for Phase 2 runtime health. It evaluates
mandatory controls in a fixed order:

1. the process must be running on the supported Windows boundary;
2. `verify_active_worker()` must re-check the installer-selected immutable
   generation, signed manifest, complete dependency tree, and fixed
   `recipe_worker.exe` role;
3. a reviewed native suspended-process factory must be configured;
4. a live broker identity binder must be configured; and
5. an explicit external security-review probe must return an available,
   bounded `RuntimeHealth` result.

The result is a `ReleaseGateSnapshot` containing only safe check names, stable
codes, and a `RuntimeHealth`. Missing or invalid controls return a blocked result
at the first failed check. Unexpected exceptions are reduced to stable categories;
paths, signatures, process IDs, token values, and exception text never cross this
boundary.

The preflight performs no process creation, broker binding, provider import,
image decode, artifact publication, or lifecycle mutation. A passing preflight
therefore proves only that the supplied release controls are present and that the
separately supplied review probe approved them. It does not itself enable a
provider or make a review claim trustworthy.

## External-review contract

The external-review callback is deliberately injected rather than inferred from
local files or a boolean build flag. A production caller must verify an
out-of-band review record against the release commit, package digest, native
launcher scope, and current threat model before returning `RuntimeHealth.ready()`.
Until that verifier exists and an approved review record is available, the gate
returns `external_review_required` or `external_review_unavailable` and the
provider remains absent from the API.

## Failure and lifecycle behavior

The fixed failure order is:

| Check | Blocked code | Meaning |
| --- | --- | --- |
| Native platform | `native_windows_required` | The reviewed Windows boundary is unavailable. |
| Signed worker | Existing `worker_*` provenance code | No verified immutable worker generation is active. |
| Process factory | `native_process_factory_required` | The reviewed suspended factory is not configured. |
| Broker identity | `native_broker_binding_required` | The live PID/AppContainer binder is not configured. |
| External review | `external_review_required`, `external_review_unavailable`, `external_review_result_invalid`, or `external_review_check_failed` | Release approval is absent, failed, or malformed. |

The snapshot is suitable for an `ExecutionLifecycle` health callback, but the
application must still construct an explicitly enabled lifecycle and a reviewed
coordinator as a separate release change. The current build remains disabled by
default, so ordinary chat readiness is unaffected.

## Invariants

1. A blocked or malformed check never authorizes process creation or provider
   loading.
2. The active worker is reverified at each preflight; a previous green result is
   never reused as launch authorization.
3. Adapter presence is necessary but not sufficient; the native launcher still
   enforces suspended creation, Job Object policy, live identity binding, and
   cleanup at launch time.
4. External review is explicit and release-scoped; local configuration cannot
   silently mark it approved.
5. No host-process, shell, stdio, in-process decode, or weaker-sandbox fallback is
   introduced by this preflight.

## Verification

`tests/test_phase2_release_gate.py` covers non-Windows refusal before provenance,
missing-worker refusal, mandatory adapter checks, explicit review requirements,
invalid/exceptional review redaction, and the all-controls-ready snapshot. The
preflight tests use disposable signed fixtures and assert that process and broker
methods are never called.

This stage closes the composition/preflight implementation gate. It does not close
the external security review, signed production package installation, real-worker
resource-enforcement, or provider lifecycle enablement gates.
