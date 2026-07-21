# ADR-0001 Phase 1 production lifecycle and recovery gate

- **Status:** Implemented and verified; real execution providers remain disabled
- **Parent:** [Phase 1 recovery and approval contract](0001-phase1-recovery-approval.md)
- **Scope:** Application lifecycle ownership, runtime health gating, fake-only startup
  recovery wiring, and fail-closed shutdown

This stage connects the durable execution control plane to the production app
lifecycle without enabling Wasmtime, recipes, host processes, or arbitrary code. A
runtime is visible to the API only after an explicit health check passes and the
coordinator has safely recovered persisted work.

## Lifecycle state machine

```text
disabled  ───────────────────────────────────────────────┐
                                                         │
stopped ──health check──> starting ──success/recovery──> ready
   │                         │                            │
   │                         └──failure──────────────────> blocked
   │                                                      │
   └──────────────────────────────────────────────────────┘
ready ──shutdown──> stopping ──cleanup success──> stopped
                         └──cleanup failure─────> blocked
```

`ExecutionLifecycle` owns a repository, an injected coordinator factory, and a
runtime health callback. The factory is not called while the build is disabled or
the health result is unavailable. A failed check or factory is reduced to a safe
diagnostic (`runtime_unavailable` or `runtime_start_failed`) and leaves normal chat
readiness intact. No raw exception, path, token, or provider detail reaches the API.

## Startup and recovery contract

The FastAPI lifespan starts the lifecycle before accepting requests. A qualified
factory is constructed with `auto_recover=False`; the lifecycle then calls its
single `startup_recover()` hook exactly once. Recovery therefore owns the existing
single-instance lease, expired worker leases, immutable fake payload validation, and
ordered recovery events. The app publishes the coordinator to execution routes only
after the lifecycle reaches `ready`.

The current packaged/desktop build passes an explicitly disabled lifecycle. This is
intentional: the local Phase 0 probe can report blocked optional runtime controls,
and no unavailable or weaker provider may be selected as a fallback. The build can
continue serving chat and health endpoints while execution remains absent from the
system capability response.

## Shutdown contract

Application shutdown first stops ordinary chat jobs, then calls the lifecycle stop
hook. The lifecycle clears the coordinator before invoking cleanup, so new execution
requests cannot race shutdown. A successful stop releases the supervisor lease and
reports `stopped`; a cleanup failure reports `runtime_stop_failed` and keeps
execution unavailable. Stop is idempotent for disabled or already-stopped lifecycles.

## Required invariants

1. Disabled or unhealthy runtimes never invoke a coordinator factory.
2. A coordinator cannot be published when its repository is not the lifecycle's
   repository.
3. Startup recovery is invoked once per lifecycle start and before execution becomes
   available.
4. Factory, recovery, and shutdown failures are redacted to stable safe diagnostics.
5. Runtime unavailability does not make ordinary chat readiness fail.
6. The packaged build remains fake-provider-free at the API boundary unless a future
   explicitly qualified build supplies an enabled lifecycle.

## Verification

`tests/test_phase1_execution_lifecycle.py` covers disabled, health-blocked, healthy
startup/recovery/shutdown, and redacted factory-failure paths. Existing repository,
API, restart, approval, frontend, and Phase 0 contract tests remain required before
this stage can be merged.

This ADR does not authorize a production execution provider. Provider implementation,
broker ACL/framing, staging/publish validation, external review, and release enablement
remain separate gates.
