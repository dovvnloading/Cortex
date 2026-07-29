# ADR-0001 Phase 2 explicit qualification-profile lifecycle

- **Status:** Implemented as a local/CI lifecycle composition boundary; recipe request execution remains a separate slice
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
source application remain `disabled` by default. This stage does not add a
recipe API route, automatic model tool selection, worker-package discovery, or
persistent signing/trust material. A caller must deliberately provide those
qualified controls in local/CI code.

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

The next Phase 2 slice is the recipe-specific coordinator/request and artifact
publication path that can be injected behind this lifecycle. It must preserve
the same release gate, native launcher, broker identity, resource/watchdog, and
trusted artifact controls.

## Verification

`tests/test_phase2_qualification_lifecycle.py` covers exact profile parsing,
default-off behavior, missing configuration, official-gate rejection, blocked
health ordering, provider-health composition, coordinator startup, profile
diagnostics, and clean stop.
