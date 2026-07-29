# ADR-0001 Phase 2 explicit qualification-profile lifecycle

- **Status:** Implemented as a local/CI lifecycle composition boundary; the
  qualification-only recipe API is now available behind it and the application
  remains default-off
- **Phase:** 2 - fixed-function image provider
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Depends on:** [Phase 2 release/lifecycle preflight](0001-phase2-release-lifecycle-gate.md), [recipe provider](0001-phase2-recipe-provider.md), and [Phase 1 lifecycle](0001-phase1-production-lifecycle.md)
- **Scope:** Explicit local/CI qualification-profile selection and health-gated lifecycle construction

## Decision

The source checkout now has one deliberate composition path for the fixed
qualification profile: `build_execution_lifecycle(...)` in
`cortex_backend.execution.qualification`. The profile parser accepts exactly
`disabled` and `qualification`; an omitted value is `disabled`.

The qualification path requires all of the following injected controls:

1. a `RecipeRuntimeReleaseGate` constructed with
   `release_profile="qualification"`;
2. a coordinator factory that owns the already-qualified worker boundary; and
3. a provider-health probe that receives the passing release health result.

The lifecycle evaluates those controls in order. A failed or malformed release
gate, provider probe, or coordinator startup returns a bounded blocked state and
keeps the coordinator unavailable. No path falls back to a host process,
in-process image decoding, or an unverified worker. The lifecycle records the
safe profile label `qualification` for diagnostics.

`Cortex_Preview.build_preview_app` accepts the explicit profile/configuration as
an injection point. Its normal call site supplies neither, so the packaged and
source application remain `disabled` by default. The helper
`build_recipe_coordinator_factory` now binds an injected, already-qualified
worker-attempt factory to the lifecycle-owned repository; it does not discover a
package, launch a process, bind a broker, or provide a host fallback. The
qualification-only recipe API route consumes an opaque artifact ID and remains
unavailable unless this lifecycle is ready. The companion
`POST /api/v1/execution/attachments` route uses the same ready lifecycle and
stages bounded bytes through the trusted artifact boundary; it does not discover
paths or accept executable content. `build_native_recipe_coordinator_factory`
provides the explicit signed/native attempt composition when callers supply the
installer, allowed user SIDs, and reviewed process-factory factory. Automatic
model tool selection, worker-package discovery, and persistent signing/trust
material remain separate gates.

## Failure and recovery behavior

- Missing qualification configuration is `qualification_configuration_missing`.
- Gate, provider, and result-shape exceptions are reduced to stable health codes;
  internal paths, exception text, process IDs, tokens, and signatures do not
  cross the lifecycle boundary.
- A coordinator is constructed only after every health check passes. If startup
  recovery fails, the existing lifecycle cleanup path closes the partial
  coordinator and leaves execution blocked.
- `disabled` never calls a health probe or coordinator factory.
- `official` release review remains inside `RecipeRuntimeReleaseGate` and is not
  selected by this local profile helper.

## Consequences

Open-source contributors can exercise the fixed, bounded runtime in local/CI
qualification code without an outside reviewer, production signing key, or
trusted release root. The normal application cannot be enabled accidentally by
importing the provider or setting an implicit default.

The recipe-specific coordinator/request, trusted attachment staging, native
attempt composition, and artifact-publication path plus their typed
qualification-only API surfaces are now implemented behind this lifecycle
boundary. It preserves the same release gate, native launcher, broker identity,
resource/watchdog, and trusted artifact controls. The durable-coordinator run
against the packaged signed worker is now implemented as a separate strict
qualification probe; it exercises cancellation, retention, publication,
owner-isolation, and native cleanup while remaining default-off in the
application.

## Verification

`tests/test_phase2_qualification_lifecycle.py` covers exact profile parsing,
default-off behavior, missing configuration, official-gate rejection, blocked
health ordering, provider-health composition, coordinator startup, profile
diagnostics, clean stop, and repository-bound worker-attempt factory wiring.
`tests/test_phase2_recipe_api.py` covers both routes' ready-lifecycle gates and
owner/idempotency/error behavior. `tests/test_phase2_native_recipe_attempt.py`
covers explicit native composition without launch during configuration.
The packaged-worker coordinator gate is
`tools/execution_spikes/recipe_coordinator_e2e_qualification.py`; its local
Windows run passed the transform, owner-isolation, retention, cancellation,
atomic-publication, and native-cleanup cases. Quality CI runs it after the
packaged worker release qualification.

## Next stage

Hosted Quality CI run [30467455657](https://github.com/dovvnloading/Cortex/actions/runs/30467455657)
and job [90628811567](https://github.com/dovvnloading/Cortex/actions/runs/30467455657/job/90628811567)
passed the coordinator gate on merged PR #64. This stage is complete. The
application remains disabled unless an explicit caller injects a ready
qualification profile; that default-off behavior is intentional until the normal
user-facing recipe flow is wired.
